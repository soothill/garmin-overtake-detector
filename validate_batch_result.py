#!/usr/bin/env python3
"""Validate one completed Garmin pipeline result before the batch advances."""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--camera", choices=("front", "rear"), required=True)
    parser.add_argument("--duration", required=True, type=float)
    parser.add_argument("--size", required=True, type=int)
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--benchmark-dir", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    return parser.parse_args()


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def host_output_path(container_path: str, output_root: Path) -> Path | None:
    path = Path(container_path)
    if not path.is_absolute() or path.parts[:2] != ("/", "output"):
        return None
    return output_root.joinpath(*path.parts[2:])


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    errors: list[str] = []
    warnings: list[str] = []
    output_root = args.output_root.resolve()
    result_dir = args.result_dir.resolve()
    benchmark_dir = args.benchmark_dir.resolve()

    require(within(result_dir, output_root), "result directory is outside output root", errors)
    require(within(benchmark_dir, output_root), "benchmark directory is outside output root", errors)

    run_path = result_dir / "run.json"
    events_path = result_dir / "events.csv"
    tracks_path = result_dir / "tracks.jsonl"
    heartbeat_path = result_dir / "progress.json"
    benchmark_path = benchmark_dir / "benchmark.json"
    for path in (run_path, events_path, tracks_path, heartbeat_path, benchmark_path):
        require(path.is_file(), f"missing {path}", errors)
    if errors:
        payload = {"valid": False, "errors": errors, "warnings": warnings}
        result_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(result_dir / "validation.json", payload)
        print(json.dumps(payload, indent=2))
        return 1

    try:
        run = json.loads(run_path.read_text(encoding="utf-8"))
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"could not read JSON evidence: {error}")
        run = {}
        heartbeat = {}
        benchmark = {}

    require(run.get("source") == args.source, "run source does not match manifest", errors)
    require(run.get("camera") == args.camera, "run camera does not match manifest", errors)
    require(run.get("weights") == "/models/yolov8s.pt", "unexpected detector weights", errors)
    require(run.get("device") == "0", "run did not use GPU device 0", errors)
    require(run.get("decode") == "vaapi", "run did not use VAAPI decoding", errors)
    require(float(run.get("sample_fps") or 0) == 5.0, "unexpected sample rate", errors)

    source_video = run.get("source_video") or {}
    require(int(source_video.get("size") or -1) == args.size, "source size changed", errors)
    require(
        abs(float(source_video.get("duration") or 0) - args.duration) <= 0.05,
        "source duration changed",
        errors,
    )
    processed_seconds = float(run.get("processed_source_seconds") or 0)
    require(
        abs(processed_seconds - args.duration) <= 1.0,
        "processed duration does not cover the source",
        errors,
    )
    require(float(run.get("wall_seconds") or 0) > 0, "wall time is missing", errors)

    candidate_events = int(run.get("candidate_events") or 0)
    require(
        len(run.get("events") or []) == candidate_events,
        "run event count is inconsistent",
        errors,
    )
    with events_path.open(newline="", encoding="utf-8") as handle:
        event_rows = list(csv.DictReader(handle))
    require(len(event_rows) == candidate_events, "events.csv count is inconsistent", errors)
    track_lines = sum(1 for line in tracks_path.read_text(encoding="utf-8").splitlines() if line)
    require(
        track_lines == int(run.get("completed_tracks") or 0),
        "tracks.jsonl count is inconsistent",
        errors,
    )

    clips_enabled = bool(run.get("clips_enabled", True))
    for row in event_rows:
        clip_text = row.get("clip") or ""
        if not clips_enabled:
            require(not clip_text, "no-clips run unexpectedly references a clip", errors)
            continue
        clip_path = host_output_path(clip_text, output_root)
        require(clip_path is not None, f"invalid clip path {clip_text!r}", errors)
        if clip_path is not None:
            require(clip_path.is_file(), f"missing clip {clip_path}", errors)
            if clip_path.is_file():
                require(clip_path.stat().st_size > 0, f"empty clip {clip_path}", errors)

    require(heartbeat.get("state") == "complete", "pipeline heartbeat is not complete", errors)
    require(heartbeat.get("source") == args.source, "heartbeat source mismatch", errors)

    benchmark_run = benchmark.get("run") or {}
    require(benchmark_run.get("source") == args.source, "benchmark source mismatch", errors)
    require(
        abs(float(benchmark_run.get("processed_source_seconds") or 0) - processed_seconds)
        <= 0.01,
        "benchmark duration mismatch",
        errors,
    )
    if not benchmark.get("telemetry_available", benchmark.get("samples", 0)):
        warnings.append("AMD SMI power telemetry is unavailable for this file")

    expected_uid = os.getuid()
    evidence_paths = (run_path, events_path, tracks_path, heartbeat_path, benchmark_path)
    for path in evidence_paths:
        require(path.stat().st_uid == expected_uid, f"wrong owner for {path}", errors)

    payload = {
        "valid": not errors,
        "validated_epoch": time.time(),
        "source": args.source,
        "camera": args.camera,
        "duration_seconds": args.duration,
        "candidate_events": candidate_events,
        "power_telemetry_available": bool(
            benchmark.get("telemetry_available", benchmark.get("samples", 0))
        ),
        "errors": errors,
        "warnings": warnings,
    }
    atomic_json(result_dir / "validation.json", payload)
    print(json.dumps(payload, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
