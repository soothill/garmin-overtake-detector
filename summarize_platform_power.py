#!/usr/bin/env python3
"""Summarize timestamped platform power samples for one benchmark interval."""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
from pathlib import Path
from typing import Iterable


AMD_COLUMNS = {
    "system_package": "apu_average_socket_power",
    "gpu": "apu_average_gfx_power",
    "npu": "apu_average_ipu_power",
}


def numeric_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def values_between(
    rows: Iterable[dict[str, str]], column: str, start: float, end: float
) -> list[float]:
    values = []
    for row in rows:
        try:
            timestamp = float(row["timestamp"])
            value = float(row[column])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= timestamp <= end:
            values.append(value)
    return values


def metric_summary(
    rows: list[dict[str, str]],
    column: str,
    run_start: float,
    run_end: float,
    idle_start: float | None,
    idle_end: float | None,
) -> dict | None:
    run_values = values_between(rows, column, run_start, run_end)
    if not run_values:
        return None
    run_seconds = run_end - run_start
    mean_watts = statistics.fmean(run_values)
    result = {
        "samples": len(run_values),
        "mean_watts": round(mean_watts, 6),
        "minimum_watts": round(min(run_values), 6),
        "maximum_watts": round(max(run_values), 6),
        "gross_energy_wh": round(mean_watts * run_seconds / 3600.0, 6),
    }
    if idle_start is not None and idle_end is not None:
        idle_values = values_between(rows, column, idle_start, idle_end)
        if idle_values:
            idle_watts = statistics.fmean(idle_values)
            result.update(
                {
                    "idle_samples": len(idle_values),
                    "idle_mean_watts": round(idle_watts, 6),
                    "incremental_mean_watts": round(
                        max(0.0, mean_watts - idle_watts), 6
                    ),
                    "incremental_energy_wh": round(
                        max(0.0, mean_watts - idle_watts) * run_seconds / 3600.0, 6
                    ),
                }
            )
    return result


def summarize(
    rows: list[dict[str, str]],
    input_format: str,
    run_start: float,
    run_end: float,
    idle_start: float | None = None,
    idle_end: float | None = None,
    external_scope: str = "whole_system_wall",
) -> dict:
    if run_end <= run_start:
        raise ValueError("run end must be later than run start")
    columns = (
        AMD_COLUMNS if input_format == "amd-smi" else {external_scope: "power_watts"}
    )
    metrics = {
        scope: metric
        for scope, column in columns.items()
        if (
            metric := metric_summary(
                rows, column, run_start, run_end, idle_start, idle_end
            )
        )
        is not None
    }
    timestamps = []
    for row in rows:
        try:
            timestamp = float(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if run_start <= timestamp <= run_end:
            timestamps.append(timestamp)
    run_seconds = run_end - run_start
    observed_span = max(timestamps) - min(timestamps) if len(timestamps) > 1 else 0.0
    return {
        "schema_version": 1,
        "available": bool(metrics),
        "input_format": input_format,
        "run_start_epoch": run_start,
        "run_end_epoch": run_end,
        "run_seconds": round(run_seconds, 6),
        "time_coverage_fraction": round(min(1.0, observed_span / run_seconds), 6),
        "metrics": metrics,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("amd-smi", "external"), required=True)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--run-start", required=True, type=float)
    parser.add_argument("--run-end", required=True, type=float)
    parser.add_argument("--idle-start", type=float)
    parser.add_argument("--idle-end", type=float)
    parser.add_argument("--external-scope", default="whole_system_wall")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = summarize(
        numeric_rows(args.csv),
        args.format,
        args.run_start,
        args.run_end,
        args.idle_start,
        args.idle_end,
        args.external_scope,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
