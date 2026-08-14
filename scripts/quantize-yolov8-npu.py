#!/usr/bin/env python3
"""Quantize a standard Ultralytics YOLOv8 ONNX graph for Ryzen AI.

The image calibration path deliberately stays separate from the benchmark
pair.  YOLO's final box-decoding graph remains in floating point, following
AMD's object-detection example, while the detector body is quantized.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import onnx
from onnxruntime.quantization import CalibrationDataReader
from quark.onnx import ModelQuantizer
from quark.onnx.quantization.config import custom_config as qcc
from quark.onnx.quantization.config.config import Config


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def input_details(model_path: Path) -> tuple[str, int, int]:
    model = onnx.load(model_path)
    tensor = model.graph.input[0]
    dimensions = tensor.type.tensor_type.shape.dim
    shape = [item.dim_value for item in dimensions]
    if len(shape) != 4 or shape[1] != 3 or min(shape[2:]) <= 0:
        raise ValueError(f"expected a static NCHW image input, got {shape}")
    return tensor.name, shape[2], shape[3]


def excluded_postprocess(model_path: Path) -> list[tuple[list[str], list[str]]]:
    """Return AMD's YOLO post-process exclusion boundary."""

    model = onnx.load(model_path)
    concat_nodes = [node for node in model.graph.node if node.op_type == "Concat"]
    if len(concat_nodes) < 4:
        raise ValueError("YOLO graph does not contain the expected Concat nodes")
    # A standard Ultralytics detect head has two parallel inputs to its final
    # floating-point decoder: box distributions and class logits.  Both must
    # be named as starts for Quark to recognise a closed subgraph.
    return [
        (
            [concat_nodes[-4].name, concat_nodes[-3].name],
            [concat_nodes[-1].name],
        )
    ]


def topologically_sort(model: onnx.ModelProto) -> onnx.ModelProto:
    """Repair stable node ordering after Quark graph rewrites."""

    pending = list(model.graph.node)
    produced = {name for node in pending for name in node.output if name}
    available = {item.name for item in model.graph.input}
    available.update(item.name for item in model.graph.initializer)
    available.update(
        name for node in pending for name in node.input if name and name not in produced
    )
    ordered = []
    while pending:
        ready = [
            node
            for node in pending
            if all(not name or name in available for name in node.input)
        ]
        if not ready:
            names = ", ".join(node.name or node.op_type for node in pending[:5])
            raise ValueError(f"could not topologically order quantized graph near: {names}")
        for node in ready:
            ordered.append(node)
            available.update(name for name in node.output if name)
            pending.remove(node)
    del model.graph.node[:]
    model.graph.node.extend(ordered)
    return model


def letterbox_rgb(image_bgr: np.ndarray, height: int, width: int) -> np.ndarray:
    source_height, source_width = image_bgr.shape[:2]
    scale = min(width / source_width, height / source_height)
    resized_width = int(round(source_width * scale))
    resized_height = int(round(source_height * scale))
    resized = cv2.resize(
        cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB),
        (resized_width, resized_height),
        interpolation=cv2.INTER_LINEAR,
    )
    canvas = np.full((height, width, 3), 114, dtype=np.uint8)
    left = (width - resized_width) // 2
    top = (height - resized_height) // 2
    canvas[top : top + resized_height, left : left + resized_width] = resized
    return np.transpose(canvas.astype(np.float32) / 255.0, (2, 0, 1))[None, ...]


class ImageDataReader(CalibrationDataReader):
    def __init__(
        self,
        image_paths: list[Path],
        input_name: str,
        height: int,
        width: int,
    ) -> None:
        samples = []
        for path in image_paths:
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f"could not read calibration image: {path}")
            samples.append({input_name: letterbox_rgb(image, height, width)})
        self.samples = samples
        self.iterator = None

    def get_next(self):
        if self.iterator is None:
            self.iterator = iter(self.samples)
        return next(self.iterator, None)

    def rewind(self) -> None:
        self.iterator = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--calibration-dir", required=True, type=Path)
    parser.add_argument("--calibration-count", type=int, default=128)
    parser.add_argument("--config", default="XINT8")
    parser.add_argument("--metadata", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    image_paths = sorted(args.calibration_dir.glob("*.jpg"))
    if len(image_paths) < args.calibration_count:
        raise ValueError(
            f"need {args.calibration_count} calibration images, found {len(image_paths)}"
        )
    image_paths = image_paths[: args.calibration_count]
    input_name, height, width = input_details(args.input)
    reader = ImageDataReader(image_paths, input_name, height, width)

    quant_config = copy.deepcopy(qcc.get_default_config(args.config))
    quant_config.subgraphs_to_exclude = excluded_postprocess(args.input)
    quant_config.execution_providers = ["CPUExecutionProvider"]
    quant_config.extra_op_types_to_quantize = ["Einsum", "ReduceMax"]
    quant_config.extra_options["CopySharedInit"] = True
    configuration = Config(global_quant_config=quant_config)
    configuration.global_quant_config.log_severity_level = 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    ModelQuantizer(configuration).quantize_model(
        args.input.as_posix(), args.output.as_posix(), reader
    )
    quantized_model = topologically_sort(onnx.load(args.output))
    onnx.save(quantized_model, args.output)
    onnx.checker.check_model(quantized_model)

    metadata_path = args.metadata or args.output.with_suffix(".metadata.json")
    payload = {
        "schema_version": 1,
        "source_model": str(args.input),
        "source_sha256": sha256(args.input),
        "quantized_model": str(args.output),
        "quantized_sha256": sha256(args.output),
        "quantization_config": args.config,
        "input_name": input_name,
        "input_shape": [1, 3, height, width],
        "postprocess_excluded": True,
        "calibration_images": len(image_paths),
        "calibration_files": [path.name for path in image_paths],
    }
    metadata_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
