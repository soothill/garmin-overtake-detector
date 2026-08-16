#!/usr/bin/env python3
"""Set and record the input-cache precondition without requiring root."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("cold", "warm"), default="cold")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("source", nargs="+", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    records = []
    for path in args.source:
        with path.open("rb", buffering=0) as handle:
            if args.mode == "cold":
                os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            else:
                while handle.read(8 * 1024 * 1024):
                    pass
        stat = path.stat()
        records.append(
            {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        )
    payload = {
        "schema_version": 1,
        "mode": args.mode,
        "method": "posix_fadvise_dontneed"
        if args.mode == "cold"
        else "sequential_read",
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "sources": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
