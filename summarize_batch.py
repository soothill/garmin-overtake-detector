#!/usr/bin/env python3
"""Summarize a resumable collection of per-video GPU benchmark runs."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from collections import defaultdict
from pathlib import Path


CSV_COLUMNS = (
    "status",
    "camera",
    "date",
    "source",
    "source_hours",
    "wall_minutes",
    "realtime_factor",
    "minutes_per_source_hour",
    "candidate_events",
    "power_telemetry_available",
    "mean_socket_watts",
    "socket_wh_per_source_hour",
    "result_dir",
    "benchmark_dir",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-csv", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    return parser.parse_args()


def rounded(value: float | None, places: int = 4) -> float | None:
    return round(value, places) if value is not None else None


def summarize_group(rows: list[dict]) -> dict:
    completed = [row for row in rows if row["status"] == "completed"]
    expected_source_seconds = sum(float(row["expected_source_seconds"]) for row in rows)
    source_seconds = sum(float(row.get("processed_source_seconds") or 0) for row in completed)
    wall_seconds = sum(float(row.get("wall_seconds") or 0) for row in completed)
    power_rows = [row for row in completed if row.get("power_telemetry_available")]
    power_source_seconds = sum(
        float(row.get("processed_source_seconds") or 0) for row in power_rows
    )
    energy_wh = sum(float(row.get("socket_energy_wh") or 0) for row in power_rows)
    realtime_factor = source_seconds / wall_seconds if wall_seconds else None
    remaining_source_seconds = max(0.0, expected_source_seconds - source_seconds)
    estimated_remaining_wall_seconds = (
        remaining_source_seconds / realtime_factor if realtime_factor else None
    )
    return {
        "expected_files": len(rows),
        "completed_files": len(completed),
        "expected_source_hours": rounded(expected_source_seconds / 3600.0, 3),
        "processed_source_hours": rounded(source_seconds / 3600.0, 3),
        "wall_hours": rounded(wall_seconds / 3600.0, 3),
        "aggregate_realtime_factor": rounded(realtime_factor, 3),
        "minutes_per_source_hour": rounded(
            60.0 / realtime_factor if realtime_factor else None, 3
        ),
        "estimated_remaining_wall_hours": rounded(
            estimated_remaining_wall_seconds / 3600.0
            if estimated_remaining_wall_seconds is not None
            else None,
            3,
        ),
        "candidate_events": sum(int(row.get("candidate_events") or 0) for row in completed),
        "power_telemetry_files": len(power_rows),
        "power_telemetry_source_hours": rounded(power_source_seconds / 3600.0, 3),
        "estimated_socket_energy_wh": rounded(energy_wh, 3),
        "socket_wh_per_source_hour": rounded(
            energy_wh / (power_source_seconds / 3600.0)
            if power_source_seconds
            else None,
            3,
        ),
    }


def main() -> int:
    args = parse_args()
    with args.manifest.open(newline="", encoding="utf-8") as handle:
        manifest = list(csv.DictReader(handle, delimiter="\t"))

    results: list[dict] = []
    for item in manifest:
        result_dir = Path(item["result_dir"])
        benchmark_dir = Path(item["benchmark_dir"])
        run_path = result_dir / "run.json"
        benchmark_path = benchmark_dir / "benchmark.json"
        validation_path = result_dir / "validation.json"
        row: dict[str, object] = {
            "status": "pending",
            "camera": item["camera"],
            "date": item["date"],
            "source": item["source"],
            "expected_source_seconds": float(item["duration_seconds"]),
            "result_dir": str(result_dir),
            "benchmark_dir": str(benchmark_dir),
        }
        validation = {}
        if validation_path.exists():
            try:
                validation = json.loads(validation_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                validation = {}
        if run_path.exists() and benchmark_path.exists() and validation.get("valid") is True:
            run = json.loads(run_path.read_text(encoding="utf-8"))
            benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
            source_seconds = float(run["processed_source_seconds"])
            wall_seconds = float(run["wall_seconds"])
            realtime_factor = source_seconds / wall_seconds if wall_seconds else None
            metrics = benchmark.get("metrics", {})
            socket_metric = metrics.get("apu_average_socket_power", {})
            telemetry_available = bool(
                benchmark.get("telemetry_available", benchmark.get("samples", 0))
                and socket_metric
                and benchmark.get("estimated_socket_energy_wh") is not None
            )
            row.update(
                {
                    "status": "completed",
                    "processed_source_seconds": source_seconds,
                    "wall_seconds": wall_seconds,
                    "source_hours": rounded(source_seconds / 3600.0, 4),
                    "wall_minutes": rounded(wall_seconds / 60.0, 3),
                    "realtime_factor": rounded(realtime_factor, 3),
                    "minutes_per_source_hour": rounded(
                        60.0 / realtime_factor if realtime_factor else None, 3
                    ),
                    "candidate_events": int(run.get("candidate_events") or 0),
                    "power_telemetry_available": telemetry_available,
                    "mean_socket_watts": socket_metric.get("mean"),
                    "socket_energy_wh": benchmark.get("estimated_socket_energy_wh"),
                    "socket_wh_per_source_hour": benchmark.get(
                        "estimated_socket_wh_per_source_hour"
                    ),
                }
            )
        elif run_path.exists() and benchmark_path.exists():
            row["status"] = "awaiting_validation"
        elif run_path.exists():
            row["status"] = "awaiting_benchmark"
        results.append(row)

    by_camera: dict[str, list[dict]] = defaultdict(list)
    for row in results:
        by_camera[str(row["camera"])].append(row)
    payload = {
        "overall": summarize_group(results),
        "by_camera": {
            camera: summarize_group(camera_rows)
            for camera, camera_rows in sorted(by_camera.items())
        },
        "files": results,
    }

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(results)
    for path, contents in (
        (args.output_csv, csv_buffer.getvalue()),
        (args.output_json, json.dumps(payload, indent=2) + "\n"),
    ):
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(contents, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    print(json.dumps(payload["overall"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
