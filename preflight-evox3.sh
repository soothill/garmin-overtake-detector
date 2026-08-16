#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
expected_front=${GARMIN_EXPECTED_FRONT_FILES:-0}
expected_rear=${GARMIN_EXPECTED_REAR_FILES:-0}
allow_additional_sources=${GARMIN_ALLOW_ADDITIONAL_SOURCES:-0}
minimum_free_kib=$((${GARMIN_MINIMUM_FREE_GIB:-100} * 1024 * 1024))
allowed_output_root=${GARMIN_ALLOWED_OUTPUT_ROOT:-$project_root/output}

if [[ -n "$source_exclude_file" && ! -r "$source_exclude_file" ]]; then
  echo "source exclusion file is not readable: $source_exclude_file" >&2
  exit 1
fi

resolved_output=$(realpath -m -- "$output_root")
resolved_allowed=$(realpath -m -- "$allowed_output_root")
case "$resolved_output" in
  "$resolved_allowed" | "$resolved_allowed"/*) ;;
  *)
    echo "refusing output root outside $resolved_allowed: $resolved_output" >&2
    exit 1
    ;;
esac

# Trigger the systemd automount before checking the underlying NFS mount.
test -d "$source_mount/$front_dir"
nfs_options=$(
  findmnt -rn -t nfs4 -o TARGET,OPTIONS \
    | awk -v target="$source_mount" '$1 == target {print $2}'
)
if [[ -z "$nfs_options" || ",${nfs_options}," != *,ro,* ]]; then
  echo "$source_mount is not an active read-only NFS4 mount" >&2
  exit 1
fi

count_sources() {
  local camera_path=$1 source_host source_container count=0
  while IFS= read -r -d '' source_host; do
    source_container="/videos/${source_host#"$source_mount"/}"
    source_is_excluded "$source_container" && continue
    count=$((count + 1))
  done < <(find "$camera_path" -type f -name '*.mp4' -print0)
  printf '%s\n' "$count"
}

front_count=$(count_sources "$source_mount/$front_dir")
rear_count=$(count_sources "$source_mount/$rear_dir")
inventory_valid=1
if (( expected_front > 0 )); then
  if [[ "$allow_additional_sources" == 1 ]]; then
    (( front_count >= expected_front )) || inventory_valid=0
  else
    (( front_count == expected_front )) || inventory_valid=0
  fi
fi
if (( expected_rear > 0 )); then
  if [[ "$allow_additional_sources" == 1 ]]; then
    (( rear_count >= expected_rear )) || inventory_valid=0
  else
    (( rear_count == expected_rear )) || inventory_valid=0
  fi
fi
if (( ! inventory_valid )); then
  echo "unexpected source inventory: front=$front_count rear=$rear_count" >&2
  exit 1
fi

mkdir -p -- "$resolved_output"
probe_file="$resolved_output/.preflight-write-test.$$"
: >"$probe_file"
rm -f -- "$probe_file"

free_kib=$(df -Pk "$resolved_output" | awk 'NR == 2 {print $4}')
if (( free_kib < minimum_free_kib )); then
  echo "less than 100 GiB is available under $resolved_output" >&2
  exit 1
fi

for device in /dev/kfd /dev/dri/renderD128; do
  if [[ ! -r "$device" || ! -w "$device" ]]; then
    echo "GPU device is unavailable to user $(id -un): $device" >&2
    exit 1
  fi
done

docker info >/dev/null
docker image inspect "$docker_image" >/dev/null

kfd_gid=$(stat -c '%g' /dev/kfd)
dri_gid=$(stat -c '%g' /dev/dri/renderD128)
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --device=/dev/kfd \
  --device=/dev/dri \
  --group-add "$kfd_gid" \
  --group-add "$dri_gid" \
  --ipc=host \
  --env HOME=/tmp \
  --entrypoint python3 \
  "$docker_image" \
  -c 'import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))' \
  >/dev/null

representative=$(find "$source_mount/$front_dir" -type f -name '*.mp4' | sort | head -n 1)
dd if="$representative" of=/dev/null bs=4096 count=1 status=none

telemetry_file=$(mktemp)
telemetry_error=$(mktemp)
cleanup() {
  rm -f -- "$telemetry_file" "$telemetry_error"
}
trap cleanup EXIT
if ! timeout 10 amd-smi metric -p -u --csv >"$telemetry_file" 2>"$telemetry_error"; then
  echo "AMD SMI preflight failed: $(tr '\n' ' ' <"$telemetry_error")" >&2
  exit 1
fi
if (( $(wc -l <"$telemetry_file") < 2 )); then
  echo "AMD SMI preflight returned no metric sample" >&2
  exit 1
fi

echo "preflight passed: $front_count front + $rear_count rear files, $((free_kib / 1024 / 1024)) GiB free"
