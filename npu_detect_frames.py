#!/usr/bin/env python3
"""Run AMD's quantized YOLOv8m NPU model on RGB frames and decode detections."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort


CLASS_NAMES = (
    "person bicycle car motorcycle airplane bus train truck boat traffic_light "
    "fire_hydrant stop_sign parking_meter bench bird cat dog horse sheep cow elephant "
    "bear zebra giraffe backpack umbrella handbag tie suitcase frisbee skis snowboard "
    "sports_ball kite baseball_bat baseball_glove skateboard surfboard tennis_racket "
    "bottle wine_glass cup fork knife spoon bowl banana apple sandwich orange broccoli "
    "carrot hot_dog pizza donut cake chair couch potted_plant bed dining_table toilet tv "
    "laptop mouse remote keyboard cell_phone microwave oven toaster sink refrigerator "
    "book clock vase scissors teddy_bear hair_drier toothbrush"
).split()
VEHICLE_CLASSES = {2, 3, 5, 7}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--cache-key", default="yolov8m-real-frames")
    parser.add_argument(
        "--frame",
        action="append",
        required=True,
        metavar="TIMESTAMP:RGB_FILE",
        help="640x360 packed RGB24 frame with its source timestamp",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--confidence", type=float, default=0.20)
    parser.add_argument("--iou", type=float, default=0.50)
    return parser.parse_args()


def sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -30.0, 30.0)))


def softmax(values: np.ndarray, axis: int) -> np.ndarray:
    shifted = values - np.max(values, axis=axis, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / np.sum(exponentials, axis=axis, keepdims=True)


def make_anchors() -> tuple[np.ndarray, np.ndarray]:
    anchors: list[np.ndarray] = []
    strides: list[np.ndarray] = []
    for size, stride in ((80, 8), (40, 16), (20, 32)):
        y, x = np.meshgrid(
            np.arange(size, dtype=np.float32) + 0.5,
            np.arange(size, dtype=np.float32) + 0.5,
            indexing="ij",
        )
        anchors.append(np.stack((x.reshape(-1), y.reshape(-1))))
        strides.append(np.full((1, size * size), stride, dtype=np.float32))
    return np.concatenate(anchors, axis=1), np.concatenate(strides, axis=1)


def box_iou(box: np.ndarray, boxes: np.ndarray) -> np.ndarray:
    left_top = np.maximum(box[:2], boxes[:, :2])
    right_bottom = np.minimum(box[2:], boxes[:, 2:])
    intersection = np.prod(np.maximum(0.0, right_bottom - left_top), axis=1)
    box_area = np.prod(np.maximum(0.0, box[2:] - box[:2]))
    boxes_area = np.prod(np.maximum(0.0, boxes[:, 2:] - boxes[:, :2]), axis=1)
    return intersection / np.maximum(box_area + boxes_area - intersection, 1e-9)


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    order = np.argsort(scores)[::-1]
    keep: list[int] = []
    while order.size:
        index = int(order[0])
        keep.append(index)
        if order.size == 1:
            break
        remaining = order[1:]
        order = remaining[box_iou(boxes[index], boxes[remaining]) <= threshold]
    return keep


def letterbox(frame: np.ndarray, size: int = 640) -> tuple[np.ndarray, float, int, int]:
    height, width = frame.shape[:2]
    scale = min(size / width, size / height)
    resized_width = int(round(width * scale))
    resized_height = int(round(height * scale))
    if (resized_width, resized_height) != (width, height):
        raise ValueError(
            "Input raw frame must already have the model-width aspect-preserving size; "
            f"got {width}x{height}, expected {resized_width}x{resized_height}"
        )
    left = (size - resized_width) // 2
    top = (size - resized_height) // 2
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[top : top + resized_height, left : left + resized_width] = frame
    return canvas.astype(np.float32)[None, ...] / 255.0, scale, left, top


def decode_outputs(
    outputs: list[np.ndarray],
    confidence: float,
    iou_threshold: float,
    scale: float,
    pad_left: int,
    pad_top: int,
    frame_width: int,
    frame_height: int,
) -> list[dict]:
    feature_maps = [output.transpose(0, 3, 1, 2) for output in outputs]
    combined = np.concatenate(
        [feature.reshape(feature.shape[0], 144, -1) for feature in feature_maps],
        axis=2,
    )
    box_logits, class_logits = np.split(combined, (64,), axis=1)
    distributions = softmax(box_logits.reshape(1, 4, 16, -1), axis=2)
    bins = np.arange(16, dtype=np.float32).reshape(1, 1, 16, 1)
    distances = np.sum(distributions * bins, axis=2)[0]
    anchors, strides = make_anchors()
    top_left = anchors - distances[:2]
    bottom_right = anchors + distances[2:]
    boxes = np.concatenate((top_left, bottom_right), axis=0) * strides

    probabilities = sigmoid(class_logits[0])
    class_ids = np.argmax(probabilities, axis=0)
    scores = probabilities[class_ids, np.arange(probabilities.shape[1])]
    selected = np.flatnonzero(
        (scores >= confidence) & np.isin(class_ids, tuple(VEHICLE_CLASSES))
    )
    if not selected.size:
        return []

    selected_boxes = boxes[:, selected].T
    selected_scores = scores[selected]
    selected_classes = class_ids[selected]
    detections: list[dict] = []
    for class_id in sorted(set(selected_classes.tolist())):
        class_positions = np.flatnonzero(selected_classes == class_id)
        kept_positions = nms(
            selected_boxes[class_positions],
            selected_scores[class_positions],
            iou_threshold,
        )
        for kept in kept_positions:
            position = int(class_positions[kept])
            x1, y1, x2, y2 = selected_boxes[position]
            x1 = float(np.clip((x1 - pad_left) / scale, 0, frame_width))
            x2 = float(np.clip((x2 - pad_left) / scale, 0, frame_width))
            y1 = float(np.clip((y1 - pad_top) / scale, 0, frame_height))
            y2 = float(np.clip((y2 - pad_top) / scale, 0, frame_height))
            if x2 <= x1 or y2 <= y1:
                continue
            detections.append(
                {
                    "class_id": int(class_id),
                    "class_name": CLASS_NAMES[int(class_id)],
                    "confidence": round(float(selected_scores[position]), 5),
                    "box": [round(value, 2) for value in (x1, y1, x2, y2)],
                    "normalized_box": [
                        round(x1 / frame_width, 5),
                        round(y1 / frame_height, 5),
                        round(x2 / frame_width, 5),
                        round(y2 / frame_height, 5),
                    ],
                }
            )
    return sorted(detections, key=lambda item: item["confidence"], reverse=True)


def load_frame(specification: str, width: int, height: int) -> tuple[float, Path, np.ndarray]:
    timestamp_text, separator, path_text = specification.partition(":")
    if not separator:
        raise ValueError(f"Invalid --frame {specification!r}; expected TIMESTAMP:RGB_FILE")
    timestamp = float(timestamp_text)
    path = Path(path_text)
    payload = path.read_bytes()
    expected = width * height * 3
    if len(payload) != expected:
        raise ValueError(f"{path} is {len(payload)} bytes; expected {expected}")
    return timestamp, path, np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 3)


def main() -> int:
    args = parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    options = ort.SessionOptions()
    options.log_severity_level = 2
    provider_options = {
        "cache_dir": str(args.cache_dir),
        "cache_key": args.cache_key,
        "enable_cache_file_io_in_mem": "0",
    }
    session = ort.InferenceSession(
        str(args.model),
        sess_options=options,
        providers=["VitisAIExecutionProvider"],
        provider_options=[provider_options],
    )
    if "VitisAIExecutionProvider" not in session.get_providers():
        raise RuntimeError(f"NPU provider unavailable: {session.get_providers()}")

    input_name = session.get_inputs()[0].name
    results = []
    for specification in args.frame:
        timestamp, path, frame = load_frame(specification, args.width, args.height)
        input_data, scale, pad_left, pad_top = letterbox(frame)
        started = time.monotonic()
        outputs = session.run(None, {input_name: input_data})
        inference_ms = (time.monotonic() - started) * 1000.0
        detections = decode_outputs(
            outputs,
            confidence=args.confidence,
            iou_threshold=args.iou,
            scale=scale,
            pad_left=pad_left,
            pad_top=pad_top,
            frame_width=args.width,
            frame_height=args.height,
        )
        results.append(
            {
                "timestamp": timestamp,
                "frame": str(path),
                "inference_ms": round(inference_ms, 4),
                "vehicle_detections": detections,
            }
        )

    payload = {
        "model": str(args.model),
        "providers": session.get_providers(),
        "confidence": args.confidence,
        "iou": args.iou,
        "frames": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
