#!/usr/bin/env python3
"""Explain cross-platform vehicle candidate differences and timing sensitivity."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import median

from prepare_platform_review import EventNode, PLATFORMS, union_find_components
from summarize_platform_results import event_records, named_path


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 6)


def distribution(values: list[float]) -> dict:
    return {
        "count": len(values),
        "minimum": round(min(values), 6) if values else None,
        "p10": percentile(values, 0.10),
        "median": round(median(values), 6) if values else None,
        "p90": percentile(values, 0.90),
        "maximum": round(max(values), 6) if values else None,
    }


def components_for_camera(records: dict, camera: str, tolerance: float):
    nodes = [
        EventNode(platform, camera, index, event)
        for platform in PLATFORMS
        for index, event in enumerate(records[platform][camera])
    ]
    return union_find_components(nodes, tolerance)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", action="append", required=True, type=named_path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--tolerance-sweep",
        default="0.5,1,2,3,5",
        help="comma-separated event matching tolerances in seconds",
    )
    args = parser.parse_args()
    result_paths = dict(args.result)
    missing = set(PLATFORMS) - set(result_paths)
    if missing:
        raise SystemExit("missing platform results: " + ", ".join(sorted(missing)))

    records = {platform: event_records(path) for platform, path in result_paths.items()}
    platform_summary = {}
    for platform in PLATFORMS:
        result = json.loads(result_paths[platform].read_text(encoding="utf-8"))
        camera_summary = {}
        for camera in ("front", "rear"):
            events = records[platform][camera]
            tracks_path = result_paths[platform].parent / camera / "tracks.json"
            tracks = (
                json.loads(tracks_path.read_text(encoding="utf-8"))
                if tracks_path.exists()
                else None
            )
            camera_summary[camera] = {
                "final_candidates": len(events),
                "raw_candidates": sum(
                    bool(item.get("candidate_raw")) for item in tracks
                ) if tracks is not None else None,
                "deduplicated_candidates": sum(
                    bool(item.get("candidate_raw")) and not bool(item.get("candidate"))
                    for item in tracks
                ) if tracks is not None else None,
                "completed_tracks": (
                    len(tracks)
                    if tracks is not None
                    else result["cameras"][camera].get("completed_tracks")
                ),
                "classes": dict(sorted(Counter(item.get("class_name") for item in events).items())),
                "confidence": distribution(
                    [float(item["max_confidence"]) for item in events]
                ),
                "duration_seconds": distribution(
                    [float(item["duration"]) for item in events]
                ),
                "detections_per_candidate": distribution(
                    [float(item["detections"]) for item in events]
                ),
            }
        platform_summary[platform] = camera_summary

    tolerance_summary = {}
    for tolerance in (float(value) for value in args.tolerance_sweep.split(",")):
        consensus = 0
        support = Counter()
        singles = Counter()
        component_counts = Counter()
        for camera in ("front", "rear"):
            for component in components_for_camera(records, camera, tolerance):
                platforms = {item.platform for item in component}
                key = "+".join(platform for platform in PLATFORMS if platform in platforms)
                component_counts[key] += 1
                if len(platforms) >= 2:
                    consensus += 1
                    support.update(platforms)
                else:
                    singles.update(platforms)
        tolerance_summary[str(tolerance)] = {
            "consensus_components": consensus,
            "platform_supported_components": dict(support),
            "platform_only_components": dict(singles),
            "component_patterns": dict(sorted(component_counts.items())),
        }

    payload = {
        "schema_version": 1,
        "result_paths": {name: str(path) for name, path in result_paths.items()},
        "platforms": platform_summary,
        "tolerance_sensitivity": tolerance_summary,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
