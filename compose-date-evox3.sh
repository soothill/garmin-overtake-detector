#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# != 7 )); then
  echo "usage: $0 BATCH_NAME SLOT DATE FRONT_SOURCE FRONT_RESULT REAR_SOURCE REAR_RESULT" >&2
  exit 2
fi

batch_name=$1
slot=$2
date_name=$3
front_source=$4
front_result=$5
rear_source=$6
rear_result=$7
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
batch_root="$output_root/$batch_name"
combined_dir="$batch_root/combined/$date_name"
combined_container="/output/${combined_dir#"$output_root"/}"
status_file="$batch_root/status-combined-worker-$slot.tsv"
attempt_log="$batch_root/attempts-combined.tsv"
attempt_lock="$batch_root/attempts-combined.lock"
container_name="garmin-overtakes-composer-$slot"
alignment_version=vehicle_handoff_clock_v2
heartbeat_timeout=${GARMIN_HEARTBEAT_TIMEOUT_SECONDS:-300}
sources="$rear_source + $front_source"
compose_pid=""
sleep_pid=""
failure_recorded=0

write_status() {
  local state=$1 message=${2:-} temporary="$status_file.tmp.$$"
  printf 'state\t%s\ntimestamp\t%s\nworker\t%s\ndate\t%s\nsource\t%s\nmessage\t%s\n' \
    "$state" "$(date --iso-8601=seconds)" "$slot" "$date_name" "$sources" "$message" \
    >"$temporary"
  mv -- "$temporary" "$status_file"
}

record_attempt() {
  local state=$1 detail=${2:-}
  exec 8>>"$attempt_lock"
  flock 8
  if [[ ! -f "$attempt_log" ]]; then
    printf 'timestamp\tstate\tdate\tsource\tdetail\n' >"$attempt_log"
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date --iso-8601=seconds)" "$state" \
    "$date_name" "$sources" "$alignment_version: $detail" >>"$attempt_log"
  flock -u 8
  exec 8>&-
}

is_valid() {
  [[ -f "$combined_dir/validation.json" ]] && python3 -c \
    'import json,sys; p=json.load(open(sys.argv[1])); assert p.get("valid") is True and p.get("alignment_method")=="vehicle_handoff_clock_v2" and p.get("layout")=="front-left_rear-right"' \
    "$combined_dir/validation.json" 2>/dev/null
}

stop_current() {
  if [[ -n "$sleep_pid" ]] && kill -0 "$sleep_pid" 2>/dev/null; then
    kill "$sleep_pid" 2>/dev/null || true
    wait "$sleep_pid" 2>/dev/null || true
  fi
  sleep_pid=""
  if [[ -n "$compose_pid" ]] && kill -0 "$compose_pid" 2>/dev/null; then
    kill -TERM "$compose_pid" 2>/dev/null || true
    wait "$compose_pid" 2>/dev/null || true
  fi
  compose_pid=""
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}

archive_partial() {
  local archive
  [[ -e "$combined_dir" ]] || return 0
  is_valid && return 0
  archive="$batch_root/failed-attempts/combined/$date_name/$(date +%Y%m%dT%H%M%S)-worker$slot-$$"
  mkdir -p "$(dirname -- "$archive")"
  mv -- "$combined_dir" "$archive"
  record_attempt archived "$archive"
}

finish() {
  local status=$?
  trap - EXIT
  stop_current
  if (( status == 0 )); then
    write_status complete "$date_name validated"
  else
    archive_partial
    if (( status == 130 )); then
      record_attempt interrupted "worker $slot stopped; partial evidence archived"
      write_status interrupted "worker stopped; partial evidence archived"
    else
      if (( ! failure_recorded )); then
        record_attempt failed "worker $slot exited with status $status"
      fi
      write_status failed "worker exited with status $status"
    fi
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT TERM

archive_partial
mkdir -p "$combined_dir"
write_status running "creating synchronized combined clips"
record_attempt started "worker $slot combined $date_name"
GARMIN_COMPOSE_CONTAINER_NAME="$container_name" "$script_dir/compose-evox3.sh" \
  --date "$date_name" --front-source "$front_source" --rear-source "$rear_source" \
  --front-run "/output/${front_result#"$output_root"/}/run.json" \
  --rear-run "/output/${rear_result#"$output_root"/}/run.json" \
  --output-dir "$combined_container" \
  --heartbeat-file "$combined_container/progress.json" \
  >"$combined_dir/compose.log" 2>&1 &
compose_pid=$!
started=$(date +%s)
while kill -0 "$compose_pid" 2>/dev/null; do
  sleep 15 &
  sleep_pid=$!
  wait "$sleep_pid" 2>/dev/null || true
  sleep_pid=""
  now=$(date +%s)
  if [[ -f "$combined_dir/progress.json" ]]; then
    updated=$(stat -c %Y "$combined_dir/progress.json")
    if (( now - updated > heartbeat_timeout )); then
      stop_current
      record_attempt failed "worker $slot heartbeat timeout"
      failure_recorded=1
      exit 124
    fi
  elif (( now - started > heartbeat_timeout )); then
    stop_current
    record_attempt failed "worker $slot created no heartbeat"
    failure_recorded=1
    exit 124
  fi
done
compose_status=0
wait "$compose_pid" || compose_status=$?
compose_pid=""
if (( compose_status != 0 )); then
  record_attempt failed "worker $slot composer status $compose_status"
  failure_recorded=1
  exit "$compose_status"
fi
if ! python3 "$script_dir/validate_combined_result.py" \
    --result-dir "$combined_dir" --output-root "$output_root"; then
  record_attempt failed "worker $slot combined validation failed"
  failure_recorded=1
  exit 1
fi
record_attempt completed "worker $slot combined $date_name"
systemctl --user start --no-block garmin-output-video-mirror.service >/dev/null 2>&1 || true
