#!/usr/bin/env python3
"""Recompose validated clips as front-left/rear-right without redoing detection."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Sequence

from compose_paired_events import compose_clip, probe_clip


TARGET_LAYOUT = "front-left_rear-right"
OLD_LAYOUT = "rear-left_front-right"


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def heartbeat(path: Path, state: str, **values: object) -> None:
    atomic_json(path, {"state": state, "updated_epoch": time.time(), **values})


def host_path(path: str, output_root: Path) -> Path:
    candidate = Path(path)
    if candidate.parts[:2] == ("/", "output"):
        return output_root.joinpath(*candidate.parts[2:])
    return candidate


def valid_media(media: dict) -> bool:
    return (
        int(media.get("width", 0)) == 2560
        and int(media.get("height", 0)) == 720
        and float(media.get("duration", 0.0)) >= 5.0
        and int(media.get("size", 0)) > 0
    )


def collect_dates(batch_root: Path, output_root: Path, stage_root: Path) -> list[dict]:
    dates: list[dict] = []
    for combined_path in sorted((batch_root / "combined").glob("*/combined.json")):
        result_dir = combined_path.parent
        payload = load_json(combined_path)
        validation_path = result_dir / "validation.json"
        validation = load_json(validation_path)
        if validation.get("valid") is not True:
            raise RuntimeError(f"input combined result is not valid: {result_dir}")
        if payload.get("alignment_method") != "vehicle_handoff_clock_v2":
            raise RuntimeError(f"input alignment is not vehicle_handoff_clock_v2: {result_dir}")
        layout = payload.get("layout")
        if layout not in (OLD_LAYOUT, TARGET_LAYOUT):
            raise RuntimeError(f"unsupported input layout {layout!r}: {result_dir}")
        date = str(payload["date"])
        stage_dir = stage_root / date
        stage_clip_dir = stage_dir / "clips"
        stage_clip_dir.mkdir(parents=True, exist_ok=True)
        tasks: list[dict] = []
        for event in payload.get("events") or []:
            final_clip = host_path(str(event["clip"]), output_root)
            tasks.append(
                {
                    "date": date,
                    "rear_source": Path(str(payload["rear_source"])),
                    "front_source": Path(str(payload["front_source"])),
                    "rear_start": float(event["rear_start"]),
                    "front_start": float(event["front_start"]),
                    "duration": float(event["duration"]),
                    "stage_clip": stage_clip_dir / final_clip.name,
                    "final_clip": final_clip,
                    "event": event,
                }
            )
        dates.append(
            {
                "date": date,
                "result_dir": result_dir,
                "combined_path": combined_path,
                "payload": payload,
                "stage_dir": stage_dir,
                "stage_clip_dir": stage_clip_dir,
                "tasks": tasks,
            }
        )
    return dates


def render_task(task: dict) -> dict:
    output = task["stage_clip"]
    if output.is_file() and output.stat().st_size > 0:
        try:
            media = probe_clip(output)
        except (KeyError, OSError, subprocess.SubprocessError, ValueError):
            output.unlink(missing_ok=True)
        else:
            if valid_media(media):
                return {**task, "media": media, "reused": True}
            output.unlink(missing_ok=True)
    compose_clip(
        task["rear_source"],
        task["front_source"],
        output,
        task["rear_start"],
        task["front_start"],
        task["duration"],
        1280,
        720,
    )
    media = probe_clip(output)
    if not valid_media(media):
        raise RuntimeError(f"invalid recomposed clip {output}: {media}")
    return {**task, "media": media, "reused": False}


def validate_stage(dates: list[dict], completed: list[dict], stage_root: Path) -> dict:
    by_date: dict[str, list[dict]] = {item["date"]: [] for item in dates}
    for row in completed:
        by_date[row["date"]].append(row)
    errors: list[str] = []
    expected = sum(len(item["tasks"]) for item in dates)
    if len(completed) != expected:
        errors.append(f"completed {len(completed)} of {expected} clips")
    report_dates: list[dict] = []
    for item in dates:
        rows = sorted(by_date[item["date"]], key=lambda row: str(row["stage_clip"]))
        if len(rows) != len(item["tasks"]):
            errors.append(f"{item['date']}: clip count mismatch")
        events: list[dict] = []
        for row in rows:
            clip = row["stage_clip"]
            if not clip.is_file() or clip.stat().st_size <= 0:
                errors.append(f"missing stage clip: {clip}")
            elif clip.stat().st_uid != os.getuid():
                errors.append(f"wrong owner: {clip}")
            if not valid_media(row["media"]):
                errors.append(f"invalid stage media: {clip}")
            events.append(
                {
                    "filename": clip.name,
                    "stage_clip": str(clip),
                    "final_clip": str(row["final_clip"]),
                    "media": row["media"],
                    "reused": row["reused"],
                }
            )
        date_report = {
            "date": item["date"],
            "layout": TARGET_LAYOUT,
            "clips": len(events),
            "events": events,
        }
        atomic_json(item["stage_dir"] / "recompose.json", date_report)
        atomic_json(
            item["stage_dir"] / "validation.json",
            {
                "valid": not any(error.startswith(f"{item['date']}:") for error in errors),
                "layout": TARGET_LAYOUT,
                "clips": len(events),
            },
        )
        report_dates.append(date_report)
    report = {
        "schema_version": 1,
        "layout": TARGET_LAYOUT,
        "dates": len(dates),
        "clips": len(completed),
        "errors": errors,
        "results": report_dates,
    }
    atomic_json(stage_root / "recompose.json", report)
    atomic_json(
        stage_root / "validation.json",
        {"valid": not errors, "layout": TARGET_LAYOUT, "dates": len(dates), "clips": len(completed), "errors": errors},
    )
    if errors:
        raise RuntimeError("stage validation failed: " + "; ".join(errors[:10]))
    return report


def run_combined_validator(result_dir: Path, output_root: Path) -> None:
    result = subprocess.run(
        [
            "python3",
            "/app/validate_combined_result.py",
            "--result-dir",
            str(result_dir),
            "--output-root",
            str(output_root),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"combined validation failed for {result_dir}: {result.stdout} {result.stderr}"
        )


def promote_dates(
    dates: list[dict],
    report: dict,
    batch_root: Path,
    output_root: Path,
    heartbeat_path: Path,
) -> None:
    report_by_date = {item["date"]: item for item in report["results"]}
    archive_root = batch_root / "layout-history" / OLD_LAYOUT
    for index, item in enumerate(dates, start=1):
        date = item["date"]
        result_dir = item["result_dir"]
        final_clip_dir = result_dir / "clips"
        stage_clip_dir = item["stage_clip_dir"]
        archive_dir = archive_root / date
        archive_clip_dir = archive_dir / "clips"
        existing_payload = load_json(item["combined_path"])
        existing_validation = load_json(result_dir / "validation.json")
        if (
            existing_payload.get("layout") == TARGET_LAYOUT
            and existing_validation.get("valid") is True
            and existing_validation.get("layout") == TARGET_LAYOUT
        ):
            continue

        archive_dir.mkdir(parents=True, exist_ok=True)
        for evidence_name in ("combined.json", "validation.json", "progress.json", "events.csv"):
            source = result_dir / evidence_name
            destination = archive_dir / evidence_name
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
        atomic_json(
            result_dir / "validation.json",
            {
                "valid": False,
                "alignment_method": "vehicle_handoff_clock_v2",
                "layout": TARGET_LAYOUT,
                "errors": ["front-left/rear-right promotion in progress"],
            },
        )

        if archive_clip_dir.exists() and stage_clip_dir.exists() and final_clip_dir.exists():
            raise RuntimeError(f"ambiguous promotion state for {date}")
        if not archive_clip_dir.exists() and final_clip_dir.exists():
            os.rename(final_clip_dir, archive_clip_dir)
        if stage_clip_dir.exists() and not final_clip_dir.exists():
            os.rename(stage_clip_dir, final_clip_dir)
        if not final_clip_dir.is_dir():
            raise RuntimeError(f"new clip directory is missing for {date}")

        media_by_name = {
            event["filename"]: event["media"]
            for event in report_by_date[date]["events"]
        }
        payload = existing_payload
        payload["layout"] = TARGET_LAYOUT
        payload["layout_schema_version"] = 2
        payload["layout_recomposed_epoch"] = time.time()
        for event in payload.get("events") or []:
            final_clip = host_path(str(event["clip"]), output_root)
            event["media"] = media_by_name[final_clip.name]
        atomic_json(item["combined_path"], payload)
        progress = load_json(result_dir / "progress.json")
        progress.update(
            {
                "state": "complete",
                "updated_epoch": time.time(),
                "phase": "front_left_rear_right_complete",
                "layout": TARGET_LAYOUT,
            }
        )
        atomic_json(result_dir / "progress.json", progress)
        run_combined_validator(result_dir, output_root)
        heartbeat(
            heartbeat_path,
            "processing",
            phase="atomic_layout_promotion",
            dates_completed=index,
            dates_total=len(dates),
            clips_total=report["clips"],
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--stage-root", required=True, type=Path)
    parser.add_argument("--heartbeat-file", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--expected-dates", type=int, default=28)
    parser.add_argument("--expected-clips", type=int, default=633)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 1 or args.workers > 16:
        raise RuntimeError("workers must be between one and sixteen")
    batch_root = args.batch_root.resolve()
    output_root = args.output_root.resolve()
    stage_root = args.stage_root.resolve()
    stage_root.mkdir(parents=True, exist_ok=True)
    dates = collect_dates(batch_root, output_root, stage_root)
    tasks = [task for item in dates for task in item["tasks"]]
    if len(dates) != args.expected_dates or len(tasks) != args.expected_clips:
        raise RuntimeError(
            f"expected {args.expected_dates} dates/{args.expected_clips} clips, "
            f"found {len(dates)} dates/{len(tasks)} clips"
        )
    heartbeat(
        args.heartbeat_file,
        "processing",
        phase="front_left_rear_right_recompose",
        clips_completed=0,
        clips_total=len(tasks),
    )
    completed: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(render_task, task): task for task in tasks}
        for future in concurrent.futures.as_completed(futures):
            completed.append(future.result())
            heartbeat(
                args.heartbeat_file,
                "processing",
                phase="front_left_rear_right_recompose",
                clips_completed=len(completed),
                clips_total=len(tasks),
                reused_clips=sum(row["reused"] for row in completed),
            )
    report = validate_stage(dates, completed, stage_root)
    heartbeat(
        args.heartbeat_file,
        "processing",
        phase="stage_validated",
        clips_completed=len(completed),
        clips_total=len(tasks),
    )
    promote_dates(dates, report, batch_root, output_root, args.heartbeat_file)
    heartbeat(
        args.heartbeat_file,
        "complete",
        phase="complete",
        dates_completed=len(dates),
        clips_completed=len(completed),
        layout=TARGET_LAYOUT,
    )
    print(json.dumps({"valid": True, "layout": TARGET_LAYOUT, "dates": len(dates), "clips": len(completed)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
