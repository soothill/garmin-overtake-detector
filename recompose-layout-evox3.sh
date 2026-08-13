#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
batch_name=${GARMIN_OUTPUT_BATCH:-paired-parallel-v1}
batch_root="$output_root/$batch_name"
stage_root="$batch_root/layout-front-left-v1"
container_name=garmin-overtakes-layout-recompose
minimum_free_gib=${GARMIN_LAYOUT_MINIMUM_FREE_GIB:-350}
workers=${GARMIN_LAYOUT_WORKERS:-8}
timer_was_active=0
dri_gid=$(stat -c '%g' /dev/dri/renderD128)

cleanup() {
  local status=$?
  trap - EXIT
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  if (( timer_was_active )); then
    systemctl --user start garmin-output-video-mirror.timer >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

exec 9>"$batch_root/layout-recompose.lock"
if ! flock -n 9; then
  echo "the layout recomposition is already running" >&2
  exit 0
fi

find "$source_mount" -maxdepth 1 -mindepth 1 -print -quit >/dev/null
mount_options=$(findmnt -t nfs4 -n -o OPTIONS "$source_mount")
if [[ ",$mount_options," != *,ro,* ]]; then
  echo "$source_mount is not read-only" >&2
  exit 65
fi
free_gib=$(df --output=avail -BG "$output_root" | tail -1 | tr -dc '0-9')
if (( free_gib < minimum_free_gib )); then
  echo "only ${free_gib} GiB is free; ${minimum_free_gib} GiB is required" >&2
  exit 65
fi

if systemctl --user is-active --quiet garmin-output-video-mirror.timer; then
  timer_was_active=1
fi
systemctl --user stop garmin-output-video-mirror.timer garmin-output-video-mirror.service
mkdir -p "$stage_root"

docker run --rm --name "$container_name" \
  --user "$(id -u):$(id -g)" \
  --device=/dev/dri \
  --group-add "$dri_gid" \
  --env HOME=/tmp \
  --volume "$source_mount":/videos:ro \
  --volume "$output_root":/output \
  --volume "$script_dir/recompose_combined_layout.py":/app/recompose_combined_layout.py:ro \
  --volume "$script_dir/compose_paired_events.py":/app/compose_paired_events.py:ro \
  --volume "$script_dir/validate_combined_result.py":/app/validate_combined_result.py:ro \
  --entrypoint python3 \
  "$docker_image" \
  /app/recompose_combined_layout.py \
  --batch-root "/output/$batch_name" \
  --output-root /output \
  --stage-root "/output/$batch_name/layout-front-left-v1" \
  --heartbeat-file "/output/$batch_name/layout-front-left-v1/progress.json" \
  --workers "$workers"

systemctl --user start --no-block garmin-output-video-mirror.service >/dev/null 2>&1 || true
