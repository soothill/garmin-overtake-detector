#!/usr/bin/env python3
"""Sample Raspberry Pi PMIC output-rail power for a timestamped benchmark CSV."""

from __future__ import annotations

import argparse
import csv
import re
import signal
import subprocess
import time
from pathlib import Path


RAILS = (
    "3V7_WL_SW",
    "3V3_SYS",
    "1V8_SYS",
    "DDR_VDD2",
    "DDR_VDDQ",
    "1V1_SYS",
    "0V8_SW",
    "VDD_CORE",
    "3V3_DAC",
    "3V3_ADC",
    "0V8_AON",
)
VALUE = re.compile(
    r"^\s*(?P<rail>[A-Z0-9_]+)_(?P<kind>[AV])\s+"
    r"(?:current|volt)\(\d+\)=(?P<value>[0-9.]+)[AV]$"
)


def parse_pmic_output(output: str) -> dict[str, float]:
    values: dict[tuple[str, str], float] = {}
    for line in output.splitlines():
        match = VALUE.match(line)
        if match:
            values[(match.group("rail"), match.group("kind"))] = float(
                match.group("value")
            )
    powers = {
        rail: values[(rail, "A")] * values[(rail, "V")]
        for rail in RAILS
        if (rail, "A") in values and (rail, "V") in values
    }
    if len(powers) != len(RAILS):
        missing = sorted(set(RAILS) - set(powers))
        raise ValueError(f"PMIC output is missing rails: {missing}")
    return powers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    columns = ["timestamp", "power_watts", *(f"{rail}_watts" for rail in RAILS)]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        handle.flush()
        while running:
            started = time.monotonic()
            output = subprocess.run(
                ["vcgencmd", "pmic_read_adc"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            powers = parse_pmic_output(output)
            writer.writerow(
                {
                    "timestamp": f"{time.time():.6f}",
                    "power_watts": f"{sum(powers.values()):.8f}",
                    **{f"{rail}_watts": f"{powers[rail]:.8f}" for rail in RAILS},
                }
            )
            handle.flush()
            delay = args.interval - (time.monotonic() - started)
            if delay > 0:
                time.sleep(delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
