#!/usr/bin/env python3
"""Run one paired-camera workload through a GPU, NPU, or Hailo backend.

The decoder, image geometry, tracker, trajectory rules, reporting, and timing
boundaries are shared.  Only the detector backend and its model are changed.
This is deliberately separate from the production BoT-SORT pipeline: its job
is reproducible platform comparison, not production result generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from overtake_pipeline import (
    Observation,
    TrackHistory,
    deduplicate_candidates,
    evaluate_track,
)


VEHICLE_CLASSES = {2, 3, 5, 7}


@dataclass(frozen=True)
class Detection:
    class_id: int
    confidence: float
    left: float
    top: float
    right: float
    bottom: float

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.right - self.left) * max(0.0, self.bottom - self.top)


@dataclass
class ActiveTrack:
    history: TrackHistory

    @property
    def last_detection(self) -> Detection:
        item = self.history.observations[-1]
        return Detection(
            self.history.class_id,
            item.confidence,
            item.left,
            item.top,
            item.right,
            item.bottom,
        )


def box_iou(first: Detection, second: Detection) -> float:
    left = max(first.left, second.left)
    top = max(first.top, second.top)
    right = min(first.right, second.right)
    bottom = min(first.bottom, second.bottom)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    return intersection / max(first.area + second.area - intersection, 1e-9)


def center_distance(first: Detection, second: Detection) -> float:
    return math.hypot(
        first.center_x - second.center_x, first.center_y - second.center_y
    )


class CommonTracker:
    """Small deterministic tracker used identically for every detector backend."""

    def __init__(
        self,
        track_gap: float = 1.0,
        minimum_iou: float = 0.10,
        maximum_center_distance: float = 0.10,
    ) -> None:
        self.track_gap = track_gap
        self.minimum_iou = minimum_iou
        self.maximum_center_distance = maximum_center_distance
        self.next_id = 1
        self.active: dict[int, ActiveTrack] = {}
        self.completed: list[TrackHistory] = []

    def _close_stale(self, timestamp: float) -> None:
        stale = [
            identifier
            for identifier, track in self.active.items()
            if timestamp - track.history.last_seen > self.track_gap
        ]
        for identifier in stale:
            self.completed.append(self.active.pop(identifier).history)

    def update(self, timestamp: float, detections: Sequence[Detection]) -> None:
        self._close_stale(timestamp)
        possible: list[tuple[float, float, int, int]] = []
        for identifier, track in self.active.items():
            previous = track.last_detection
            for detection_index, detection in enumerate(detections):
                if detection.class_id != track.history.class_id:
                    continue
                overlap = box_iou(previous, detection)
                distance = center_distance(previous, detection)
                area_ratio = max(previous.area, detection.area) / max(
                    min(previous.area, detection.area), 1e-9
                )
                if overlap >= self.minimum_iou or (
                    distance <= self.maximum_center_distance and area_ratio <= 4.0
                ):
                    possible.append((overlap, -distance, identifier, detection_index))

        used_tracks: set[int] = set()
        used_detections: set[int] = set()
        for _, _, identifier, detection_index in sorted(possible, reverse=True):
            if identifier in used_tracks or detection_index in used_detections:
                continue
            detection = detections[detection_index]
            self.active[identifier].history.add(
                Observation(
                    timestamp,
                    detection.left,
                    detection.top,
                    detection.right,
                    detection.bottom,
                    detection.confidence,
                )
            )
            used_tracks.add(identifier)
            used_detections.add(detection_index)

        for index, detection in enumerate(detections):
            if index in used_detections:
                continue
            identifier = self.next_id
            self.next_id += 1
            history = TrackHistory(identifier, detection.class_id)
            history.add(
                Observation(
                    timestamp,
                    detection.left,
                    detection.top,
                    detection.right,
                    detection.bottom,
                    detection.confidence,
                )
            )
            self.active[identifier] = ActiveTrack(history)

    def finish(self) -> list[TrackHistory]:
        self.completed.extend(item.history for item in self.active.values())
        self.active.clear()
        return self.completed


def letterbox_rgb(
    frame: np.ndarray, size: int = 640
) -> tuple[np.ndarray, float, int, int]:
    height, width = frame.shape[:2]
    scale = min(size / width, size / height)
    resized_width = int(round(width * scale))
    resized_height = int(round(height * scale))
    if (resized_width, resized_height) != (width, height):
        raise ValueError(
            f"decoder must emit aspect-preserving model width; got {width}x{height}, "
            f"which would resize to {resized_width}x{resized_height}"
        )
    left = (size - resized_width) // 2
    top = (size - resized_height) // 2
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[top : top + resized_height, left : left + resized_width] = frame
    return canvas, scale, left, top


def normalized_detection(
    class_id: int,
    confidence: float,
    box: Sequence[float],
    frame_width: int,
    frame_height: int,
    scale: float,
    pad_left: int,
    pad_top: int,
    coordinates_are_normalized: bool = False,
    model_size: int = 640,
) -> Detection | None:
    left, top, right, bottom = (float(value) for value in box)
    if coordinates_are_normalized:
        left *= model_size
        top *= model_size
        right *= model_size
        bottom *= model_size
    left = float(np.clip((left - pad_left) / scale, 0, frame_width))
    right = float(np.clip((right - pad_left) / scale, 0, frame_width))
    top = float(np.clip((top - pad_top) / scale, 0, frame_height))
    bottom = float(np.clip((bottom - pad_top) / scale, 0, frame_height))
    if right <= left or bottom <= top:
        return None
    return Detection(
        int(class_id),
        float(confidence),
        left / frame_width,
        top / frame_height,
        right / frame_width,
        bottom / frame_height,
    )


class GpuDetector:
    name = "gpu"

    def __init__(self, args: argparse.Namespace) -> None:
        import torch
        from ultralytics import YOLO

        if not torch.cuda.is_available():
            raise RuntimeError("ROCm/CUDA GPU is unavailable to PyTorch")
        self.device = args.device
        self.confidence = args.confidence
        self.iou = args.iou
        self.model_size = args.model_size
        self.model_path = str(args.model)
        self.model = YOLO(self.model_path)
        self.device_name = torch.cuda.get_device_name(0)

    def infer(self, frame: np.ndarray) -> list[Detection]:
        canvas, scale, pad_left, pad_top = letterbox_rgb(frame, self.model_size)
        result = self.model.predict(
            canvas[:, :, ::-1].copy(),
            classes=sorted(VEHICLE_CLASSES),
            conf=self.confidence,
            iou=self.iou,
            imgsz=self.model_size,
            device=self.device,
            quantize=16,
            verbose=False,
        )[0]
        detections: list[Detection] = []
        if result.boxes is None:
            return detections
        for class_id, confidence, box in zip(
            result.boxes.cls.int().cpu().tolist(),
            result.boxes.conf.cpu().tolist(),
            result.boxes.xyxy.cpu().numpy(),
        ):
            detection = normalized_detection(
                class_id,
                confidence,
                box,
                frame.shape[1],
                frame.shape[0],
                scale,
                pad_left,
                pad_top,
                model_size=self.model_size,
            )
            if detection:
                detections.append(detection)
        return detections

    def metadata(self) -> dict:
        return {
            "backend": self.name,
            "model": self.model_path,
            "device": self.device_name,
            "precision": "FP16",
            "model_parameter_dtype": str(next(self.model.model.parameters()).dtype),
        }

    def close(self) -> None:
        return None


class NpuDetector:
    name = "npu"

    def __init__(self, args: argparse.Namespace) -> None:
        import onnxruntime as ort

        from npu_detect_frames import decode_outputs

        if args.cache_dir is None:
            raise ValueError("--cache-dir is required for the NPU backend")
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        options = ort.SessionOptions()
        options.log_severity_level = 2
        provider_options = {
            "cache_dir": str(args.cache_dir),
            "cache_key": args.cache_key,
            "enable_cache_file_io_in_mem": "0",
        }
        self.session = ort.InferenceSession(
            str(args.model),
            sess_options=options,
            providers=["VitisAIExecutionProvider"],
            provider_options=[provider_options],
        )
        if "VitisAIExecutionProvider" not in self.session.get_providers():
            raise RuntimeError(
                f"NPU provider unavailable: {self.session.get_providers()}"
            )
        self.input_name = self.session.get_inputs()[0].name
        self.input_shape = list(self.session.get_inputs()[0].shape)
        if len(self.input_shape) != 4:
            raise RuntimeError(f"unsupported NPU input shape: {self.input_shape}")
        if self.input_shape[1] == 3:
            self.input_layout = "NCHW"
        elif self.input_shape[-1] == 3:
            self.input_layout = "NHWC"
        else:
            raise RuntimeError(f"unsupported NPU image layout: {self.input_shape}")
        self.output_shapes = [list(item.shape) for item in self.session.get_outputs()]
        self.decode_outputs = decode_outputs
        self.confidence = args.confidence
        self.iou = args.iou
        self.model_size = args.model_size
        self.model_path = str(args.model)

    def infer(self, frame: np.ndarray) -> list[Detection]:
        canvas, scale, pad_left, pad_top = letterbox_rgb(frame, self.model_size)
        input_data = canvas.astype(np.float32) / 255.0
        if self.input_layout == "NCHW":
            input_data = np.transpose(input_data, (2, 0, 1))
        outputs = self.session.run(
            None, {self.input_name: input_data[None, ...]}
        )
        decoded = self.decode_outputs(
            outputs,
            confidence=self.confidence,
            iou_threshold=self.iou,
            scale=scale,
            pad_left=pad_left,
            pad_top=pad_top,
            frame_width=frame.shape[1],
            frame_height=frame.shape[0],
        )
        return [
            Detection(item["class_id"], item["confidence"], *item["normalized_box"])
            for item in decoded
        ]

    def metadata(self) -> dict:
        return {
            "backend": self.name,
            "model": self.model_path,
            "device": "Ryzen AI NPU",
            "precision": "AMD quantized",
            "providers": self.session.get_providers(),
            "input_layout": self.input_layout,
            "input_shape": self.input_shape,
            "output_shapes": self.output_shapes,
        }

    def close(self) -> None:
        return None


def _hailo_class_arrays(value: object) -> list[np.ndarray]:
    """Normalize Hailo NMS output to one Nx5 array per COCO class."""

    if isinstance(value, list):
        current: object = value
        while (
            isinstance(current, list)
            and len(current) == 1
            and isinstance(current[0], list)
        ):
            current = current[0]
        if isinstance(current, list) and len(current) >= 80:
            return [
                np.asarray(item, dtype=np.float32).reshape(-1, 5) for item in current
            ]
    array = np.asarray(value)
    while array.ndim > 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim == 3 and array.shape[0] >= 80 and array.shape[1] == 5:
        return [array[index].T for index in range(80)]
    if array.ndim == 3 and array.shape[0] >= 80 and array.shape[2] == 5:
        return [array[index] for index in range(80)]
    raise RuntimeError(
        f"unsupported Hailo NMS result shape/type: {array.shape} / {type(value)}"
    )


class HailoDetector:
    name = "hailo"

    def __init__(self, args: argparse.Namespace) -> None:
        from hailo_platform import (
            FormatType,
            HEF,
            InferVStreams,
            InputVStreamParams,
            OutputVStreamParams,
            VDevice,
        )

        self.model_size = args.model_size
        self.model_path = str(args.model)
        self.confidence = args.confidence
        self.vdevice = VDevice()
        self.hef = HEF(self.model_path)
        self.configured = self.vdevice.configure(self.hef)[0]
        self.activation = self.configured.activate()
        self.activation.__enter__()
        input_params = InputVStreamParams.make(
            self.configured, format_type=FormatType.UINT8
        )
        output_params = OutputVStreamParams.make(
            self.configured, format_type=FormatType.FLOAT32
        )
        self.pipeline = InferVStreams(
            self.configured, input_params, output_params, tf_nms_format=False
        )
        self.pipeline.__enter__()
        self.input_name = self.hef.get_input_vstream_infos()[0].name
        self.output_name = self.hef.get_output_vstream_infos()[0].name

    def infer(self, frame: np.ndarray) -> list[Detection]:
        canvas, scale, pad_left, pad_top = letterbox_rgb(frame, self.model_size)
        result = self.pipeline.infer({self.input_name: canvas[None, ...]})
        class_arrays = _hailo_class_arrays(result[self.output_name])
        detections: list[Detection] = []
        for class_id in sorted(VEHICLE_CLASSES):
            for row in class_arrays[class_id]:
                y_min, x_min, y_max, x_max, score = (float(value) for value in row[:5])
                if score < self.confidence:
                    continue
                normalized = max(abs(x_min), abs(y_min), abs(x_max), abs(y_max)) <= 2.0
                detection = normalized_detection(
                    class_id,
                    score,
                    (x_min, y_min, x_max, y_max),
                    frame.shape[1],
                    frame.shape[0],
                    scale,
                    pad_left,
                    pad_top,
                    coordinates_are_normalized=normalized,
                    model_size=self.model_size,
                )
                if detection:
                    detections.append(detection)
        return detections

    def metadata(self) -> dict:
        return {
            "backend": self.name,
            "model": self.model_path,
            "device": "Hailo-8L",
            "precision": "quantized HEF",
        }

    def close(self) -> None:
        try:
            self.pipeline.__exit__(None, None, None)
        finally:
            try:
                self.activation.__exit__(None, None, None)
            finally:
                self.vdevice.release()


def make_detector(
    args: argparse.Namespace,
) -> GpuDetector | NpuDetector | HailoDetector:
    if args.backend == "gpu":
        return GpuDetector(args)
    if args.backend == "npu":
        return NpuDetector(args)
    if args.backend == "hailo":
        return HailoDetector(args)
    raise ValueError(args.backend)


def frame_reader(
    source: Path,
    sample_fps: float,
    width: int,
    height: int,
    decode: str,
    duration: float | None,
    ffmpeg_binary: str = "ffmpeg",
    decoder_threads: int = 0,
) -> Iterable[tuple[float, np.ndarray]]:
    command = [ffmpeg_binary, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if decode == "vaapi":
        command.extend(["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"])
    elif decode == "drm":
        command.extend(["-hwaccel", "drm"])
    if decoder_threads:
        command.extend(["-threads", str(decoder_threads)])
    command.extend(["-i", str(source)])
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(
        [
            "-an",
            "-sn",
            "-vf",
            f"fps={sample_fps},scale={width}:{height}:flags=fast_bilinear",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
    )
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    frame_size = width * height * 3
    frame_index = 0
    try:
        while True:
            payload = process.stdout.read(frame_size)
            if not payload:
                break
            if len(payload) != frame_size:
                raise RuntimeError(
                    f"short FFmpeg frame: {len(payload)} of {frame_size} bytes"
                )
            yield (
                frame_index / sample_fps,
                np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 3),
            )
            frame_index += 1
    finally:
        stderr = (
            process.stderr.read().decode("utf-8", errors="replace")
            if process.stderr
            else ""
        )
        return_code = process.wait()
        if return_code:
            raise RuntimeError(
                f"FFmpeg decode failed ({return_code}): {stderr.strip()}"
            )


def probe_video(path: Path, ffprobe_binary: str = "ffprobe") -> dict:
    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,codec_name:format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    numerator, denominator = stream["avg_frame_rate"].split("/", 1)
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "frame_rate": float(numerator) / float(denominator),
        "codec": stream["codec_name"],
        "duration": float(payload["format"]["duration"]),
        "size": int(payload["format"]["size"]),
    }


def source_snapshot(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mode": stat.st_mode,
        "uid": stat.st_uid,
        "gid": stat.st_gid,
    }


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def process_camera(
    detector: GpuDetector | NpuDetector | HailoDetector,
    source: Path,
    camera: str,
    args: argparse.Namespace,
    output_dir: Path,
) -> dict:
    info = probe_video(source, args.ffprobe)
    detect_height = int(round(info["height"] * args.detect_width / info["width"]))
    if detect_height % 2:
        detect_height += 1
    tracker = CommonTracker(
        args.track_gap, args.minimum_track_iou, args.maximum_center_distance
    )
    frames = 0
    detections_seen = 0
    inference_seconds = 0.0
    started = time.monotonic()
    for timestamp, frame in frame_reader(
        source,
        args.sample_fps,
        args.detect_width,
        detect_height,
        args.decode,
        args.duration,
        args.ffmpeg,
        args.decoder_threads,
    ):
        inference_started = time.monotonic()
        detections = detector.infer(frame)
        inference_seconds += time.monotonic() - inference_started
        tracker.update(timestamp, detections)
        frames += 1
        detections_seen += len(detections)
        if args.progress_every and frames % args.progress_every == 0:
            elapsed = time.monotonic() - started
            source_seconds = frames / args.sample_fps
            print(
                f"camera={camera} frames={frames} source={source_seconds:.1f}s "
                f"wall={elapsed:.1f}s speed={source_seconds / elapsed:.2f}x",
                flush=True,
            )
    wall_seconds = time.monotonic() - started
    tracks = tracker.finish()
    summaries = deduplicate_candidates(
        [evaluate_track(track, camera) for track in tracks]
    )
    events = [item for item in summaries if item["candidate"]]
    atomic_json(output_dir / "tracks.json", summaries)
    atomic_json(output_dir / "events.json", events)
    result = {
        "camera": camera,
        "source": str(source),
        "source_video": info,
        "detect_size": [args.detect_width, detect_height],
        "processed_frames": frames,
        "processed_source_seconds": round(frames / args.sample_fps, 3),
        "wall_seconds": round(wall_seconds, 6),
        "inference_seconds": round(inference_seconds, 6),
        "inference_fps": round(frames / inference_seconds, 3)
        if inference_seconds
        else None,
        "realtime_factor": round((frames / args.sample_fps) / wall_seconds, 3),
        "vehicle_detections": detections_seen,
        "completed_tracks": len(summaries),
        "candidate_events": len(events),
    }
    atomic_json(output_dir / "run.json", result)
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=("gpu", "npu", "hailo"))
    parser.add_argument("--front", required=True, type=Path)
    parser.add_argument("--rear", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--cache-key", default="paired-platform-benchmark-v1")
    parser.add_argument("--device", default="0")
    parser.add_argument("--decode", choices=("cpu", "vaapi", "drm"), default="cpu")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--decoder-threads", type=int, default=0)
    parser.add_argument("--sample-fps", type=float, default=5.0)
    parser.add_argument("--detect-width", type=int, default=640)
    parser.add_argument("--model-size", type=int, default=640)
    parser.add_argument("--confidence", type=float, default=0.20)
    parser.add_argument("--iou", type=float, default=0.50)
    parser.add_argument("--track-gap", type=float, default=1.0)
    parser.add_argument("--minimum-track-iou", type=float, default=0.10)
    parser.add_argument("--maximum-center-distance", type=float, default=0.10)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--progress-every", type=int, default=500)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    args.front = args.front.resolve()
    args.rear = args.rear.resolve()
    args.output_dir = args.output_dir.resolve()
    args.model = args.model.resolve()
    if args.cache_dir is not None:
        args.cache_dir = args.cache_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_sha256 = sha256_file(args.model)
    before = {"front": source_snapshot(args.front), "rear": source_snapshot(args.rear)}
    pair_started_epoch = time.time()
    setup_started = time.monotonic()
    detector = make_detector(args)
    setup_seconds = time.monotonic() - setup_started
    try:
        warmup_frame = np.zeros((360, 640, 3), dtype=np.uint8)
        warmup_started = time.monotonic()
        detector.infer(warmup_frame)
        warmup_seconds = time.monotonic() - warmup_started
        processing_started = time.monotonic()
        front = process_camera(
            detector, args.front, "front", args, args.output_dir / "front"
        )
        rear = process_camera(
            detector, args.rear, "rear", args, args.output_dir / "rear"
        )
        processing_seconds = time.monotonic() - processing_started
    finally:
        detector.close()
    pair_ended_epoch = time.time()
    after = {"front": source_snapshot(args.front), "rear": source_snapshot(args.rear)}
    sources_unchanged = before == after
    source_seconds = (
        front["processed_source_seconds"] + rear["processed_source_seconds"]
    )
    detector_metadata = detector.metadata()
    detector_metadata["model_sha256"] = model_sha256
    payload = {
        "schema_version": 1,
        "benchmark_scope": "common detection, tracking, trajectory evaluation, and reports; no clips",
        "backend": args.backend,
        "detector": detector_metadata,
        "host": {
            "hostname": platform.node(),
            "system": platform.platform(),
            "python": sys.version.split()[0],
        },
        "settings": {
            "sample_fps": args.sample_fps,
            "detect_width": args.detect_width,
            "model_size": args.model_size,
            "confidence": args.confidence,
            "iou": args.iou,
            "track_gap": args.track_gap,
            "minimum_track_iou": args.minimum_track_iou,
            "maximum_center_distance": args.maximum_center_distance,
            "decode": args.decode,
            "decoder_threads": args.decoder_threads or "automatic",
            "pair_scheduling": "sequential",
            "duration_limit_seconds_per_camera": args.duration,
        },
        "timing": {
            "start_epoch": pair_started_epoch,
            "end_epoch": pair_ended_epoch,
            "setup_seconds": round(setup_seconds, 6),
            "warmup_seconds": round(warmup_seconds, 6),
            "processing_seconds": round(processing_seconds, 6),
            "total_wall_seconds": round(pair_ended_epoch - pair_started_epoch, 6),
            "total_source_seconds": round(source_seconds, 3),
            "realtime_factor": round(source_seconds / processing_seconds, 3),
        },
        "quality": {
            "front_candidate_events": front["candidate_events"],
            "rear_candidate_events": rear["candidate_events"],
            "total_candidate_events": front["candidate_events"]
            + rear["candidate_events"],
            "front_vehicle_detections": front["vehicle_detections"],
            "rear_vehicle_detections": rear["vehicle_detections"],
        },
        "cameras": {"front": front, "rear": rear},
        "source_evidence": {
            "before": before,
            "after": after,
            "unchanged": sources_unchanged,
        },
        "valid": bool(
            sources_unchanged and front["processed_frames"] and rear["processed_frames"]
        ),
    }
    atomic_json(args.output_dir / "result.json", payload)
    print(json.dumps(payload, indent=2), flush=True)
    return 0 if payload["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
