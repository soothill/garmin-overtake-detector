#!/usr/bin/env python3
"""Create Plex-ready synchronized rear/front overtake videos for one ride."""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import os
import re
import statistics
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence


CLOCK_PATTERN = re.compile(
    r"(?P<day>\d{2})/(?P<month>\d{2})/(?P<year>\d{4})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
)

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--front-source", required=True, type=Path)
    parser.add_argument("--rear-source", required=True, type=Path)
    parser.add_argument("--front-run", required=True, type=Path)
    parser.add_argument("--rear-run", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--heartbeat-file", type=Path)
    parser.add_argument("--clip-pre", type=float, default=20.0)
    parser.add_argument("--clip-post", type=float, default=25.0)
    parser.add_argument("--transition-seconds", type=float, default=5.0)
    parser.add_argument("--match-tolerance", type=float, default=6.0)
    parser.add_argument("--handoff-tolerance", type=float, default=1.5)
    parser.add_argument("--max-clock-skew", type=float, default=120.0)
    parser.add_argument("--max-offset", type=float, default=1800.0)
    parser.add_argument("--width-per-camera", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--max-clips", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def atomic_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def heartbeat(path: Path | None, state: str, **values: object) -> None:
    if path:
        atomic_text(
            path,
            json.dumps({"state": state, "updated_epoch": time.time(), **values}, indent=2)
            + "\n",
        )


def load_run(path: Path, expected_camera: str) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("camera") != expected_camera:
        raise RuntimeError(f"{path} is not a {expected_camera} result")
    return payload


def nearest_unused(values: list[float], target: float, used: set[int]) -> tuple[int, float] | None:
    position = bisect.bisect_left(values, target)
    choices: list[tuple[float, int]] = []
    for index in range(max(0, position - 2), min(len(values), position + 3)):
        if index not in used:
            choices.append((abs(values[index] - target), index))
    if not choices:
        return None
    delta, index = min(choices)
    return index, delta


def score_offset(
    rear_times: list[float],
    front_times: list[float],
    offset: float,
    transition: float,
    tolerance: float,
) -> tuple[int, float, list[tuple[float, float]]]:
    used: set[int] = set()
    residual = 0.0
    pairs: list[tuple[float, float]] = []
    for rear_time in rear_times:
        target = rear_time - offset + transition
        match = nearest_unused(front_times, target, used)
        if match is None or match[1] > tolerance:
            continue
        index, delta = match
        used.add(index)
        residual += delta
        pairs.append((rear_time, front_times[index]))
    return len(pairs), residual, pairs


def estimate_event_offset(
    rear_events: list[dict],
    front_events: list[dict],
    transition: float,
    tolerance: float,
    max_offset: float,
    duration_fallback: float,
) -> dict:
    rear_times = sorted(float(item["peak_time"]) for item in rear_events)
    front_times = sorted(float(item["peak_time"]) for item in front_events)
    if not rear_times or not front_times:
        return {
            "offset_seconds": duration_fallback,
            "matches": 0,
            "method": "duration_fallback",
            "anchor": None,
        }

    candidates = {round(duration_fallback, 1), 10.0, 0.0}
    for rear_time in rear_times:
        for front_time in front_times:
            candidate = round(rear_time - front_time + transition, 1)
            if abs(candidate) <= max_offset:
                candidates.add(candidate)

    best: tuple[tuple[int, float, float], float, list[tuple[float, float]]] | None = None
    for candidate in candidates:
        count, residual, pairs = score_offset(
            rear_times, front_times, candidate, transition, tolerance
        )
        ranking = (count, -residual, -abs(candidate - duration_fallback))
        if best is None or ranking > best[0]:
            best = (ranking, candidate, pairs)
    assert best is not None
    pairs = best[2]
    anchor = None
    if pairs:
        anchor = min(
            pairs,
            key=lambda pair: abs(
                pair[1] - (pair[0] - best[1] + transition)
            ),
        )
    return {
        "offset_seconds": best[1],
        "matches": best[0][0],
        "method": "event_sequence",
        "anchor": {"rear_peak": anchor[0], "front_peak": anchor[1]} if anchor else None,
    }


def match_event_handoffs(
    rear_events: Sequence[dict],
    front_events: Sequence[dict],
    offset: float,
    tolerance: float,
) -> list[dict]:
    """Match a rear disappearance to the nearest front appearance.

    ``offset`` maps media timelines as rear_timestamp = front_timestamp + offset.
    Unlike the burned-in clocks, this describes the same physical instant.
    """
    rear_sorted = sorted(rear_events, key=lambda item: float(item["last_seen"]))
    front_sorted = sorted(front_events, key=lambda item: float(item["first_seen"]))
    front_times = [float(item["first_seen"]) for item in front_sorted]
    used: set[int] = set()
    matches: list[dict] = []
    for rear in rear_sorted:
        rear_handoff = float(rear["last_seen"])
        target = rear_handoff - offset
        match = nearest_unused(front_times, target, used)
        if match is None or match[1] > tolerance:
            continue
        front_index, residual = match
        used.add(front_index)
        front = front_sorted[front_index]
        observed_offset = rear_handoff - float(front["first_seen"])
        matches.append(
            {
                "rear_event": rear,
                "front_event": front,
                "observed_offset_seconds": observed_offset,
                "handoff_residual_seconds": observed_offset - offset,
                "absolute_residual_seconds": residual,
            }
        )
    return matches


def build_vehicle_handoff_alignment(
    rear_events: list[dict],
    front_events: list[dict],
    transition: float,
    match_tolerance: float,
    handoff_tolerance: float,
    max_offset: float,
    duration_fallback: float,
) -> dict:
    """Build a physical alignment from repeated rear-to-front vehicle handoffs."""
    coarse = estimate_event_offset(
        rear_events,
        front_events,
        transition,
        match_tolerance,
        max_offset,
        duration_fallback,
    )
    # The coarse estimator compares event peaks and includes the expected travel
    # time between views.  Remove that travel time before matching disappearances
    # to appearances of the same vehicles.
    initial_offset = float(coarse["offset_seconds"]) - transition
    initial_matches = match_event_handoffs(
        rear_events, front_events, initial_offset, match_tolerance
    )
    if not initial_matches:
        return {
            "method": "vehicle_handoff_v1",
            "offset_seconds": initial_offset,
            "coarse": coarse,
            "candidate_matches": 0,
            "accepted_matches": 0,
            "handoff_tolerance_seconds": handoff_tolerance,
            "matches": [],
        }

    observed = [item["observed_offset_seconds"] for item in initial_matches]
    preferred = initial_offset
    best_values: list[float] = []
    best_rank: tuple[int, float, float] | None = None
    for candidate in observed:
        values = [value for value in observed if abs(value - candidate) <= handoff_tolerance]
        center = statistics.median(values)
        rank = (
            len(values),
            -sum(abs(value - center) for value in values),
            -abs(center - preferred),
        )
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_values = values
    offset = statistics.median(best_values)

    # Re-match directly on the physical handoff using the robust modal offset.
    matches = match_event_handoffs(
        rear_events, front_events, offset, handoff_tolerance
    )
    if matches:
        offset = statistics.median(
            item["observed_offset_seconds"] for item in matches
        )
        matches = match_event_handoffs(
            rear_events, front_events, offset, handoff_tolerance
        )
    for item in matches:
        item["handoff_residual_seconds"] = (
            item["observed_offset_seconds"] - offset
        )

    return {
        "method": "vehicle_handoff_v1",
        "mapping": "rear_timestamp = front_timestamp + offset_seconds",
        "offset_seconds": round(offset, 3),
        "coarse": coarse,
        "candidate_matches": len(initial_matches),
        "accepted_matches": len(matches),
        "handoff_tolerance_seconds": handoff_tolerance,
        "max_absolute_handoff_residual_seconds": round(
            max((abs(item["handoff_residual_seconds"]) for item in matches), default=0.0),
            3,
        ),
        "matches": matches,
    }


def parse_overlay_clock(text: str) -> datetime:
    match = CLOCK_PATTERN.search(text)
    if not match:
        raise RuntimeError(f"camera timestamp was not recognized: {text!r}")
    values = {name: int(value) for name, value in match.groupdict().items()}
    try:
        return datetime(
            values["year"],
            values["month"],
            values["day"],
            values["hour"],
            values["minute"],
            values["second"],
        )
    except ValueError as error:
        raise RuntimeError(f"camera timestamp is invalid: {text!r}") from error


def read_overlay_clock(path: Path, timestamp: float) -> tuple[datetime, str, float]:
    import cv2
    import numpy as np

    errors: list[str] = []
    for adjustment in (0.0, 0.4, -0.4, 0.8, -0.8, 2.0, -2.0, 5.0, -5.0):
        sample_time = max(0.0, timestamp + adjustment)
        frame = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{sample_time:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "crop=iw*0.7:ih*0.12:0:ih*0.88",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
        ).stdout
        if not frame:
            errors.append(f"ffmpeg returned no clock frame at {sample_time:.3f}s")
            continue
        try:
            decoded = cv2.imdecode(
                np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_GRAYSCALE
            )
        except cv2.error as error:
            errors.append(
                f"OpenCV could not decode the clock frame at {sample_time:.3f}s: {error}"
            )
            continue
        if decoded is None:
            errors.append(f"could not decode clock frame at {sample_time:.3f}s")
            continue
        for threshold in (210, 180):
            prepared = cv2.threshold(decoded, threshold, 255, cv2.THRESH_BINARY)[1]
            encoded, payload = cv2.imencode(".png", prepared)
            if not encoded:
                raise RuntimeError(f"could not prepare clock frame from {path}")
            try:
                text = subprocess.run(
                    ["tesseract", "stdin", "stdout", "--psm", "7"],
                    input=payload.tobytes(),
                    check=True,
                    capture_output=True,
                    text=False,
                ).stdout.decode("utf-8", errors="replace")
            except subprocess.CalledProcessError as error:
                errors.append(
                    "tesseract failed "
                    f"at {sample_time:.3f}s with status {error.returncode}"
                )
                continue
            try:
                return parse_overlay_clock(text), text.strip(), sample_time
            except RuntimeError as error:
                errors.append(str(error))
    raise RuntimeError(
        f"could not read the burned-in camera clock from {path} near {timestamp:.3f}: "
        + "; ".join(errors)
    )


def synchronize_clock_time(
    front_source: Path,
    rear_source: Path,
    rear_time: float,
    initial_offset: float,
    front_duration: float,
) -> dict:
    rear_clock, rear_text, rear_sample_time = read_overlay_clock(rear_source, rear_time)
    front_time = min(max(0.0, rear_time - initial_offset), front_duration - 0.001)
    observations: list[dict] = []
    for _ in range(4):
        front_clock, front_text, front_sample_time = read_overlay_clock(
            front_source, front_time
        )
        delta = (rear_clock - front_clock).total_seconds()
        observations.append(
            {
                "front_time": round(front_sample_time, 3),
                "front_clock": front_clock.isoformat(),
                "clock_delta_seconds": delta,
                "ocr": front_text,
            }
        )
        front_time = front_sample_time + delta
        if not 0.0 <= front_time < front_duration:
            raise RuntimeError(
                f"clock synchronization mapped rear time {rear_time:.3f} outside "
                f"the front recording ({front_time:.3f})"
            )
        if abs(delta) <= 1.0:
            mapped_front_time = front_time + (rear_time - rear_sample_time)
            return {
                "rear_time": round(rear_sample_time, 3),
                "requested_rear_time": round(rear_time, 3),
                "front_time": round(mapped_front_time, 3),
                "offset_seconds": round(rear_time - mapped_front_time, 3),
                "rear_clock": rear_clock.isoformat(),
                "front_clock": front_clock.isoformat(),
                "residual_seconds": delta,
                "rear_ocr": rear_text,
                "observations": observations,
            }
    raise RuntimeError(
        f"burned-in clocks did not converge for rear time {rear_time:.3f}: "
        f"{observations!r}"
    )


def refine_offset_with_overlay(
    front_source: Path,
    rear_source: Path,
    coarse: dict,
    front_duration: float,
    rear_duration: float,
    rear_events: Sequence[dict] = (),
) -> dict:
    anchor = coarse.get("anchor")
    candidates: list[float] = []
    if anchor:
        candidates.append(float(anchor["rear_peak"]))
    candidates.extend(float(event["peak_time"]) for event in rear_events)
    candidates.append(min(front_duration, rear_duration) / 2.0)
    failures: list[str] = []
    tried: set[float] = set()
    for rear_time in candidates:
        rounded_time = round(rear_time, 3)
        if rounded_time in tried:
            continue
        tried.add(rounded_time)
        try:
            synchronized = synchronize_clock_time(
                front_source,
                rear_source,
                rear_time,
                float(coarse["offset_seconds"]),
                front_duration,
            )
        except RuntimeError as error:
            failures.append(f"{rear_time:.3f}s: {error}")
            if len(failures) >= 20:
                break
            continue
        return {
            "method": "burned_in_clock_ocr",
            "offset_seconds": synchronized["offset_seconds"],
            "coarse_offset_seconds": coarse["offset_seconds"],
            "event_matches": coarse["matches"],
            "mapping": "rear_timestamp = front_timestamp + offset_seconds",
            "anchor": synchronized,
            "failed_anchor_attempts": failures,
        }
    raise RuntimeError(
        "could not calibrate the burned-in clocks at any candidate time: "
        + "; ".join(failures)
    )


def observe_clock_delta(
    front_source: Path,
    rear_source: Path,
    rear_time: float,
    physical_offset: float,
    front_duration: float,
) -> dict:
    """Record camera clock skew without using it to move either timeline."""
    front_time = rear_time - physical_offset
    if not 0.0 <= front_time < front_duration:
        raise RuntimeError("physical alignment maps outside the front recording")
    rear_clock, rear_text, rear_sample = read_overlay_clock(rear_source, rear_time)
    mapped_front = front_time + (rear_sample - rear_time)
    front_clock, front_text, front_sample = read_overlay_clock(
        front_source, mapped_front
    )
    return {
        "rear_time": round(rear_sample, 3),
        "front_time": round(front_sample, 3),
        "rear_clock": rear_clock.isoformat(),
        "front_clock": front_clock.isoformat(),
        "display_clock_delta_seconds": (rear_clock - front_clock).total_seconds(),
        "rear_ocr": rear_text,
        "front_ocr": front_text,
    }


def read_event_clock_observations(
    source: Path,
    events: Sequence[dict],
    handoff_field: str,
    heartbeat_file: Path | None = None,
    date: str = "",
    camera: str = "",
) -> tuple[list[dict], list[dict]]:
    """OCR the camera clock at each detected vehicle handoff."""
    observations: list[dict] = []
    failures: list[dict] = []
    epoch = datetime(1970, 1, 1)
    for index, event in enumerate(events, start=1):
        requested = float(event[handoff_field])
        try:
            clock, text, sample_time = read_overlay_clock(source, requested)
        except RuntimeError as error:
            failures.append(
                {
                    "track_id": event.get("track_id"),
                    "media_time": requested,
                    "error": str(error),
                }
            )
        else:
            # read_overlay_clock may move to a nearby readable frame. Translate
            # the whole-second OCR result back to the requested event timestamp.
            event_clock = clock + timedelta(seconds=requested - sample_time)
            observations.append(
                {
                    "event": event,
                    "media_time": requested,
                    "clock": event_clock.isoformat(timespec="milliseconds"),
                    "clock_seconds": (event_clock - epoch).total_seconds(),
                    "ocr_sample_time": sample_time,
                    "ocr": text,
                }
            )
        heartbeat(
            heartbeat_file,
            "processing",
            date=date,
            phase=f"{camera}_event_clock_ocr",
            observations_completed=index,
            observations_total=len(events),
            observations_valid=len(observations),
        )
    return observations, failures


def match_clock_observations(
    rear_observations: Sequence[dict],
    front_observations: Sequence[dict],
    clock_bias: float,
    tolerance: float,
) -> list[dict]:
    """Match rear/front events after compensating for camera clock bias."""
    rear_sorted = sorted(rear_observations, key=lambda item: item["clock_seconds"])
    front_sorted = sorted(front_observations, key=lambda item: item["clock_seconds"])
    front_times = [float(item["clock_seconds"]) for item in front_sorted]
    used: set[int] = set()
    matches: list[dict] = []
    for rear in rear_sorted:
        target = float(rear["clock_seconds"]) - clock_bias
        match = nearest_unused(front_times, target, used)
        if match is None or match[1] > tolerance:
            continue
        front_index, residual = match
        used.add(front_index)
        front = front_sorted[front_index]
        observed_bias = float(rear["clock_seconds"]) - float(front["clock_seconds"])
        matches.append(
            {
                "rear_observation": rear,
                "front_observation": front,
                "observed_clock_bias_seconds": observed_bias,
                "clock_match_residual_seconds": observed_bias - clock_bias,
                "absolute_residual_seconds": residual,
                "physical_offset_seconds": (
                    float(rear["media_time"]) - float(front["media_time"])
                ),
            }
        )
    return matches


def build_clock_handoff_alignment(
    rear_observations: Sequence[dict],
    front_observations: Sequence[dict],
    tolerance: float,
    max_clock_skew: float,
) -> dict:
    """Find the stable clock bias that maximizes matched vehicle handoffs."""
    candidates = {0.0}
    for rear in rear_observations:
        rear_clock = float(rear["clock_seconds"])
        for front in front_observations:
            bias = rear_clock - float(front["clock_seconds"])
            if abs(bias) <= max_clock_skew:
                candidates.add(round(bias, 1))

    best: tuple[tuple[int, float, float], float, list[dict]] | None = None
    for candidate in candidates:
        matches = match_clock_observations(
            rear_observations, front_observations, candidate, tolerance
        )
        residual = sum(item["absolute_residual_seconds"] for item in matches)
        rank = (len(matches), -residual, -abs(candidate))
        if best is None or rank > best[0]:
            best = (rank, candidate, matches)
    if best is None or not best[2]:
        return {
            "method": "vehicle_handoff_clock_v2",
            "clock_bias_seconds": 0.0,
            "accepted_matches": 0,
            "clock_match_tolerance_seconds": tolerance,
            "matches": [],
        }

    clock_bias = statistics.median(
        item["observed_clock_bias_seconds"] for item in best[2]
    )
    matches = match_clock_observations(
        rear_observations, front_observations, clock_bias, tolerance
    )
    for item in matches:
        item["clock_match_residual_seconds"] = (
            item["observed_clock_bias_seconds"] - clock_bias
        )
    return {
        "method": "vehicle_handoff_clock_v2",
        "clock_bias_seconds": round(clock_bias, 3),
        "accepted_matches": len(matches),
        "clock_match_tolerance_seconds": tolerance,
        "max_absolute_clock_residual_seconds": round(
            max(
                (abs(item["clock_match_residual_seconds"]) for item in matches),
                default=0.0,
            ),
            3,
        ),
        "physical_offset_min_seconds": round(
            min((item["physical_offset_seconds"] for item in matches), default=0.0), 3
        ),
        "physical_offset_max_seconds": round(
            max((item["physical_offset_seconds"] for item in matches), default=0.0), 3
        ),
        "matches": matches,
    }


def probe_clip(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    return {
        "duration": float(payload["format"]["duration"]),
        "size": int(payload["format"]["size"]),
        "width": int(payload["streams"][0]["width"]),
        "height": int(payload["streams"][0]["height"]),
    }


def compose_clip(
    rear_source: Path,
    front_source: Path,
    output: Path,
    rear_start: float,
    front_start: float,
    duration: float,
    width: int,
    height: int,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    common = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30"
    )
    filters = (
        f"[0:v]setpts=PTS-STARTPTS,{common},"
        f"drawtext=fontfile={font}:text=REAR:x=24:y=24:fontsize=38:"
        "fontcolor=white:box=1:boxcolor=black@0.6[rear];"
        f"[1:v]setpts=PTS-STARTPTS,{common},"
        f"drawtext=fontfile={font}:text=FRONT:x=24:y=24:fontsize=38:"
        "fontcolor=white:box=1:boxcolor=black@0.6[front];"
        "[front][rear]hstack=inputs=2,format=nv12,hwupload[video]"
    )
    temporary = output.with_name(f".{output.stem}.{os.getpid()}.tmp.mp4")
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-vaapi_device",
        "/dev/dri/renderD128",
        "-ss",
        f"{rear_start:.3f}",
        "-i",
        str(rear_source),
        "-ss",
        f"{front_start:.3f}",
        "-i",
        str(front_source),
        "-t",
        f"{duration:.3f}",
        "-filter_complex",
        filters,
        "-map",
        "[video]",
        "-map",
        "0:a:0?",
        "-c:v",
        "h264_vaapi",
        "-profile:v",
        "high",
        "-qp",
        "22",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    try:
        subprocess.run(command, check=True)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    front_run = load_run(args.front_run, "front")
    rear_run = load_run(args.rear_run, "rear")
    front_events = list(front_run.get("events") or [])
    rear_events = list(rear_run.get("events") or [])
    front_duration = float(front_run["source_video"]["duration"])
    rear_duration = float(rear_run["source_video"]["duration"])
    if rear_events or front_events:
        rear_observations, rear_clock_failures = read_event_clock_observations(
            args.rear_source,
            rear_events,
            "last_seen",
            args.heartbeat_file,
            args.date,
            "rear",
        )
        front_observations, front_clock_failures = read_event_clock_observations(
            args.front_source,
            front_events,
            "first_seen",
            args.heartbeat_file,
            args.date,
            "front",
        )
        alignment = build_clock_handoff_alignment(
            rear_observations,
            front_observations,
            args.handoff_tolerance,
            args.max_clock_skew,
        )
    else:
        rear_observations, front_observations = [], []
        rear_clock_failures, front_clock_failures = [], []
        alignment = build_clock_handoff_alignment([], [], args.handoff_tolerance, args.max_clock_skew)
    if rear_events and not alignment["matches"]:
        raise RuntimeError("no rear-to-front vehicle handoffs could be matched")
    heartbeat(
        args.heartbeat_file,
        "processing",
        date=args.date,
        phase="clock_biased_vehicle_handoff_alignment",
        clock_bias_seconds=alignment["clock_bias_seconds"],
        matched_handoffs=alignment["accepted_matches"],
    )
    calibration = {
        key: value for key, value in alignment.items() if key != "matches"
    }
    calibration["rear_clock_observations"] = len(rear_observations)
    calibration["front_clock_observations"] = len(front_observations)
    calibration["rear_clock_failures"] = rear_clock_failures
    calibration["front_clock_failures"] = front_clock_failures
    # A tracker can emit more than one candidate event with the same track ID
    # after losing and reacquiring an object. Key by the actual event object so
    # one accepted handoff can never be reused to encode another candidate.
    matches_by_rear_event = {
        id(match["rear_observation"]["event"]): match
        for match in alignment["matches"]
    }

    clip_dir = args.output_dir / "clips"
    rows: list[dict] = []
    events_to_encode = rear_events[: args.max_clips] if args.max_clips else rear_events
    skipped: list[dict] = []
    for index, event in enumerate(events_to_encode, start=1):
        desired_start = float(event["peak_time"]) - args.clip_pre
        desired_end = float(event["peak_time"]) + args.clip_post
        rear_start = max(0.0, desired_start)
        rear_end = min(rear_duration, desired_end)
        match = matches_by_rear_event.get(id(event))
        try:
            if match is None:
                raise RuntimeError(
                    "no unambiguous matching vehicle appearance in the front camera"
                )
            physical_offset = float(match["physical_offset_seconds"])
            front_start = rear_start - physical_offset
            if front_start < 0.0:
                rear_start -= front_start
                front_start = 0.0
            duration = min(rear_end - rear_start, front_duration - front_start)
            if duration < 5.0:
                raise RuntimeError("less than five seconds overlap between cameras")
            clock_residual = float(match["clock_match_residual_seconds"])
            if abs(clock_residual) > args.handoff_tolerance:
                raise RuntimeError(
                    f"vehicle clock-sequence residual is {clock_residual:.3f}s"
                )
        except RuntimeError as error:
            skipped.append(
                {
                    "rear_track_id": event["track_id"],
                    "rear_peak_time": event["peak_time"],
                    "reason": str(error),
                }
            )
            continue
        filename = (
            f"{args.date}_t{float(event['peak_time']):010.3f}_"
            f"track{int(event['track_id']):05d}_{event['class_name']}_rear-front.mp4"
        )
        output = clip_dir / filename
        if not args.dry_run:
            compose_clip(
                args.rear_source,
                args.front_source,
                output,
                rear_start,
                front_start,
                duration,
                args.width_per_camera,
                args.height,
            )
            media = probe_clip(output)
        else:
            media = {
                "duration": duration,
                "size": 1,
                "width": args.width_per_camera * 2,
                "height": args.height,
            }
        rows.append(
            {
                "date": args.date,
                "rear_track_id": event["track_id"],
                "class_name": event["class_name"],
                "side": event["side"],
                "rear_peak_time": event["peak_time"],
                "front_track_id": match["front_observation"]["event"]["track_id"],
                "physical_offset_seconds": round(physical_offset, 3),
                "rear_start": round(rear_start, 3),
                "front_start": round(front_start, 3),
                "duration": round(duration, 3),
                "synchronization": {
                    "method": "vehicle_handoff_clock_v2",
                    "rear_last_seen": match["rear_observation"]["media_time"],
                    "front_first_seen": match["front_observation"]["media_time"],
                    "rear_display_clock": match["rear_observation"]["clock"],
                    "front_display_clock": match["front_observation"]["clock"],
                    "front_track_id": match["front_observation"]["event"]["track_id"],
                    "observed_clock_bias_seconds": round(
                        float(match["observed_clock_bias_seconds"]), 3
                    ),
                    "calibrated_clock_bias_seconds": alignment["clock_bias_seconds"],
                    "clock_match_residual_seconds": round(clock_residual, 3),
                    "physical_offset_seconds": round(physical_offset, 3),
                },
                "clip": str(output),
                "media": media,
            }
        )
        heartbeat(
            args.heartbeat_file,
            "processing",
            date=args.date,
            phase="combined_clip_encoding",
            clips_completed=index,
            clips_total=len(events_to_encode),
            clock_bias_seconds=alignment["clock_bias_seconds"],
        )

    payload = {
        "schema_version": 3,
        "alignment_method": "vehicle_handoff_clock_v2",
        "date": args.date,
        "front_source": str(args.front_source),
        "rear_source": str(args.rear_source),
        "clip_pre_seconds": args.clip_pre,
        "clip_post_seconds": args.clip_post,
        "layout": "front-left_rear-right",
        "calibration": calibration,
        "rear_candidate_events": len(rear_events),
        "events_attempted": len(events_to_encode),
        "combined_clips": len(rows),
        "skipped_events": skipped,
        "events": rows,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    atomic_text(args.output_dir / "combined.json", json.dumps(payload, indent=2) + "\n")
    columns = (
        "date",
        "rear_track_id",
        "class_name",
        "side",
        "rear_peak_time",
        "front_track_id",
        "physical_offset_seconds",
        "rear_start",
        "front_start",
        "duration",
        "clip",
    )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(args.output_dir / "events.csv", buffer.getvalue())
    heartbeat(
        args.heartbeat_file,
        "complete",
        date=args.date,
        phase="complete",
        combined_clips=len(rows),
        clock_bias_seconds=alignment["clock_bias_seconds"],
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
