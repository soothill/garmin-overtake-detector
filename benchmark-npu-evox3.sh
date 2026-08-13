#!/usr/bin/env bash
set -euo pipefail

if (( $# < 2 )); then
  echo "usage: $0 NAME NPU_BENCHMARK_ARGUMENTS..." >&2
  exit 2
fi

benchmark_name=$1
shift
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
benchmark_dir="$output_root/benchmarks/$benchmark_name"
power_csv="$benchmark_dir/power.csv"
inference_json="$benchmark_dir/inference.json"
benchmark_json="$benchmark_dir/benchmark.json"
mkdir -p "$benchmark_dir"

monitor_pid=""
stop_monitor() {
  if [[ -n "$monitor_pid" ]] && kill -0 "$monitor_pid" 2>/dev/null; then
    kill "$monitor_pid"
    wait "$monitor_pid" 2>/dev/null || true
  fi
}
trap stop_monitor EXIT INT TERM

start_epoch=$(date +%s)
amd-smi metric -p -u --csv -w 1 --file "$power_csv" --overwrite >/dev/null &
monitor_pid=$!

"$(dirname "$0")/run-npu-benchmark.sh" "$@" --output "$inference_json"

end_epoch=$(date +%s)
stop_monitor
monitor_pid=""

python3 "$(dirname "$0")/summarize_power.py" \
  --csv "$power_csv" \
  --start "$start_epoch" \
  --end "$end_epoch" \
  --inference-json "$inference_json" \
  --output "$benchmark_json"
