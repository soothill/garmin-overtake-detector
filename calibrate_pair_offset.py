#!/usr/bin/env python3
"""Estimate the clock offset between synchronized front and rear recordings."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path

import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--front", required=True, type=Path)
    parser.add_argument("--rear", required=True, type=Path)
    parser.add_argument("--max-offset", type=float, default=1800.0)
    parser.add_argument("--envelope-rate", type=int, default=20)
    parser.add_argument("--front-start", type=float, default=0.0)
    parser.add_argument("--rear-start", type=float, default=0.0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def audio_envelope(
    path: Path, rate: int, start: float = 0.0, duration: float | None = None
) -> np.ndarray:
    sample_rate = rate * 50
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if start > 0:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(["-i", str(path)])
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend([
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ])
    payload = subprocess.run(command, check=True, capture_output=True).stdout
    audio = np.frombuffer(payload, dtype="<i2").astype(np.float32)
    usable = len(audio) - len(audio) % 50
    if usable < sample_rate * 60:
        raise RuntimeError(f"insufficient audio decoded from {path}")
    frames = audio[:usable].reshape(-1, 50)
    envelope = np.sqrt(np.mean(frames * frames, axis=1) + 1.0)
    envelope = np.log1p(envelope)
    # Remove slow wind/road-level changes while retaining shared transients.
    window = max(rate * 5, 1)
    kernel = np.ones(window, dtype=np.float32) / window
    trend = np.convolve(envelope, kernel, mode="same")
    signal = envelope - trend
    scale = float(np.std(signal))
    if scale <= 1e-6:
        raise RuntimeError(f"audio envelope has no useful variation: {path}")
    return signal / scale


def audio_waveform(
    path: Path, rate: int = 1000, start: float = 0.0, duration: float | None = None
) -> np.ndarray:
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(["-i", str(path)])
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(
        ["-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"]
    )
    payload = subprocess.run(command, check=True, capture_output=True).stdout
    audio = np.frombuffer(payload, dtype="<i2").astype(np.float32)
    if len(audio) < rate * 30:
        raise RuntimeError(f"insufficient audio decoded from {path}")
    # First differences suppress steady wind/engine energy and retain the
    # shared, precisely timed acoustic transients heard by both cameras.
    signal = np.diff(audio)
    signal -= float(np.mean(signal))
    scale = float(np.std(signal))
    if scale <= 1e-6:
        raise RuntimeError(f"audio waveform has no useful variation: {path}")
    return signal / scale


def correlation_offset(
    front: np.ndarray,
    rear: np.ndarray,
    rate: int,
    limit: float,
    absolute: bool = False,
) -> dict:
    # FFT cross-correlation. np.correlate(rear, front, "full") uses lag zero at
    # len(front)-1. Positive lag means an event occurs later in the rear file,
    # so rear_timestamp = front_timestamp + offset.
    full_length = len(front) + len(rear) - 1
    fft_length = 1 << math.ceil(math.log2(full_length))
    spectrum = np.fft.rfft(rear, fft_length) * np.conj(np.fft.rfft(front, fft_length))
    circular = np.fft.irfft(spectrum, fft_length)
    correlation = np.concatenate((circular[-(len(front) - 1) :], circular[: len(rear)]))
    lags = np.arange(-(len(front) - 1), len(rear), dtype=np.int64)

    max_lag = round(limit * rate)
    selection = (lags >= -max_lag) & (lags <= max_lag)
    selected_lags = lags[selection]
    selected = correlation[selection]

    # Penalize low-overlap edge matches. All production searches are limited,
    # but this normalization makes unusually short/misaligned rides safer.
    overlaps = np.minimum(len(front), len(rear) - np.maximum(selected_lags, 0))
    overlaps = np.minimum(overlaps, len(front) + np.minimum(selected_lags, 0))
    overlaps = np.maximum(overlaps, 1)
    normalized = selected / overlaps
    scores = np.abs(normalized) if absolute else normalized
    best_index = int(np.argmax(scores))
    best_lag = int(selected_lags[best_index])

    excluded = np.abs(selected_lags - best_lag) <= rate * 10
    alternatives = scores[~excluded]
    second = float(np.max(alternatives)) if len(alternatives) else 0.0
    best = float(scores[best_index])
    return {
        "offset_seconds": round(best_lag / rate, 3),
        "score": best,
        "correlation_polarity": 1 if normalized[best_index] >= 0 else -1,
        "score_ratio": round(best / second, 3) if second > 0 else None,
        "front_audio_seconds": round(len(front) / rate, 3),
        "rear_audio_seconds": round(len(rear) / rate, 3),
        "envelope_rate": rate,
        "max_offset_seconds": limit,
        "mapping": "rear_timestamp = front_timestamp + offset_seconds",
    }


def main() -> int:
    args = parse_args()
    front = audio_envelope(
        args.front, args.envelope_rate, args.front_start, args.duration
    )
    rear = audio_envelope(
        args.rear, args.envelope_rate, args.rear_start, args.duration
    )
    result = correlation_offset(front, rear, args.envelope_rate, args.max_offset)
    result["offset_seconds"] = round(
        args.rear_start - args.front_start + result["offset_seconds"], 3
    )
    result.update(
        {
            "front": str(args.front),
            "rear": str(args.rear),
            "front_start": args.front_start,
            "rear_start": args.rear_start,
            "sample_duration": args.duration,
        }
    )
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
