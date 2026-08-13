#!/usr/bin/env python3
"""Detect vehicle overtakes and extract original-quality source clips."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence


VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


@dataclass(frozen=True)
class Observation:
    timestamp: float
    left: float
    top: float
    right: float
    bottom: float
    confidence: float

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.right - self.left) * max(0.0, self.bottom - self.top)


@dataclass
class TrackHistory:
    track_id: int
    class_id: int
    observations: list[Observation] = field(default_factory=list)

    @property
    def first_seen(self) -> float:
        return self.observations[0].timestamp

    @property
    def last_seen(self) -> float:
        return self.observations[-1].timestamp

    def add(self, observation: Observation) -> None:
        self.observations.append(observation)


@dataclass(frozen=True)
class Thresholds:
    min_detections: int = 4
    min_duration: float = 0.8
    min_peak_area: float = 0.006
    min_area_ratio: float = 1.7
    min_vertical_travel: float = 0.06
    min_side_offset: float = 0.08
    min_radial_ratio: float = 0.30
    max_endpoint_offset_growth: float = 0.03
    centerline_tolerance: float = 0.04
    max_peak_fraction_front: float = 0.45
    min_peak_fraction_rear: float = 0.45


def _linear_slope(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    mean_x = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    denominator = sum((point[0] - mean_x) ** 2 for point in points)
    if denominator <= 1e-12:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator


def evaluate_track(
    track: TrackHistory,
    camera: str,
    thresholds: Thresholds | None = None,
) -> dict:
    """Return event metrics and a deterministic candidate decision."""

    thresholds = thresholds or Thresholds()
    observations = track.observations
    if not observations:
        raise ValueError("track has no observations")

    areas = [max(observation.area, 1e-9) for observation in observations]
    peak_index = max(range(len(areas)), key=areas.__getitem__)
    peak = observations[peak_index]
    peak_area = areas[peak_index]
    final_area = areas[-1]
    first_area = areas[0]
    duration = track.last_seen - track.first_seen
    peak_fraction = peak_index / max(1, len(observations) - 1)
    side_offset = abs(peak.center_x - 0.5)

    if camera == "front":
        tail = observations[peak_index:]
        endpoint = observations[-1]
        area_ratio = peak_area / max(final_area, 1e-9)
        vertical_travel = peak.bottom - endpoint.bottom
        direction_slope = _linear_slope(
            [(item.timestamp, math.log(max(item.area, 1e-9))) for item in tail]
        )
        peak_position_ok = peak_fraction <= thresholds.max_peak_fraction_front
        direction_ok = direction_slope < 0.0
    elif camera == "rear":
        head = observations[: peak_index + 1]
        endpoint = observations[0]
        area_ratio = peak_area / max(first_area, 1e-9)
        vertical_travel = peak.bottom - endpoint.bottom
        direction_slope = _linear_slope(
            [(item.timestamp, math.log(max(item.area, 1e-9))) for item in head]
        )
        peak_position_ok = peak_fraction >= thresholds.min_peak_fraction_rear
        direction_ok = direction_slope > 0.0
    else:
        raise ValueError(f"unsupported camera orientation: {camera}")

    endpoint_offset = abs(endpoint.center_x - 0.5)
    lateral_travel = abs(peak.center_x - endpoint.center_x)
    radial_ratio = vertical_travel / max(lateral_travel, 1e-6)
    peak_side = peak.center_x - 0.5
    endpoint_side = endpoint.center_x - 0.5
    centerline_consistent = (
        peak_side * endpoint_side >= 0.0
        or endpoint_offset <= thresholds.centerline_tolerance
    )
    radial_trajectory_ok = (
        centerline_consistent
        and endpoint_offset <= side_offset + thresholds.max_endpoint_offset_growth
        and radial_ratio >= thresholds.min_radial_ratio
    )
    trajectory_ok = (
        peak_position_ok
        and direction_ok
        and vertical_travel >= thresholds.min_vertical_travel
        and radial_trajectory_ok
    )

    checks = {
        "detections": len(observations) >= thresholds.min_detections,
        "duration": duration >= thresholds.min_duration,
        "peak_area": peak_area >= thresholds.min_peak_area,
        "area_ratio": area_ratio >= thresholds.min_area_ratio,
        "side_offset": side_offset >= thresholds.min_side_offset,
        "trajectory": trajectory_ok,
    }
    candidate = all(checks.values())

    return {
        "track_id": track.track_id,
        "class_id": track.class_id,
        "class_name": VEHICLE_CLASSES.get(track.class_id, str(track.class_id)),
        "candidate": candidate,
        "checks": checks,
        "first_seen": round(track.first_seen, 3),
        "last_seen": round(track.last_seen, 3),
        "duration": round(duration, 3),
        "peak_time": round(peak.timestamp, 3),
        "peak_area": round(peak_area, 6),
        "area_ratio": round(area_ratio, 3),
        "vertical_travel": round(vertical_travel, 3),
        "lateral_travel": round(lateral_travel, 3),
        "radial_ratio": round(radial_ratio, 3),
        "endpoint_offset": round(endpoint_offset, 3),
        "centerline_consistent": centerline_consistent,
        "side": "left" if peak.center_x < 0.5 else "right",
        "side_offset": round(side_offset, 3),
        "direction_slope": round(direction_slope, 4),
        "peak_fraction": round(peak_fraction, 3),
        "max_confidence": round(max(item.confidence for item in observations), 4),
        "detections": len(observations),
    }


def deduplicate_candidates(
    summaries: Sequence[dict],
    peak_window: float = 1.0,
    track_gap: float = 0.25,
) -> list[dict]:
    """Suppress adjacent candidate tracks that are one class-switching vehicle."""

    ordered = sorted(summaries, key=lambda item: item["peak_time"])
    for summary in ordered:
        summary["candidate_raw"] = bool(summary["candidate"])
        summary["duplicate_of"] = None

    unique: list[dict] = []
    for summary in (item for item in ordered if item["candidate"]):
        if unique:
            previous = unique[-1]
            adjacent = (
                summary["side"] == previous["side"]
                and summary["peak_time"] - previous["peak_time"] <= peak_window
                and summary["first_seen"] <= previous["last_seen"] + track_gap
            )
            if adjacent:
                winner = max(
                    (previous, summary),
                    key=lambda item: (item["detections"], item["duration"], item["peak_area"]),
                )
                loser = summary if winner is previous else previous
                loser["candidate"] = False
                loser["duplicate_of"] = winner["track_id"]
                if winner is summary:
                    unique[-1] = summary
                continue
        unique.append(summary)
    return ordered


def probe_video(path: Path) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,codec_name:format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    numerator, denominator = stream["avg_frame_rate"].split("/", 1)
    frame_rate = float(numerator) / float(denominator)
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "frame_rate": frame_rate,
        "codec": stream["codec_name"],
        "duration": float(payload["format"]["duration"]),
        "size": int(payload["format"]["size"]),
    }


def detection_dimensions(width: int, height: int, target_width: int) -> tuple[int, int]:
    target_height = int(round(height * target_width / width))
    if target_height % 2:
        target_height += 1
    return target_width, target_height


def frame_reader(
    source: Path,
    sample_fps: float,
    width: int,
    height: int,
    decode: str,
    start: float,
    duration: float | None,
) -> Iterator[tuple[float, "object"]]:
    import numpy as np

    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        command.extend(["-ss", f"{start:.3f}"])
    if decode == "vaapi":
        command.extend(
            ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"]
        )
    command.extend(["-i", str(source)])
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(
        [
            "-an",
            "-sn",
            "-vf",
            f"fps={sample_fps},scale={width}:{height}:flags=fast_bilinear",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ]
    )

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    frame_size = width * height * 3
    frame_index = 0
    while True:
        payload = process.stdout.read(frame_size)
        if not payload:
            break
        if len(payload) != frame_size:
            process.kill()
            raise RuntimeError(f"short FFmpeg frame: {len(payload)} of {frame_size} bytes")
        frame = np.frombuffer(payload, dtype=np.uint8).reshape((height, width, 3))
        yield start + frame_index / sample_fps, frame
        frame_index += 1

    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    return_code = process.wait()
    if return_code:
        raise RuntimeError(f"FFmpeg decode failed ({return_code}): {stderr.strip()}")


def extract_clip(
    source: Path,
    output: Path,
    start: float,
    end: float,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.1, end - start)
    command = [
        "ffmpeg",
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
        str(output),
    ]
    subprocess.run(command, check=True)


def atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_heartbeat(path: Path | None, state: str, **values: object) -> None:
    if path is None:
        return
    atomic_write_text(
        path,
        json.dumps(
            {"state": state, "updated_epoch": time.time(), **values}, indent=2
        )
        + "\n",
    )


def write_reports(
    output_dir: Path,
    metadata: dict,
    summaries: Sequence[dict],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    events = [summary for summary in summaries if summary["candidate"]]
    atomic_write_text(
        output_dir / "run.json",
        json.dumps({**metadata, "events": events}, indent=2) + "\n",
    )
    atomic_write_text(
        output_dir / "tracks.jsonl",
        "".join(json.dumps(summary, sort_keys=True) + "\n" for summary in summaries),
    )

    columns = [
        "track_id",
        "class_name",
        "peak_time",
        "first_seen",
        "last_seen",
        "side",
        "max_confidence",
        "clip",
        "paired_clip",
    ]
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(events)
    atomic_write_text(output_dir / "events.csv", csv_buffer.getvalue())


def process_video(args: argparse.Namespace) -> dict:
    import torch
    from ultralytics import YOLO

    source = Path(args.source).resolve()
    output_dir = Path(args.output_dir).resolve()
    heartbeat_path = Path(args.heartbeat_file).resolve() if args.heartbeat_file else None
    output_dir.mkdir(parents=True, exist_ok=True)
    info = probe_video(source)
    paired_source = Path(args.paired_source).resolve() if args.paired_source else None
    paired_info = probe_video(paired_source) if paired_source else None
    detect_width, detect_height = detection_dimensions(
        info["width"], info["height"], args.detect_width
    )

    if args.device != "cpu" and not torch.cuda.is_available():
        raise RuntimeError("ROCm GPU is unavailable to PyTorch")
    device_name = "cpu" if args.device == "cpu" else torch.cuda.get_device_name(0)
    model = YOLO(args.weights)

    active: dict[int, TrackHistory] = {}
    completed: list[TrackHistory] = []
    last_timestamp = args.start
    frames = 0
    wall_start = time.monotonic()
    write_heartbeat(
        heartbeat_path,
        "processing",
        source=str(source),
        camera=args.camera,
        phase="detection",
        frames=0,
        source_seconds=0.0,
    )

    for timestamp, frame in frame_reader(
        source=source,
        sample_fps=args.sample_fps,
        width=detect_width,
        height=detect_height,
        decode=args.decode,
        start=args.start,
        duration=args.duration,
    ):
        last_timestamp = timestamp
        result = model.track(
            frame,
            persist=True,
            tracker="botsort.yaml",
            classes=list(VEHICLE_CLASSES),
            conf=args.confidence,
            iou=args.iou,
            imgsz=args.imgsz,
            device=args.device,
            quantize=16 if args.device != "cpu" else None,
            verbose=False,
        )[0]
        frames += 1
        seen: set[int] = set()
        boxes = result.boxes
        if boxes is not None and boxes.id is not None:
            coordinates = boxes.xyxy.cpu().numpy()
            identifiers = boxes.id.int().cpu().tolist()
            classes = boxes.cls.int().cpu().tolist()
            confidences = boxes.conf.cpu().tolist()
            for identifier, class_id, confidence, coordinates_px in zip(
                identifiers, classes, confidences, coordinates
            ):
                seen.add(identifier)
                history = active.setdefault(identifier, TrackHistory(identifier, class_id))
                left, top, right, bottom = coordinates_px.tolist()
                history.add(
                    Observation(
                        timestamp=timestamp,
                        left=left / detect_width,
                        top=top / detect_height,
                        right=right / detect_width,
                        bottom=bottom / detect_height,
                        confidence=float(confidence),
                    )
                )

        stale = [
            identifier
            for identifier, history in active.items()
            if timestamp - history.last_seen > args.track_gap
        ]
        for identifier in stale:
            completed.append(active.pop(identifier))

        if args.progress_every and frames % args.progress_every == 0:
            elapsed = time.monotonic() - wall_start
            source_seconds = frames / args.sample_fps
            speed = source_seconds / elapsed if elapsed else 0.0
            print(
                f"frames={frames} source={source_seconds:.1f}s "
                f"wall={elapsed:.1f}s speed={speed:.2f}x active_tracks={len(active)}",
                flush=True,
            )
            write_heartbeat(
                heartbeat_path,
                "processing",
                source=str(source),
                camera=args.camera,
                phase="detection",
                frames=frames,
                source_seconds=round(source_seconds, 3),
                wall_seconds=round(elapsed, 3),
                realtime_factor=round(speed, 3),
            )

    completed.extend(active.values())
    summaries = deduplicate_candidates(
        [evaluate_track(track, args.camera) for track in completed]
    )

    clip_dir = output_dir / "clips"
    if not args.no_clips:
        candidates = [summary for summary in summaries if summary["candidate"]]
        for clip_index, summary in enumerate(candidates, start=1):
            clip_start = max(0.0, summary["peak_time"] - args.clip_pre)
            clip_end = min(info["duration"], summary["peak_time"] + args.clip_post)
            clip_name = (
                f"{source.stem}_t{summary['peak_time']:010.3f}_"
                f"track{summary['track_id']:05d}_{summary['class_name']}.mp4"
            )
            clip_path = clip_dir / clip_name
            extract_clip(source, clip_path, clip_start, clip_end)
            summary["clip"] = str(clip_path)
            if paired_source is not None and paired_info is not None:
                paired_start = max(0.0, clip_start + args.paired_offset)
                paired_end = min(
                    paired_info["duration"], clip_end + args.paired_offset
                )
                paired_name = (
                    f"{source.stem}_t{summary['peak_time']:010.3f}_"
                    f"track{summary['track_id']:05d}_{paired_source.stem}_paired.mp4"
                )
                paired_path = clip_dir / "paired" / paired_name
                extract_clip(paired_source, paired_path, paired_start, paired_end)
                summary["paired_clip"] = str(paired_path)
            write_heartbeat(
                heartbeat_path,
                "processing",
                source=str(source),
                camera=args.camera,
                phase="clip_extraction",
                clips_completed=clip_index,
                clips_total=len(candidates),
            )

    wall_seconds = time.monotonic() - wall_start
    source_seconds = frames / args.sample_fps
    metadata = {
        "source": str(source),
        "camera": args.camera,
        "weights": args.weights,
        "device": args.device,
        "device_name": device_name,
        "decode": args.decode,
        "sample_fps": args.sample_fps,
        "detect_size": [detect_width, detect_height],
        "source_video": info,
        "paired_source": str(paired_source) if paired_source else None,
        "paired_source_video": paired_info,
        "paired_offset": args.paired_offset,
        "clips_enabled": not args.no_clips,
        "clip_pre_seconds": args.clip_pre,
        "clip_post_seconds": args.clip_post,
        "processed_frames": frames,
        "processed_source_seconds": round(source_seconds, 3),
        "wall_seconds": round(wall_seconds, 3),
        "realtime_factor": round(source_seconds / wall_seconds, 3) if wall_seconds else None,
        "candidate_events": sum(item["candidate"] for item in summaries),
        "completed_tracks": len(summaries),
    }
    write_reports(output_dir, metadata, summaries)
    write_heartbeat(
        heartbeat_path,
        "complete",
        source=str(source),
        camera=args.camera,
        phase="complete",
        frames=frames,
        source_seconds=round(source_seconds, 3),
        wall_seconds=round(wall_seconds, 3),
        candidate_events=metadata["candidate_events"],
    )
    return metadata


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Input MP4 path")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--paired-source", help="Synchronized second-camera MP4")
    parser.add_argument(
        "--paired-offset",
        type=float,
        default=0.0,
        help="Seconds added to detection-camera timestamps for paired clips",
    )
    parser.add_argument("--camera", choices=("front", "rear"), default="front")
    parser.add_argument("--weights", default="/models/yolov8s.pt")
    parser.add_argument("--device", default="0", help="Ultralytics device, normally 0 or cpu")
    parser.add_argument("--decode", choices=("vaapi", "cpu"), default="vaapi")
    parser.add_argument("--sample-fps", type=float, default=5.0)
    parser.add_argument("--detect-width", type=int, default=640)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--confidence", type=float, default=0.20)
    parser.add_argument("--iou", type=float, default=0.50)
    parser.add_argument("--track-gap", type=float, default=1.0)
    parser.add_argument("--clip-pre", type=float, default=12.0)
    parser.add_argument("--clip-post", type=float, default=15.0)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--no-clips", action="store_true")
    parser.add_argument("--heartbeat-file")
    parser.add_argument("--progress-every", type=int, default=100)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    metadata = process_video(args)
    print(json.dumps(metadata, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
