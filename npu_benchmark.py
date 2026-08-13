#!/usr/bin/env python3
"""Compile and benchmark a fixed-shape ONNX detector on the Ryzen AI NPU."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--cache-key", default="detector")
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


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

    compile_start = time.monotonic()
    session = ort.InferenceSession(
        str(args.model),
        sess_options=options,
        providers=["VitisAIExecutionProvider"],
        provider_options=[provider_options],
    )
    compile_seconds = time.monotonic() - compile_start
    active_providers = session.get_providers()
    if "VitisAIExecutionProvider" not in active_providers:
        raise RuntimeError(
            f"Vitis AI provider failed to initialize; active providers: {active_providers}"
        )
    input_info = session.get_inputs()[0]
    shape = [1 if not isinstance(item, int) or item <= 0 else item for item in input_info.shape]
    input_data = np.zeros(shape, dtype=np.float32)

    for _ in range(args.warmup):
        session.run(None, {input_info.name: input_data})

    timings: list[float] = []
    for _ in range(args.iterations):
        start = time.monotonic()
        outputs = session.run(None, {input_info.name: input_data})
        timings.append(time.monotonic() - start)

    total_seconds = sum(timings)
    result = {
        "model": str(args.model),
        "providers": active_providers,
        "input_name": input_info.name,
        "input_shape": shape,
        "output_shapes": [list(item.shape) for item in outputs],
        "compile_seconds": round(compile_seconds, 4),
        "warmup_iterations": args.warmup,
        "timed_iterations": args.iterations,
        "mean_latency_ms": round(1000.0 * total_seconds / args.iterations, 4),
        "fps": round(args.iterations / total_seconds, 4),
        "min_latency_ms": round(1000.0 * min(timings), 4),
        "max_latency_ms": round(1000.0 * max(timings), 4),
    }
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
