#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
container_name=${GARMIN_COMPOSE_CONTAINER_NAME:-garmin-overtakes-composer}
dri_gid=$(stat -c '%g' /dev/dri/renderD128)

exec docker run --rm --name "$container_name" \
  --user "$(id -u):$(id -g)" \
  --device=/dev/dri \
  --group-add "$dri_gid" \
  --env HOME=/tmp \
  --volume "$source_mount":/videos:ro \
  --volume "$output_root":/output \
  --entrypoint python3 \
  "$docker_image" \
  /app/compose_paired_events.py "$@"
