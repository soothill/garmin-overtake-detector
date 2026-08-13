#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
smoke_root="$output_root/parallel-smoke"
duration=${GARMIN_SMOKE_DURATION_SECONDS:-120}
front_source=${1:?pass a front-camera path beneath /videos}
rear_source=${2:?pass a rear-camera path beneath /videos}
front_pid=""
rear_pid=""

cleanup() {
  docker rm -f garmin-overtakes-smoke-front garmin-overtakes-smoke-rear \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

if [[ "$(realpath -m "$smoke_root")" != "$output_root/parallel-smoke" ]]; then
  echo "unsafe smoke output path" >&2
  exit 2
fi
rm -rf -- "$smoke_root"
mkdir -p "$smoke_root/front" "$smoke_root/rear"

GARMIN_OVERTAKES_CONTAINER_NAME=garmin-overtakes-smoke-front \
GARMIN_DISABLE_PER_FILE_POWER=1 \
  "$script_dir/benchmark-evox3.sh" parallel-smoke-front \
    --source "$front_source" --output-dir /output/parallel-smoke/front \
    --heartbeat-file /output/parallel-smoke/front/progress.json \
    --camera front --duration "$duration" --no-clips \
    >"$smoke_root/front.log" 2>&1 &
front_pid=$!

GARMIN_OVERTAKES_CONTAINER_NAME=garmin-overtakes-smoke-rear \
GARMIN_DISABLE_PER_FILE_POWER=1 \
  "$script_dir/benchmark-evox3.sh" parallel-smoke-rear \
    --source "$rear_source" --output-dir /output/parallel-smoke/rear \
    --heartbeat-file /output/parallel-smoke/rear/progress.json \
    --camera rear --duration "$duration" --no-clips \
    >"$smoke_root/rear.log" 2>&1 &
rear_pid=$!

front_status=0
rear_status=0
wait "$front_pid" || front_status=$?
wait "$rear_pid" || rear_status=$?
front_pid=""
rear_pid=""
if (( front_status != 0 || rear_status != 0 )); then
  echo "parallel smoke failed: front=$front_status rear=$rear_status" >&2
  exit 1
fi

python3 - "$smoke_root" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
for camera in ("front", "rear"):
    result = json.loads((root / camera / "run.json").read_text())
    print(
        f"{camera}: {result['processed_source_seconds']:.1f}s source in "
        f"{result['wall_seconds']:.2f}s wall"
    )
PY
