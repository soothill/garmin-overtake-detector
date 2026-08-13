#!/usr/bin/env python3
"""Review skipped rear events and publish validated rear-only fallback clips."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Sequence


VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
REVIEW_METHOD = "rear_vehicle_review_v1"


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def heartbeat(path: Path | None, state: str, **values: object) -> None:
    if path:
        atomic_json(
            path,
            {"state": state, "updated_epoch": time.time(), **values},
        )


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def event_key(event: dict, prefix: str = "") -> tuple[int, float]:
    return (
        int(event[f"{prefix}track_id"]),
        round(float(event[f"{prefix}peak_time"]), 3),
    )


def all_detector_checks_pass(event: dict) -> bool:
    checks = event.get("checks") or {}
    required = (
        "detections",
        "duration",
        "peak_area",
        "area_ratio",
        "side_offset",
        "trajectory",
    )
    return event.get("candidate") is True and all(checks.get(name) is True for name in required)


def has_strong_track_evidence(event: dict) -> bool:
    return (
        all_detector_checks_pass(event)
        and int(event.get("class_id", -1)) in VEHICLE_CLASSES
        and float(event.get("max_confidence", 0.0)) >= 0.80
        and int(event.get("detections", 0)) >= 5
        and float(event.get("duration", 0.0)) >= 1.0
        and float(event.get("peak_area", 0.0)) >= 0.01
    )


def find_authoritative_rear_run(batch_root: Path, date: str, source: str) -> tuple[Path, dict]:
    matches: list[tuple[Path, dict]] = []
    for path in sorted((batch_root / "rear" / date).glob("*/run.json")):
        payload = load_json(path)
        if payload.get("source") == source:
            matches.append((path, payload))
    if len(matches) != 1:
        raise RuntimeError(
            f"{date}: expected one rear result for {source}, found {len(matches)}"
        )
    return matches[0]


def collect_skipped_events(batch_root: Path) -> list[dict]:
    collected: list[dict] = []
    combined_root = batch_root / "combined"
    for combined_path in sorted(combined_root.glob("*/combined.json")):
        combined = load_json(combined_path)
        date = str(combined["date"])
        source = str(combined["rear_source"])
        run_path, rear_run = find_authoritative_rear_run(batch_root, date, source)
        rear_by_key = {event_key(event): event for event in rear_run.get("events") or []}
        for skipped in combined.get("skipped_events") or []:
            key = event_key(skipped, "rear_")
            event = rear_by_key.get(key)
            if event is None:
                raise RuntimeError(
                    f"{date}: skipped event {key} has no authoritative detector record"
                )
            collected.append(
                {
                    "date": date,
                    "source": source,
                    "source_duration": float(rear_run["source_video"]["duration"]),
                    "run": str(run_path),
                    "event": event,
                    "original_skip_reason": skipped.get("reason"),
                }
            )
    return collected


def read_frame(source: Path, timestamp: float) -> "object":
    import cv2
    import numpy as np

    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, timestamp):.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale=1280:-2",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    frame = cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError(f"could not decode frame at {timestamp:.3f}s from {source}")
    return frame


def secondary_visual_review(
    model: "object",
    item: dict,
    thumbnails: Path,
    device: str,
) -> dict:
    import cv2

    event = item["event"]
    peak = float(event["peak_time"])
    sample_times = [max(0.0, peak - 0.6), peak, min(item["source_duration"] - 0.05, peak + 0.6)]
    frames = [read_frame(Path(item["source"]), timestamp) for timestamp in sample_times]
    results = model.predict(
        frames,
        classes=list(VEHICLE_CLASSES),
        conf=0.25,
        iou=0.50,
        imgsz=1280,
        device=device,
        verbose=False,
    )
    observations: list[dict] = []
    best_result = None
    best_rank = (-1.0, -1.0)
    for timestamp, result in zip(sample_times, results):
        detections: list[dict] = []
        boxes = result.boxes
        if boxes is not None:
            for class_id, confidence, coordinates in zip(
                boxes.cls.int().cpu().tolist(),
                boxes.conf.cpu().tolist(),
                boxes.xyxyn.cpu().tolist(),
            ):
                if class_id not in VEHICLE_CLASSES:
                    continue
                left, top, right, bottom = coordinates
                area = max(0.0, right - left) * max(0.0, bottom - top)
                if area < 0.003:
                    continue
                detections.append(
                    {
                        "class_id": class_id,
                        "class_name": VEHICLE_CLASSES[class_id],
                        "confidence": round(float(confidence), 4),
                        "area": round(area, 6),
                    }
                )
                rank = (float(confidence), area)
                if rank > best_rank:
                    best_rank = rank
                    best_result = result
        observations.append({"media_time": round(timestamp, 3), "detections": detections})

    frames_with_vehicle = sum(bool(sample["detections"]) for sample in observations)
    maximum_confidence = max(
        (
            detection["confidence"]
            for sample in observations
            for detection in sample["detections"]
        ),
        default=0.0,
    )
    confirmed = frames_with_vehicle >= 2 or maximum_confidence >= 0.60
    thumbnail = None
    if best_result is not None:
        thumbnail_path = thumbnails / item["date"] / (
            f"t{peak:010.3f}_track{int(event['track_id']):05d}.jpg"
        )
        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(thumbnail_path), best_result.plot()):
            raise RuntimeError(f"could not write {thumbnail_path}")
        thumbnail = str(thumbnail_path)
    return {
        "required": True,
        "confirmed": confirmed,
        "frames_with_vehicle": frames_with_vehicle,
        "maximum_confidence": round(maximum_confidence, 4),
        "samples": observations,
        "thumbnail": thumbnail,
    }


def probe_video(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,codec_name:format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    return {
        "duration": float(payload["format"]["duration"]),
        "size": int(payload["format"]["size"]),
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "codec": stream["codec_name"],
    }


def extract_rear_clip(item: dict, output_dir: Path, clip_pre: float, clip_post: float) -> dict:
    event = item["event"]
    peak = float(event["peak_time"])
    start = max(0.0, peak - clip_pre)
    end = min(float(item["source_duration"]), peak + clip_post)
    duration = end - start
    clip = output_dir / "clips" / item["date"] / (
        f"{item['date']}_t{peak:010.3f}_track{int(event['track_id']):05d}_"
        f"{event['class_name']}_rear-only-reviewed.mp4"
    )
    clip.parent.mkdir(parents=True, exist_ok=True)
    if not clip.is_file() or clip.stat().st_size <= 0:
        temporary = clip.with_name(f".{clip.stem}.{os.getpid()}.tmp.mp4")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{start:.3f}",
                    "-i",
                    item["source"],
                    "-t",
                    f"{duration:.3f}",
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-map_metadata",
                    "0",
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    "-movflags",
                    "+faststart",
                    str(temporary),
                ],
                check=True,
            )
            os.replace(temporary, clip)
        finally:
            temporary.unlink(missing_ok=True)
    media = probe_video(clip)
    if media["width"] != 1920 or media["height"] != 1080 or media["codec"] != "h264":
        raise RuntimeError(f"unexpected rear-only media properties: {clip}: {media}")
    if media["duration"] < 5.0 or media["duration"] > duration + 3.0:
        raise RuntimeError(f"unexpected rear-only duration: {clip}: {media['duration']}")
    return {
        "clip": str(clip),
        "clip_start": round(start, 3),
        "requested_duration": round(duration, 3),
        "media": media,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--heartbeat-file", type=Path)
    parser.add_argument("--weights", default="/models/yolov8s.pt")
    parser.add_argument("--device", default="0")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--clip-pre", type=float, default=20.0)
    parser.add_argument("--clip-post", type=float, default=25.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 1 or args.workers > 8:
        raise RuntimeError("workers must be between one and eight")
    batch_root = args.batch_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    items = collect_skipped_events(batch_root)
    heartbeat(args.heartbeat_file, "processing", phase="track_evidence_review", reviewed=0, total=len(items))

    weak = [item for item in items if not has_strong_track_evidence(item["event"])]
    secondary_by_key: dict[tuple[str, int, float], dict] = {}
    if weak:
        from ultralytics import YOLO

        model = YOLO(args.weights)
        for index, item in enumerate(weak, start=1):
            secondary = secondary_visual_review(
                model, item, output_dir / "audit-thumbnails", args.device
            )
            event = item["event"]
            secondary_by_key[(item["date"], *event_key(event))] = secondary
            heartbeat(
                args.heartbeat_file,
                "processing",
                phase="secondary_visual_review",
                reviewed=index,
                total=len(weak),
                confirmed=sum(review["confirmed"] for review in secondary_by_key.values()),
            )

    reviews: list[dict] = []
    confirmed_items: list[tuple[dict, dict]] = []
    for item in items:
        event = item["event"]
        strong = has_strong_track_evidence(event)
        secondary = secondary_by_key.get((item["date"], *event_key(event)))
        confirmed = all_detector_checks_pass(event) and (strong or bool(secondary and secondary["confirmed"]))
        review = {
            "date": item["date"],
            "source": item["source"],
            "rear_track_id": int(event["track_id"]),
            "rear_peak_time": float(event["peak_time"]),
            "class_name": event["class_name"],
            "contains_vehicle": confirmed,
            "review_method": REVIEW_METHOD,
            "detector_evidence": {
                "all_six_overtake_checks_pass": all_detector_checks_pass(event),
                "strong_track_evidence": strong,
                "max_confidence": event["max_confidence"],
                "detections": event["detections"],
                "duration": event["duration"],
                "peak_area": event["peak_area"],
                "area_ratio": event["area_ratio"],
                "trajectory_checks": event["checks"],
            },
            "secondary_visual_review": secondary or {"required": False, "confirmed": None},
            "original_skip_reason": item["original_skip_reason"],
        }
        reviews.append(review)
        if confirmed:
            confirmed_items.append((item, review))

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                extract_rear_clip, item, output_dir, args.clip_pre, args.clip_post
            ): review
            for item, review in confirmed_items
        }
        for future in concurrent.futures.as_completed(futures):
            review = futures[future]
            review.update(future.result())
            completed += 1
            heartbeat(
                args.heartbeat_file,
                "processing",
                phase="rear_only_clip_extraction",
                clips_completed=completed,
                clips_total=len(confirmed_items),
            )

    reviews.sort(key=lambda row: (row["date"], row["rear_peak_time"], row["rear_track_id"]))
    rejected = [row for row in reviews if not row["contains_vehicle"]]
    confirmed = [row for row in reviews if row["contains_vehicle"]]
    errors: list[str] = []
    if len(reviews) != len(items):
        errors.append("not every skipped event was reviewed")
    for row in confirmed:
        clip = Path(row.get("clip", ""))
        if not clip.is_file() or clip.stat().st_size <= 0:
            errors.append(f"missing confirmed vehicle clip: {clip}")
            continue
        if clip.stat().st_uid != os.getuid():
            errors.append(f"wrong clip owner: {clip}")

    report = {
        "schema_version": 1,
        "review_method": REVIEW_METHOD,
        "original_skipped_events": len(items),
        "reviewed_events": len(reviews),
        "vehicles_confirmed": len(confirmed),
        "events_rejected": len(rejected),
        "strong_track_confirmations": sum(
            row["detector_evidence"]["strong_track_evidence"] for row in confirmed
        ),
        "secondary_visual_confirmations": sum(
            bool(row["secondary_visual_review"].get("required"))
            and bool(row["secondary_visual_review"].get("confirmed"))
            for row in confirmed
        ),
        "clip_pre_seconds": args.clip_pre,
        "clip_post_seconds": args.clip_post,
        "events": reviews,
    }
    atomic_json(output_dir / "review.json", report)
    validation = {
        "valid": not errors,
        "review_method": REVIEW_METHOD,
        "reviewed_events": len(reviews),
        "vehicles_confirmed": len(confirmed),
        "events_rejected": len(rejected),
        "errors": errors,
    }
    atomic_json(output_dir / "validation.json", validation)
    heartbeat(
        args.heartbeat_file,
        "complete" if not errors else "failed",
        phase="complete",
        reviewed_events=len(reviews),
        vehicles_confirmed=len(confirmed),
        events_rejected=len(rejected),
    )
    print(json.dumps(validation, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
