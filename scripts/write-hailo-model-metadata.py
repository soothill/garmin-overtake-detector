#!/usr/bin/env python3
"""Write reproducibility evidence for a Hailo HEF compiled from YOLO weights."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-weights", required=True, type=Path)
    parser.add_argument("--source-onnx", required=True, type=Path)
    parser.add_argument("--calibration-dir", required=True, type=Path)
    parser.add_argument("--hef", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    calibration = sorted(
        path
        for path in args.calibration_dir.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    payload = {
        "schema_version": 1,
        "host": platform.node(),
        "source_weights": str(args.source_weights.resolve()),
        "source_weights_sha256": sha256(args.source_weights),
        "source_onnx": str(args.source_onnx.resolve()),
        "source_onnx_sha256": sha256(args.source_onnx),
        "hef": str(args.hef.resolve()),
        "hef_sha256": sha256(args.hef),
        "target_architecture": "hailo8l",
        "model": "yolov8s",
        "calibration_image_count": len(calibration),
        "calibration_images": [
            {"name": path.name, "sha256": sha256(path)} for path in calibration
        ],
        "hailomz_version": version(["hailomz", "--version"]),
        "hailortcli_version": version(["hailortcli", "--version"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
