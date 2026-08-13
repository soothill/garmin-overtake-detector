#!/usr/bin/env python3
"""Validate one date of combined rear/front Plex clips."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    errors: list[str] = []
    result_dir = args.result_dir.resolve()
    root = args.output_root.resolve()
    try:
        result_dir.relative_to(root)
    except ValueError:
        errors.append("combined result is outside output root")
    combined_path = result_dir / "combined.json"
    progress_path = result_dir / "progress.json"
    if not combined_path.is_file() or not progress_path.is_file():
        errors.append("combined.json or progress.json is missing")
        payload = {}
        progress = {}
    else:
        try:
            payload = json.loads(combined_path.read_text(encoding="utf-8"))
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"invalid combined evidence: {error}")
            payload = {}
            progress = {}
    events = payload.get("events") or []
    skipped = payload.get("skipped_events") or []
    attempted = int(payload.get("events_attempted") or 0)
    alignment_method = payload.get("alignment_method")
    layout = payload.get("layout")
    if int(payload.get("schema_version") or 0) < 3:
        errors.append("combined evidence predates physical handoff validation")
    if alignment_method != "vehicle_handoff_clock_v2":
        errors.append("combined clips were not aligned by clock-biased vehicle handoff")
    if layout != "front-left_rear-right":
        errors.append("combined clips do not use the front-left/rear-right layout")
    calibration = payload.get("calibration") or {}
    tolerance = float(calibration.get("clock_match_tolerance_seconds") or 0)
    if tolerance <= 0 or tolerance > 2.0:
        errors.append("invalid vehicle handoff tolerance")
    if int(calibration.get("accepted_matches") or 0) < len(events):
        errors.append("accepted vehicle handoff count is inconsistent")
    if int(payload.get("combined_clips") or 0) != len(events):
        errors.append("combined clip count is inconsistent")
    if attempted != len(events) + len(skipped):
        errors.append("combined attempted event count is inconsistent")
    if attempted and not events:
        errors.append("all attempted combined events were skipped")
    if progress.get("state") != "complete":
        errors.append("combined progress is not complete")
    for event in events:
        synchronization = event.get("synchronization") or {}
        if synchronization.get("method") != "vehicle_handoff_clock_v2":
            errors.append("clip lacks vehicle handoff synchronization evidence")
        if not event.get("front_track_id"):
            errors.append("clip lacks a matching front-camera vehicle track")
        try:
            residual = abs(float(synchronization["clock_match_residual_seconds"]))
        except (KeyError, TypeError, ValueError):
            errors.append("clip has invalid vehicle handoff residual evidence")
        else:
            if residual > tolerance:
                errors.append(
                    f"vehicle handoff residual {residual:.3f}s exceeds {tolerance:.3f}s"
                )
        clip = Path(str(event.get("clip") or ""))
        if clip.parts[:2] == ("/", "output"):
            clip = root.joinpath(*clip.parts[2:])
        try:
            clip.resolve().relative_to(root)
        except ValueError:
            errors.append(f"clip is outside output root: {clip}")
            continue
        if not clip.is_file() or clip.stat().st_size <= 0:
            errors.append(f"missing or empty combined clip: {clip}")
            continue
        media = event.get("media") or {}
        if int(media.get("width") or 0) != 2560 or int(media.get("height") or 0) != 720:
            errors.append(f"unexpected combined dimensions: {clip}")
        if float(media.get("duration") or 0) < 5:
            errors.append(f"combined clip is too short: {clip}")
        if clip.stat().st_uid != os.getuid():
            errors.append(f"wrong owner for combined clip: {clip}")
    validation = {
        "valid": not errors,
        "errors": errors,
        "combined_clips": len(events),
        "alignment_method": alignment_method,
        "layout": layout,
    }
    temporary = result_dir / f".validation.json.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(validation, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, result_dir / "validation.json")
    print(json.dumps(validation, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
