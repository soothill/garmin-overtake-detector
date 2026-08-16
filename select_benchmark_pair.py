#!/usr/bin/env python3
"""Select and record one representative front/rear benchmark pair."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def probe(path: Path, ffprobe: str) -> dict:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height:format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    stream = payload["streams"][0]
    media = payload["format"]
    return {
        "path": str(path.resolve()),
        "duration_seconds": float(media["duration"]),
        "size_bytes": int(media.get("size", path.stat().st_size)),
        "codec": stream["codec_name"],
        "width": int(stream["width"]),
        "height": int(stream["height"]),
    }


def select_pair(
    front: list[dict], rear: list[dict], target_seconds: float
) -> tuple[dict, dict]:
    choices = [
        (
            abs(
                (first["duration_seconds"] + second["duration_seconds"]) / 2
                - target_seconds
            ),
            abs(first["duration_seconds"] - second["duration_seconds"]),
            first["path"],
            second["path"],
            first,
            second,
        )
        for first in front
        for second in rear
    ]
    if not choices:
        raise ValueError("no front/rear combinations are available")
    choice = min(choices)
    return choice[-2], choice[-1]


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
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--date")
    parser.add_argument("--target-minutes", type=float, default=90.0)
    parser.add_argument("--front-directory", default="varia-vue")
    parser.add_argument("--rear-directory", default="rct715")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--allow-multiple-per-camera", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not Path(args.ffprobe).is_file() and shutil.which(args.ffprobe) is None:
        raise SystemExit(
            f"FFprobe is unavailable: {args.ffprobe}; install it or use "
            "--ffprobe ./scripts/container-ffprobe.sh with the platform roots set"
        )
    root = args.source_root.resolve()
    front_paths = sorted((root / args.front_directory).glob("*/*.mp4"))
    rear_paths = sorted((root / args.rear_directory).glob("*/*.mp4"))
    common_dates = sorted(
        {path.parent.name for path in front_paths}
        & {path.parent.name for path in rear_paths}
    )
    if args.date:
        if args.date not in common_dates:
            raise SystemExit(f"no paired camera date is available for {args.date}")
        dates = [args.date]
    else:
        dates = common_dates
    if not dates:
        raise SystemExit("no dates contain both front and rear MP4 files")

    candidates = []
    for date in dates:
        dated_front_paths = [path for path in front_paths if path.parent.name == date]
        dated_rear_paths = [path for path in rear_paths if path.parent.name == date]
        if not args.allow_multiple_per_camera and (
            len(dated_front_paths) != 1 or len(dated_rear_paths) != 1
        ):
            if args.date:
                raise SystemExit(
                    f"{date} has {len(dated_front_paths)} front and "
                    f"{len(dated_rear_paths)} rear files; select paths manually or "
                    "use --allow-multiple-per-camera after confirming they overlap"
                )
            continue
        fronts = [probe(path, args.ffprobe) for path in dated_front_paths]
        rears = [probe(path, args.ffprobe) for path in dated_rear_paths]
        first, second = select_pair(fronts, rears, args.target_minutes * 60.0)
        candidates.append((date, first, second))
    if not candidates:
        raise SystemExit("no unambiguous paired date is available")
    date, front, rear = min(
        candidates,
        key=lambda item: (
            abs(
                (item[1]["duration_seconds"] + item[2]["duration_seconds"]) / 2
                - args.target_minutes * 60.0
            ),
            abs(item[1]["duration_seconds"] - item[2]["duration_seconds"]),
            item[0],
        ),
    )
    total_seconds = front["duration_seconds"] + rear["duration_seconds"]
    payload = {
        "schema_version": 1,
        "selection_method": "closest_mean_duration_then_camera_duration_match",
        "date": date,
        "target_minutes_per_camera": args.target_minutes,
        "front": front,
        "rear": rear,
        "total_source_seconds": round(total_seconds, 6),
        "total_source_hours": round(total_seconds / 3600.0, 6),
    }
    atomic_write(args.output, payload)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
