#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
mkdir -p "$output_root"

kfd_gid=$(stat -c '%g' /dev/kfd)
dri_gid=$(stat -c '%g' /dev/dri/renderD128)

docker_arguments=(run --rm)
if [[ -n "${GARMIN_OVERTAKES_CONTAINER_NAME:-}" ]]; then
  docker_arguments+=(--name "$GARMIN_OVERTAKES_CONTAINER_NAME")
fi

exec docker "${docker_arguments[@]}" \
  --user "$(id -u):$(id -g)" \
  --device=/dev/kfd \
  --device=/dev/dri \
  --group-add "$kfd_gid" \
  --group-add "$dri_gid" \
  --ipc=host \
  --env HOME=/tmp \
  --volume "$source_mount":/videos:ro \
  --volume "$output_root":/output \
  "$docker_image" "$@"
