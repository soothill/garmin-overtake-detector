#!/usr/bin/env bash
set -euo pipefail

: "${PLATFORM_BENCH_SOURCE_ROOT:?set PLATFORM_BENCH_SOURCE_ROOT}"
: "${PLATFORM_BENCH_WORK_ROOT:?set PLATFORM_BENCH_WORK_ROOT}"
image=${PLATFORM_BENCH_MEDIA_IMAGE:-garmin-overtakes:rocm7.14-yolov8s}

exec docker run --rm \
  --volume "$PLATFORM_BENCH_SOURCE_ROOT:$PLATFORM_BENCH_SOURCE_ROOT:ro" \
  --volume "$PLATFORM_BENCH_WORK_ROOT:$PLATFORM_BENCH_WORK_ROOT:ro" \
  --entrypoint ffprobe \
  "$image" "$@"
