#!/usr/bin/env python3
"""Create a blind review set for cross-platform event disagreements."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from summarize_platform_results import event_records, named_path


PLATFORMS = ("gpu", "npu", "hailo")


@dataclass(frozen=True)
class EventNode:
    platform: str
    camera: str
    index: int
    event: dict

    @property
    def peak_time(self) -> float:
        return float(self.event["peak_time"])


def union_find_components(nodes: list[EventNode], tolerance: float) -> list[list[EventNode]]:
    parent = list(range(len(nodes)))
    component_platforms = [{node.platform} for node in nodes]

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return
        if component_platforms[first_root] & component_platforms[second_root]:
            return
        parent[second_root] = first_root
        component_platforms[first_root] |= component_platforms[second_root]

    possible = sorted(
        (abs(first_node.peak_time - second_node.peak_time), first, second)
        for first, first_node in enumerate(nodes)
        for second, second_node in enumerate(nodes[first + 1 :], start=first + 1)
        if first_node.platform != second_node.platform
        and abs(first_node.peak_time - second_node.peak_time) <= tolerance
    )
    for _, first, second in possible:
        union(first, second)

    grouped: dict[int, list[EventNode]] = {}
    for index, node in enumerate(nodes):
        grouped.setdefault(find(index), []).append(node)
    return sorted(grouped.values(), key=lambda values: min(item.peak_time for item in values))


def evenly_sample(values: list[list[EventNode]], count: int) -> list[list[EventNode]]:
    if count <= 0 or not values:
        return []
    if len(values) <= count:
        return values
    if count == 1:
        return [values[len(values) // 2]]
    positions = [round(index * (len(values) - 1) / (count - 1)) for index in range(count)]
    return [values[index] for index in positions]


def extract_review_clip(
    ffmpeg: str,
    source: Path,
    output: Path,
    peak_time: float,
    context: float,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, peak_time - context)
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{context * 2.0:.3f}",
        "-an",
        "-vf",
        "scale=960:-2:flags=fast_bilinear",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "26",
        "-movflags",
        "+faststart",
        str(output),
    ]
    subprocess.run(command, check=True)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", action="append", required=True, type=named_path)
    parser.add_argument("--source", action="append", default=[], type=named_path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--event-tolerance", type=float, default=2.0)
    parser.add_argument("--all-three-sample", type=int, default=20)
    parser.add_argument("--extract-clips", action="store_true")
    parser.add_argument("--clip-context", type=float, default=6.0)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    result_paths = dict(args.result)
    missing_platforms = set(PLATFORMS) - set(result_paths)
    if missing_platforms:
        raise SystemExit("missing platform results: " + ", ".join(sorted(missing_platforms)))
    sources = dict(args.source)
    if args.extract_clips and set(sources) != {"front", "rear"}:
        raise SystemExit("--extract-clips requires front and rear --source entries")

    records = {name: event_records(path) for name, path in result_paths.items()}
    selected: list[tuple[str, list[EventNode], str]] = []
    component_evidence = []
    for camera in ("front", "rear"):
        nodes = [
            EventNode(platform, camera, index, event)
            for platform in PLATFORMS
            for index, event in enumerate(records[platform][camera])
        ]
        components = union_find_components(nodes, args.event_tolerance)
        disagreements = []
        unanimous = []
        for component in components:
            platforms = {item.platform for item in component}
            (unanimous if platforms == set(PLATFORMS) else disagreements).append(component)
            component_evidence.append(
                {
                    "camera": camera,
                    "platforms": sorted(platforms),
                    "representative_time": median(item.peak_time for item in component),
                    "events": [
                        {"platform": item.platform, "index": item.index, **item.event}
                        for item in component
                    ],
                }
            )
        selected.extend((camera, item, "disagreement") for item in disagreements)
        selected.extend(
            (camera, item, "all_three_sample")
            for item in evenly_sample(unanimous, args.all_three_sample)
        )

    selected.sort(key=lambda item: (item[0], median(node.peak_time for node in item[1])))
    rows = []
    for number, (camera, component, reason) in enumerate(selected, start=1):
        representative_time = float(median(item.peak_time for item in component))
        review_id = f"{camera}-{number:04d}-{representative_time:010.3f}"
        clip = args.output_dir / "clips" / f"{review_id}.mp4"
        by_platform: dict[str, list[EventNode]] = {platform: [] for platform in PLATFORMS}
        for item in component:
            by_platform[item.platform].append(item)
        if args.extract_clips:
            extract_review_clip(
                args.ffmpeg, sources[camera], clip, representative_time, args.clip_context
            )
        rows.append(
            {
                "review_id": review_id,
                "camera": camera,
                "representative_time": f"{representative_time:.3f}",
                "selection_reason": reason,
                "platforms": "+".join(p for p in PLATFORMS if by_platform[p]),
                "gpu_times": ";".join(f"{v.peak_time:.3f}" for v in by_platform["gpu"]),
                "npu_times": ";".join(f"{v.peak_time:.3f}" for v in by_platform["npu"]),
                "hailo_times": ";".join(f"{v.peak_time:.3f}" for v in by_platform["hailo"]),
                "gpu_max_confidences": ";".join(
                    str(v.event.get("max_confidence", "")) for v in by_platform["gpu"]
                ),
                "npu_max_confidences": ";".join(
                    str(v.event.get("max_confidence", "")) for v in by_platform["npu"]
                ),
                "hailo_max_confidences": ";".join(
                    str(v.event.get("max_confidence", "")) for v in by_platform["hailo"]
                ),
                "component_ambiguous": any(len(v) > 1 for v in by_platform.values()),
                "clip": str(clip) if args.extract_clips else "",
                "vehicle_present": "",
                "overtake_pass": "",
                "review_notes": "",
            }
        )

    columns = list(rows[0]) if rows else []
    csv_path = args.output_dir / "labels.csv"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    temporary_csv = args.output_dir / ".labels.csv.tmp"
    with temporary_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary_csv, csv_path)
    atomic_text(
        args.output_dir / "components.json",
        json.dumps(
            {
                "schema_version": 1,
                "event_tolerance_seconds": args.event_tolerance,
                "result_paths": {name: str(path) for name, path in result_paths.items()},
                "components": component_evidence,
            },
            indent=2,
        )
        + "\n",
    )
    summary = {
        "review_rows": len(rows),
        "disagreements": sum(row["selection_reason"] == "disagreement" for row in rows),
        "all_three_sample": sum(row["selection_reason"] == "all_three_sample" for row in rows),
        "labels": str(csv_path),
    }
    atomic_text(args.output_dir / "review-summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
