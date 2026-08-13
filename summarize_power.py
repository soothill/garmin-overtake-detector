#!/usr/bin/env python3
"""Summarize AMD SMI samples collected around one pipeline run."""

from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import statistics
from pathlib import Path


NUMERIC_COLUMNS = (
    "gfx_activity",
    "apu_average_gfx_activity",
    "apu_average_vcn_activity",
    "apu_average_dram_reads",
    "apu_average_dram_writes",
    "apu_average_ipu_reads",
    "apu_average_ipu_writes",
    "socket_power",
    "apu_average_socket_power",
    "apu_average_gfx_power",
    "apu_average_ipu_power",
    "apu_average_apu_power",
    "apu_average_all_core_power",
    "apu_average_sys_power",
)

ARRAY_COLUMNS = ("apu_average_ipu_activity",)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--start", required=True, type=float)
    parser.add_argument("--end", required=True, type=float)
    parser.add_argument("--run-json", type=Path)
    parser.add_argument("--inference-json", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    index = round(0.95 * (len(ordered) - 1))
    return ordered[max(0, min(len(ordered) - 1, index))]


def main() -> int:
    args = parse_args()
    with args.csv.open(newline="", encoding="utf-8") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if args.start <= float(row["timestamp"]) <= args.end
        ]
    summary: dict[str, object] = {
        "start_epoch": args.start,
        "end_epoch": args.end,
        "elapsed_seconds": round(args.end - args.start, 3),
        "samples": len(rows),
        "telemetry_available": bool(rows),
    }
    if not rows:
        summary["telemetry_warning"] = "no AMD SMI samples overlap the pipeline run"
    metrics: dict[str, dict[str, float]] = {}
    for column in NUMERIC_COLUMNS:
        values = [
            float(row[column])
            for row in rows
            if row.get(column) not in (None, "", "N/A")
        ]
        if values:
            metrics[column] = {
                "mean": round(statistics.fmean(values), 3),
                "min": round(min(values), 3),
                "max": round(max(values), 3),
                "p95": round(percentile_95(values), 3),
            }
    for column in ARRAY_COLUMNS:
        values = []
        for row in rows:
            raw = row.get(column)
            if raw in (None, "", "N/A"):
                continue
            items = [float(item) for item in ast.literal_eval(raw) if item != "N/A"]
            if items:
                values.append(statistics.fmean(items))
        if values:
            metrics[column] = {
                "mean": round(statistics.fmean(values), 3),
                "min": round(min(values), 3),
                "max": round(max(values), 3),
                "p95": round(percentile_95(values), 3),
            }
    summary["metrics"] = metrics

    elapsed_hours = (args.end - args.start) / 3600.0
    if "apu_average_socket_power" in metrics:
        summary["estimated_socket_energy_wh"] = round(
            metrics["apu_average_socket_power"]["mean"] * elapsed_hours, 4
        )

    if args.run_json and args.run_json.exists():
        run = json.loads(args.run_json.read_text(encoding="utf-8"))
        summary["run"] = {
            key: run.get(key)
            for key in (
                "source",
                "device_name",
                "decode",
                "sample_fps",
                "processed_source_seconds",
                "wall_seconds",
                "realtime_factor",
                "candidate_events",
            )
        }
        source_hours = float(run.get("processed_source_seconds") or 0) / 3600.0
        if source_hours and "estimated_socket_energy_wh" in summary:
            summary["estimated_socket_wh_per_source_hour"] = round(
                float(summary["estimated_socket_energy_wh"]) / source_hours, 4
            )

    if args.inference_json and args.inference_json.exists():
        inference = json.loads(args.inference_json.read_text(encoding="utf-8"))
        summary["inference"] = inference
        inference_fps = float(inference.get("fps") or 0)
        if inference_fps and "apu_average_socket_power" in metrics:
            summary["estimated_socket_joules_per_inference"] = round(
                metrics["apu_average_socket_power"]["mean"] / inference_fps, 6
            )

    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
