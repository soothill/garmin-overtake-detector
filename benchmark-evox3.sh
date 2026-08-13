#!/usr/bin/env bash
set -euo pipefail

if (( $# < 2 )); then
  echo "usage: $0 NAME PIPELINE_ARGUMENTS..." >&2
  exit 2
fi

benchmark_name=$1
shift
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
benchmark_dir="$output_root/benchmarks/$benchmark_name"
power_csv="$benchmark_dir/power.csv"
monitor_log="$benchmark_dir/power-monitor.log"
benchmark_json="$benchmark_dir/benchmark.json"
mkdir -p "$benchmark_dir"

if [[ -n "${GARMIN_OVERTAKES_CONTAINER_NAME:-}" ]]; then
  container_name=$GARMIN_OVERTAKES_CONTAINER_NAME
else
  container_name="garmin-${benchmark_name//[^a-zA-Z0-9_.-]/-}"
fi
export GARMIN_OVERTAKES_CONTAINER_NAME=$container_name

container_output=""
previous=""
for argument in "$@"; do
  if [[ "$previous" == "--output-dir" ]]; then
    container_output=$argument
    break
  fi
  previous=$argument
done

run_json=""
if [[ "$container_output" == /output/* ]]; then
  run_json="$output_root/${container_output#/output/}/run.json"
fi

monitor_pid=""
pipeline_pid=""
stop_monitor() {
  if [[ -n "$monitor_pid" ]] && kill -0 "$monitor_pid" 2>/dev/null; then
    kill "$monitor_pid"
    wait "$monitor_pid" 2>/dev/null || true
  fi
}

stop_pipeline() {
  if docker container inspect "$container_name" >/dev/null 2>&1; then
    timeout 25 docker stop --timeout 15 "$container_name" >/dev/null 2>&1 || true
  fi
  if [[ -n "$pipeline_pid" ]] && kill -0 "$pipeline_pid" 2>/dev/null; then
    kill "$pipeline_pid" 2>/dev/null || true
    wait "$pipeline_pid" 2>/dev/null || true
  fi
  pipeline_pid=""
}

handle_signal() {
  trap - INT TERM
  stop_pipeline
  stop_monitor
  exit 130
}

cleanup() {
  stop_pipeline
  stop_monitor
}
trap cleanup EXIT
trap handle_signal INT TERM

start_monitor() {
  local line_count
  for _ in 1 2 3; do
    rm -f -- "$power_csv"
    : >"$monitor_log"
    amd-smi metric -p -u --csv -w 1 --file "$power_csv" --overwrite \
      >/dev/null 2>"$monitor_log" &
    monitor_pid=$!
    for _ in {1..16}; do
      line_count=0
      if [[ -f "$power_csv" ]]; then
        line_count=$(wc -l <"$power_csv")
      fi
      if (( line_count >= 2 )); then
        return 0
      fi
      if ! kill -0 "$monitor_pid" 2>/dev/null; then
        break
      fi
      sleep 0.25
    done
    stop_monitor
    monitor_pid=""
  done
  echo "warning: AMD SMI telemetry did not start after 3 attempts" >&2
  return 0
}

start_epoch=$(date +%s)
if [[ "${GARMIN_DISABLE_PER_FILE_POWER:-0}" == "1" ]]; then
  printf 'timestamp\n' >"$power_csv"
else
  start_monitor
fi

if docker container inspect "$container_name" >/dev/null 2>&1; then
  echo "container $container_name already exists; refusing to start a duplicate" >&2
  exit 1
fi

"$(dirname "$0")/run-evox3.sh" "$@" &
pipeline_pid=$!
pipeline_status=0
wait "$pipeline_pid" || pipeline_status=$?
pipeline_pid=""
if (( pipeline_status != 0 )); then
  echo "video pipeline exited with status $pipeline_status" >&2
  exit "$pipeline_status"
fi

end_epoch=$(date +%s)
stop_monitor
monitor_pid=""

summary_args=(
  --csv "$power_csv"
  --start "$start_epoch"
  --end "$end_epoch"
  --output "$benchmark_json"
)
if [[ -n "$run_json" ]]; then
  summary_args+=(--run-json "$run_json")
fi
python3 "$(dirname "$0")/summarize_power.py" "${summary_args[@]}"
