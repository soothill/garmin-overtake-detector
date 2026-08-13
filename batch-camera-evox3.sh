#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# != 3 )); then
  echo "usage: $0 BATCH_NAME CAMERA MANIFEST" >&2
  exit 2
fi

batch_name=$1
camera=$2
manifest=$3
if [[ "$camera" != "front" && "$camera" != "rear" ]]; then
  echo "camera must be front or rear" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
batch_root="$output_root/$batch_name"
status_file="$batch_root/status-$camera.tsv"
attempt_log="$batch_root/attempts-$camera.tsv"
minimum_free_kib=$((${GARMIN_MINIMUM_FREE_GIB:-100} * 1024 * 1024))
heartbeat_timeout=${GARMIN_HEARTBEAT_TIMEOUT_SECONDS:-300}
max_source_failures=${GARMIN_MAX_SOURCE_FAILURES:-3}
container_name="garmin-overtakes-gpu-$camera"
current_source=""
current_job_pid=""
current_sleep_pid=""

write_status() {
  local state=$1
  local message=${2:-}
  local temporary="$status_file.tmp.$$"
  printf 'state\t%s\ntimestamp\t%s\nsource\t%s\nmessage\t%s\n' \
    "$state" "$(date --iso-8601=seconds)" "$current_source" "$message" >"$temporary"
  mv -- "$temporary" "$status_file"
}

record_attempt() {
  local state=$1
  local detail=${2:-}
  if [[ ! -f "$attempt_log" ]]; then
    printf 'timestamp\tstate\tsource\tdetail\n' >"$attempt_log"
  fi
  printf '%s\t%s\t%s\t%s\n' \
    "$(date --iso-8601=seconds)" "$state" "$current_source" "$detail" >>"$attempt_log"
}

stop_current_job() {
  if [[ -n "$current_sleep_pid" ]] && kill -0 "$current_sleep_pid" 2>/dev/null; then
    kill "$current_sleep_pid" 2>/dev/null || true
    wait "$current_sleep_pid" 2>/dev/null || true
  fi
  current_sleep_pid=""
  if [[ -n "$current_job_pid" ]] && kill -0 "$current_job_pid" 2>/dev/null; then
    kill -TERM "$current_job_pid" 2>/dev/null || true
    wait "$current_job_pid" 2>/dev/null || true
  fi
  current_job_pid=""
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}

finish() {
  local status=$?
  trap - EXIT
  stop_current_job
  if (( status == 0 )); then
    current_source=""
    write_status complete "$camera lane complete"
  else
    write_status failed "$camera lane exited with status $status"
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT TERM

validate_result() {
  local source=$1 duration=$2 size=$3 result_dir=$4 benchmark_dir=$5
  python3 "$script_dir/validate_batch_result.py" \
    --source "$source" --camera "$camera" --duration "$duration" --size "$size" \
    --result-dir "$result_dir" --benchmark-dir "$benchmark_dir" \
    --output-root "$output_root"
}

archive_partial_attempt() {
  local date_name=$1 result_dir=$2 benchmark_dir=$3 stem archive_dir
  stem=$(basename -- "$result_dir")
  archive_dir="$batch_root/failed-attempts/$camera/$date_name/${stem}-$(date +%Y%m%dT%H%M%S)"
  if [[ -e "$result_dir" || -e "$benchmark_dir" ]]; then
    mkdir -p "$archive_dir"
    [[ ! -e "$result_dir" ]] || mv -- "$result_dir" "$archive_dir/result"
    [[ ! -e "$benchmark_dir" ]] || mv -- "$benchmark_dir" "$archive_dir/benchmark"
    record_attempt archived "$archive_dir"
  fi
}

write_status running "starting $camera lane"
while IFS=$'\t' read -r row_camera date_name source duration size result_dir benchmark_dir; do
  [[ "$row_camera" == "$camera" ]] || continue
  current_source=$source
  benchmark_name=$(basename -- "$benchmark_dir")
  if [[ -f "$result_dir/run.json" && -f "$benchmark_dir/benchmark.json" ]] \
    && validate_result "$source" "$duration" "$size" "$result_dir" "$benchmark_dir" \
      >/dev/null; then
    write_status running "skipping validated $camera file for $date_name"
    continue
  fi

  failures=0
  if [[ -f "$attempt_log" ]]; then
    failures=$(awk -F '\t' -v wanted="$source" \
      'NR > 1 && $2 == "failed" && $3 == wanted {n++} END {print n + 0}' \
      "$attempt_log")
  fi
  if (( failures >= max_source_failures )); then
    write_status blocked "$source reached its retry limit"
    exit 65
  fi

  archive_partial_attempt "$date_name" "$result_dir" "$benchmark_dir"
  free_kib=$(df -Pk "$output_root" | awk 'NR == 2 {print $4}')
  if (( free_kib < minimum_free_kib )); then
    echo "less than 100 GiB remains under $output_root" >&2
    exit 1
  fi

  result_container="/output/${result_dir#"$output_root"/}"
  heartbeat_container="$result_container/progress.json"
  heartbeat_host="$result_dir/progress.json"
  job_log="$result_dir/pipeline.log"
  mkdir -p "$result_dir"
  write_status running "processing $camera recording for $date_name"
  record_attempt started "$camera $date_name"
  job_started=$(date +%s)
  GARMIN_OVERTAKES_CONTAINER_NAME="$container_name" \
  GARMIN_DISABLE_PER_FILE_POWER=1 \
    "$script_dir/benchmark-evox3.sh" "$benchmark_name" \
      --source "$source" --output-dir "$result_container" \
      --heartbeat-file "$heartbeat_container" --camera "$camera" --no-clips \
      > >(tee -a "$job_log") 2>&1 &
  current_job_pid=$!

  watchdog_status=0
  while kill -0 "$current_job_pid" 2>/dev/null; do
    sleep 15 &
    current_sleep_pid=$!
    wait "$current_sleep_pid" 2>/dev/null || true
    current_sleep_pid=""
    now=$(date +%s)
    if [[ -f "$heartbeat_host" ]]; then
      heartbeat_updated=$(stat -c %Y "$heartbeat_host")
      if (( now - heartbeat_updated > heartbeat_timeout )); then
        echo "$camera heartbeat exceeded $heartbeat_timeout seconds" >&2
        watchdog_status=124
        break
      fi
    elif (( now - job_started > heartbeat_timeout )); then
      echo "$camera pipeline did not create a heartbeat" >&2
      watchdog_status=124
      break
    fi
  done
  if (( watchdog_status != 0 )); then
    stop_current_job
    record_attempt failed "watchdog timeout"
    exit "$watchdog_status"
  fi

  job_status=0
  wait "$current_job_pid" || job_status=$?
  current_job_pid=""
  if (( job_status != 0 )); then
    record_attempt failed "pipeline status $job_status"
    exit "$job_status"
  fi
  if ! validate_result "$source" "$duration" "$size" "$result_dir" "$benchmark_dir"; then
    record_attempt failed "result validation failed"
    exit 1
  fi
  record_attempt completed "$camera $date_name"
  python3 "$script_dir/summarize_batch.py" \
    --manifest "$manifest" \
    --output-csv "$batch_root/summary.csv" \
    --output-json "$batch_root/summary.json" >/dev/null
done < <(tail -n +2 "$manifest")
