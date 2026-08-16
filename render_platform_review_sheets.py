#!/usr/bin/env python3
"""Render three-timepoint contact sheets for blind platform-event review."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path

from summarize_platform_results import named_path


def render_strip(
    ffmpeg: str,
    source: Path,
    output: Path,
    review_id: str,
    peak_time: float,
    spacing: float,
    frame_width: int,
    frame_height: int,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, peak_time - spacing)
    duration = spacing * 2.0 + 0.5
    filter_graph = (
        f"fps=1/{spacing},scale={frame_width}:{frame_height}:flags=fast_bilinear,"
        "tile=3x1:nb_frames=3:padding=2:margin=0,"
        f"drawtext=text='{review_id}':x=5:y=5:fontsize={max(18, frame_height // 8)}:"
        "fontcolor=white:box=1:boxcolor=black@0.65"
    )
    subprocess.run(
        [
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
            f"{duration:.3f}",
            "-vf",
            filter_graph,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(output),
        ],
        check=True,
    )


def render_sheet(
    ffmpeg: str,
    strips: list[Path],
    output: Path,
    columns: int,
    strip_width: int,
    strip_height: int,
) -> None:
    rows = (len(strips) + columns - 1) // columns
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for strip in strips:
        command.extend(["-i", str(strip)])
    layout = []
    for index in range(len(strips)):
        column = index % columns
        row = index // columns
        layout.append(f"{column * strip_width}_{row * strip_height}")
    command.extend(
        [
            "-filter_complex",
            f"xstack=inputs={len(strips)}:layout={'|'.join(layout)}:fill=black",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output),
        ]
    )
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--source", action="append", required=True, type=named_path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--selection-reason", choices=("disagreement", "all_three_sample"))
    parser.add_argument("--events-per-sheet", type=int, default=16)
    parser.add_argument("--columns", type=int, default=2)
    parser.add_argument("--timepoint-spacing", type=float, default=3.0)
    parser.add_argument("--frame-width", type=int, default=480)
    parser.add_argument("--frame-height", type=int, default=270)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    sources = dict(args.source)
    if set(sources) != {"front", "rear"}:
        raise SystemExit("front and rear --source entries are required")
    with args.labels.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if args.selection_reason:
        rows = [row for row in rows if row["selection_reason"] == args.selection_reason]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    strips = []
    for row in rows:
        strip = args.output_dir / "strips" / f"{row['review_id']}.jpg"
        render_strip(
            args.ffmpeg,
            sources[row["camera"]],
            strip,
            row["review_id"],
            float(row["representative_time"]),
            args.timepoint_spacing,
            args.frame_width,
            args.frame_height,
        )
        strips.append(strip)

    sheets = []
    for start in range(0, len(strips), args.events_per_sheet):
        group = strips[start : start + args.events_per_sheet]
        sheet = args.output_dir / f"review-sheet-{len(sheets) + 1:03d}.jpg"
        render_sheet(
            args.ffmpeg,
            group,
            sheet,
            args.columns,
            args.frame_width * 3 + 4,
            args.frame_height,
        )
        sheets.append(sheet)
    payload = {
        "schema_version": 1,
        "review_events": len(rows),
        "sheets": [str(path) for path in sheets],
        "timepoints": [
            -args.timepoint_spacing,
            0.0,
            args.timepoint_spacing,
        ],
    }
    (args.output_dir / "sheets.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
