#!/usr/bin/env python3
"""Create a unique-event report from an existing tracks.jsonl file."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from overtake_pipeline import deduplicate_candidates


COLUMNS = (
    "track_id",
    "class_name",
    "peak_time",
    "first_seen",
    "last_seen",
    "side",
    "max_confidence",
    "clip",
    "paired_clip",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tracks", required=True, type=Path)
    parser.add_argument("--output-csv", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    args = parser.parse_args()

    summaries = [
        json.loads(line)
        for line in args.tracks.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    deduplicated = deduplicate_candidates(summaries)
    events = [item for item in deduplicated if item["candidate"]]

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(events)
    args.output_json.write_text(
        json.dumps({"candidate_events": len(events), "events": events}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"candidate_events": len(events)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
