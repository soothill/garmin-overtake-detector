#!/usr/bin/env python3
"""Capture Raspberry Pi voltage/throttling health around a benchmark run."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path


def command_output(command: list[str]) -> str:
    try:
        return subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=20
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def parse_throttled(text: str) -> int:
    match = re.search(r"0x([0-9a-fA-F]+)", text)
    if not match:
        raise ValueError(f"could not parse throttle status: {text!r}")
    return int(match.group(1), 16)


def snapshot() -> dict:
    raw_status = command_output(["vcgencmd", "get_throttled"])
    status = parse_throttled(raw_status)
    return {
        "epoch": time.time(),
        "throttled_raw": raw_status,
        "throttled_status": status,
        "current_fault_bits": status & 0xF,
        "historical_fault_bits": status & 0xF0000,
        "temperature": command_output(["vcgencmd", "measure_temp"]),
        "arm_clock": command_output(["vcgencmd", "measure_clock", "arm"]),
    }


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
    parser.add_argument("--mode", choices=("before", "after"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = snapshot()
    payload = {"schema_version": 1, "mode": args.mode, **state}
    if args.mode == "before":
        payload["valid"] = state["current_fault_bits"] == 0
    else:
        if not args.before:
            raise SystemExit("--before is required in after mode")
        before = json.loads(args.before.read_text(encoding="utf-8"))
        started = float(before["epoch"])
        try:
            journal_result = subprocess.run(
                ["journalctl", "-k", "--since", f"@{started}", "--no-pager"],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            journal = journal_result.stdout
            journal_available = journal_result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            journal = ""
            journal_available = False
        undervoltage_events = len(
            re.findall(r"undervoltage detected", journal, flags=re.IGNORECASE)
        )
        throttle_events = len(
            re.findall(
                r"(?:temperature limit|throttl(?:e|ing).*detected)",
                journal,
                flags=re.IGNORECASE,
            )
        )
        payload.update(
            {
                "benchmark_start_epoch": started,
                "kernel_journal_available": journal_available,
                "undervoltage_events": undervoltage_events,
                "throttle_events": throttle_events,
                "valid": (
                    state["current_fault_bits"] == 0
                    and journal_available
                    and undervoltage_events == 0
                    and throttle_events == 0
                ),
            }
        )
    atomic_write(args.output, payload)
    print(json.dumps(payload, indent=2))
    return 0 if payload["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
