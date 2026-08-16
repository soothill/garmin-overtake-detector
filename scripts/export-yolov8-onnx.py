#!/usr/bin/env python3
"""Export fixed-shape YOLOv8 weights to an ONNX detector graph."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from ultralytics import YOLO


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--size", type=int, default=640)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    exported = Path(
        YOLO(str(args.model)).export(
            format="onnx",
            imgsz=args.size,
            opset=args.opset,
            simplify=True,
            dynamic=False,
            nms=False,
            device="cpu",
        )
    )
    if exported.resolve() != args.output.resolve():
        shutil.move(exported, args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
