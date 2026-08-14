#!/usr/bin/env python3
"""Wait for an AMD SMI power log to contain a genuinely idle sample window."""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
from pathlib import Path


METRICS = {
    "system_package": "apu_average_socket_power",
    "gpu": "apu_average_gfx_power",
    "npu": "apu_average_ipu_power",
}


def read_samples(path: Path) -> list[dict[str, float]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (FileNotFoundError, PermissionError):
        return []

    samples: list[dict[str, float]] = []
    for row in rows:
        try:
            sample = {"timestamp": float(row["timestamp"])}
            sample.update(
                {name: float(row[column]) for name, column in METRICS.items()}
            )
        except (KeyError, TypeError, ValueError):
            continue
        samples.append(sample)
    return samples


def assess_window(
    samples: list[dict[str, float]],
    window_size: int,
    mean_limits: dict[str, float],
    peak_limits: dict[str, float],
) -> dict:
    if len(samples) < window_size:
        return {
            "valid": False,
            "reason": "insufficient_samples",
            "sample_count": len(samples),
            "required_samples": window_size,
        }

    window = samples[-window_size:]
    metrics = {}
    failures = []
    for name in METRICS:
        values = [sample[name] for sample in window]
        mean = statistics.fmean(values)
        peak = max(values)
        metrics[name] = {
            "mean_watts": round(mean, 6),
            "minimum_watts": round(min(values), 6),
            "maximum_watts": round(peak, 6),
            "standard_deviation_watts": round(statistics.pstdev(values), 6),
        }
        if mean > mean_limits[name]:
            failures.append(f"{name}_mean")
        if peak > peak_limits[name]:
            failures.append(f"{name}_peak")

    return {
        "valid": not failures,
        "reason": "idle" if not failures else "limits_exceeded",
        "sample_count": window_size,
        "start_epoch": window[0]["timestamp"],
        "end_epoch": window[-1]["timestamp"],
        "metrics": metrics,
        "failures": failures,
    }


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--window-samples", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--package-mean-max", type=float, default=30.0)
    parser.add_argument("--package-peak-max", type=float, default=40.0)
    parser.add_argument("--gpu-mean-max", type=float, default=2.0)
    parser.add_argument("--gpu-peak-max", type=float, default=5.0)
    parser.add_argument("--npu-mean-max", type=float, default=0.5)
    parser.add_argument("--npu-peak-max", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.window_samples < 2 or args.timeout <= 0 or args.poll_seconds <= 0:
        raise SystemExit(
            "window size must be at least two and time values must be positive"
        )

    mean_limits = {
        "system_package": args.package_mean_max,
        "gpu": args.gpu_mean_max,
        "npu": args.npu_mean_max,
    }
    peak_limits = {
        "system_package": args.package_peak_max,
        "gpu": args.gpu_peak_max,
        "npu": args.npu_peak_max,
    }
    started = time.time()
    latest: dict = {"valid": False, "reason": "no_samples"}
    while time.time() - started < args.timeout:
        latest = assess_window(
            read_samples(args.csv), args.window_samples, mean_limits, peak_limits
        )
        if latest["valid"]:
            payload = {
                "schema_version": 1,
                **latest,
                "wait_started_epoch": started,
                "wait_seconds": round(time.time() - started, 6),
                "mean_limits_watts": mean_limits,
                "peak_limits_watts": peak_limits,
            }
            atomic_write(args.output, payload)
            print(json.dumps(payload, indent=2))
            return 0
        time.sleep(args.poll_seconds)

    payload = {
        "schema_version": 1,
        **latest,
        "valid": False,
        "reason": "idle_timeout",
        "wait_started_epoch": started,
        "wait_seconds": round(time.time() - started, 6),
        "mean_limits_watts": mean_limits,
        "peak_limits_watts": peak_limits,
    }
    atomic_write(args.output, payload)
    print(json.dumps(payload, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
