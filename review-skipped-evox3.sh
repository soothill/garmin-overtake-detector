#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
batch_name=${GARMIN_OUTPUT_BATCH:-paired-parallel-v1}
batch_root="$output_root/$batch_name"
review_dir="$batch_root/reviewed-skipped"
container_name=garmin-overtakes-skipped-review
minimum_free_gib=${GARMIN_REVIEW_MINIMUM_FREE_GIB:-100}
kfd_gid=$(stat -c '%g' /dev/kfd)
dri_gid=$(stat -c '%g' /dev/dri/renderD128)

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

exec 9>"$batch_root/reviewed-skipped.lock"
if ! flock -n 9; then
  echo "the skipped-event review is already running" >&2
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

mkdir -p "$review_dir"
docker run --rm --name "$container_name" \
  --user "$(id -u):$(id -g)" \
  --device=/dev/kfd \
  --device=/dev/dri \
  --group-add "$kfd_gid" \
  --group-add "$dri_gid" \
  --ipc=host \
  --env HOME=/tmp \
  --volume "$source_mount":/videos:ro \
  --volume "$output_root":/output \
  --volume "$script_dir/review_skipped_events.py":/app/review_skipped_events.py:ro \
  --entrypoint python3 \
  "$docker_image" \
  /app/review_skipped_events.py \
  --batch-root "/output/$batch_name" \
  --output-dir "/output/$batch_name/reviewed-skipped" \
  --heartbeat-file "/output/$batch_name/reviewed-skipped/progress.json" \
  --workers 3

systemctl --user start --no-block garmin-output-video-mirror.service >/dev/null 2>&1 || true
