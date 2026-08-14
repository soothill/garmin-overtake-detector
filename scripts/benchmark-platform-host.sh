#!/usr/bin/env bash
set -euo pipefail

if (( $# != 4 )); then
  echo "usage: $0 gpu|npu|hailo FRONT.mp4 REAR.mp4 OUTPUT_DIR" >&2
  exit 2
fi

backend=$1
front=$2
rear=$3
output_dir=$4
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

: "${PLATFORM_BENCH_SOURCE_ROOT:?set PLATFORM_BENCH_SOURCE_ROOT to the read-only input root}"
: "${PLATFORM_BENCH_WORK_ROOT:?set PLATFORM_BENCH_WORK_ROOT to the writable benchmark root}"

case "$output_dir" in
  "$PLATFORM_BENCH_WORK_ROOT"/*) ;;
  *) echo "output must be beneath PLATFORM_BENCH_WORK_ROOT" >&2; exit 1 ;;
esac
if [[ -e "$output_dir/result.json" ]]; then
  echo "a result already exists at $output_dir; refusing to overwrite evidence" >&2
  exit 1
fi
for source in "$front" "$rear"; do
  case "$source" in
    "$PLATFORM_BENCH_SOURCE_ROOT"/*|"$PLATFORM_BENCH_WORK_ROOT"/*) ;;
    *) echo "source is outside the declared source/work roots: $source" >&2; exit 1 ;;
  esac
  [[ -r "$source" ]] || { echo "source is unreadable: $source" >&2; exit 1; }
  if [[ "${PLATFORM_BENCH_ALLOW_WRITABLE_SOURCE:-0}" != "1" && -w "$source" ]]; then
    echo "source is writable; use a read-only mount (or explicitly allow a verified staged copy): $source" >&2
    exit 1
  fi
done

mkdir -p "$output_dir"
python3 "$script_dir/prepare_benchmark_cache.py" \
  --mode "${PLATFORM_BENCH_CACHE_MODE:-cold}" \
  --output "$output_dir/cache-precondition.json" "$front" "$rear"
monitor_pid=""
stop_monitor() {
  if [[ -n "$monitor_pid" ]] && kill -0 "$monitor_pid" 2>/dev/null; then
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true
  fi
  monitor_pid=""
}
trap stop_monitor EXIT INT TERM

idle_seconds=${PLATFORM_BENCH_IDLE_SECONDS:-30}
idle_start=""
idle_end=""
power_csv="$output_dir/power.csv"
power_json="$output_dir/power.json"
pi_health_status=0
if [[ "$backend" == "hailo" ]]; then
  python3 "$script_dir/check_pi_health.py" --mode before \
    --output "$output_dir/pi-health-before.json"
fi
if [[ "$backend" == "gpu" || "$backend" == "npu" ]]; then
  amd-smi metric -p -u --csv -w 1 --file "$power_csv" --overwrite \
    >"$output_dir/power-monitor.log" 2>&1 &
  monitor_pid=$!
  python3 "$script_dir/wait_for_amd_idle.py" \
    --csv "$power_csv" --output "$output_dir/idle-gate.json" \
    --window-samples "${PLATFORM_BENCH_IDLE_SAMPLES:-$idle_seconds}" \
    --timeout "${PLATFORM_BENCH_IDLE_TIMEOUT:-900}" \
    --package-mean-max "${PLATFORM_BENCH_IDLE_PACKAGE_MEAN_MAX:-30}" \
    --package-peak-max "${PLATFORM_BENCH_IDLE_PACKAGE_PEAK_MAX:-40}" \
    --gpu-mean-max "${PLATFORM_BENCH_IDLE_GPU_MEAN_MAX:-2}" \
    --gpu-peak-max "${PLATFORM_BENCH_IDLE_GPU_PEAK_MAX:-5}" \
    --npu-mean-max "${PLATFORM_BENCH_IDLE_NPU_MEAN_MAX:-0.5}" \
    --npu-peak-max "${PLATFORM_BENCH_IDLE_NPU_PEAK_MAX:-1}"
  read -r idle_start idle_end < <(
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["start_epoch"], d["end_epoch"])' \
      "$output_dir/idle-gate.json"
  )
elif [[ "$backend" == "hailo" ]]; then
  if [[ -z "${PLATFORM_BENCH_EXTERNAL_POWER_CSV:-}" ]]; then
    python3 "$script_dir/sample_pi_pmic.py" --output "$power_csv" \
      >"$output_dir/power-monitor.log" 2>&1 &
    monitor_pid=$!
  fi
  idle_start=$(date +%s.%N)
  sleep "$idle_seconds"
  idle_end=$(date +%s.%N)
fi

common=(
  --backend "$backend"
  --front "$front"
  --rear "$rear"
  --output-dir "$output_dir"
  --sample-fps "${PLATFORM_BENCH_SAMPLE_FPS:-5}"
  --detect-width 640
  --model-size 640
  --confidence "${PLATFORM_BENCH_CONFIDENCE:-0.20}"
  --iou "${PLATFORM_BENCH_IOU:-0.50}"
  --decoder-threads "${PLATFORM_BENCH_DECODER_THREADS:-0}"
  --progress-every "${PLATFORM_BENCH_PROGRESS_EVERY:-500}"
)
if [[ -n "${PLATFORM_BENCH_DURATION:-}" ]]; then
  common+=(--duration "$PLATFORM_BENCH_DURATION")
fi

case "$backend" in
  gpu)
    image=${PLATFORM_BENCH_GPU_IMAGE:-garmin-overtakes:rocm7.14-yolov8s}
    kfd_gid=$(stat -c '%g' /dev/kfd)
    dri_gid=$(stat -c '%g' /dev/dri/renderD128)
    command=(
      docker run --rm --name garmin-platform-benchmark-gpu
      --user "$(id -u):$(id -g)"
      --device=/dev/kfd --device=/dev/dri
      --group-add "$kfd_gid" --group-add "$dri_gid"
      --ipc=host --env HOME=/tmp
      --volume "$script_dir:/bench-code:ro"
      --volume "$PLATFORM_BENCH_SOURCE_ROOT:$PLATFORM_BENCH_SOURCE_ROOT:ro"
      --volume "$PLATFORM_BENCH_WORK_ROOT:$PLATFORM_BENCH_WORK_ROOT"
      --entrypoint python3 "$image" /bench-code/platform_video_benchmark.py
      "${common[@]}" --model /models/yolov8s.pt --decode vaapi
    )
    ;;
  npu)
    ryzen_ai_root=${RYZEN_AI_ROOT:-$HOME/.local/share/lemonade-linux-hybrid/ryzenai-1.7.1}
    npu_model=${PLATFORM_BENCH_NPU_MODEL:?set PLATFORM_BENCH_NPU_MODEL}
    npu_cache=${PLATFORM_BENCH_NPU_CACHE:-$PLATFORM_BENCH_WORK_ROOT/npu-cache}
    [[ -x "$ryzen_ai_root/bin/python" ]] || { echo "Ryzen AI runtime is unavailable" >&2; exit 1; }
    source /opt/xilinx/xrt/setup.sh >/dev/null
    export LD_LIBRARY_PATH="$ryzen_ai_root/lib/python3.12/site-packages/voe/lib:$ryzen_ai_root/lib/python3.12/site-packages/onnxruntime/capi:/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
    export PLATFORM_BENCH_MEDIA_IMAGE=${PLATFORM_BENCH_MEDIA_IMAGE:-garmin-overtakes:rocm7.14-yolov8s}
    command=(
      "$ryzen_ai_root/bin/python" "$script_dir/platform_video_benchmark.py"
      "${common[@]}" --model "$npu_model" --cache-dir "$npu_cache"
      --decode vaapi --ffmpeg "$script_dir/scripts/container-ffmpeg.sh"
      --ffprobe "$script_dir/scripts/container-ffprobe.sh"
    )
    ;;
  hailo)
    hailo_model=${PLATFORM_BENCH_HAILO_MODEL:-/usr/local/hailo/resources/models/hailo8l/yolov8s.hef}
    command=(
      python3 "$script_dir/platform_video_benchmark.py"
      "${common[@]}" --model "$hailo_model" --decode drm
    )
    ;;
  *) echo "unsupported backend: $backend" >&2; exit 2 ;;
esac

"${command[@]}" >"$output_dir/run.log" 2>&1
stop_monitor

if [[ "$backend" == "hailo" ]]; then
  python3 "$script_dir/check_pi_health.py" --mode after \
    --before "$output_dir/pi-health-before.json" \
    --output "$output_dir/pi-health-after.json" || pi_health_status=$?
fi

read -r run_start run_end < <(
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["timing"]["start_epoch"], d["timing"]["end_epoch"])' \
    "$output_dir/result.json"
)

if [[ "$backend" == "gpu" || "$backend" == "npu" ]]; then
  python3 "$script_dir/summarize_platform_power.py" \
    --format amd-smi --csv "$power_csv" \
    --idle-start "$idle_start" --idle-end "$idle_end" \
    --run-start "$run_start" --run-end "$run_end" --output "$power_json"
elif [[ -n "${PLATFORM_BENCH_EXTERNAL_POWER_CSV:-}" ]]; then
  python3 "$script_dir/summarize_platform_power.py" \
    --format external --csv "$PLATFORM_BENCH_EXTERNAL_POWER_CSV" \
    --idle-start "$idle_start" --idle-end "$idle_end" \
    --run-start "$run_start" --run-end "$run_end" --output "$power_json"
else
  python3 "$script_dir/summarize_platform_power.py" \
    --format external --external-scope pi_pmic_output_rails --csv "$power_csv" \
    --idle-start "$idle_start" --idle-end "$idle_end" \
    --run-start "$run_start" --run-end "$run_end" --output "$power_json"
fi

echo "completed $backend benchmark: $output_dir"
if (( pi_health_status != 0 )); then
  echo "Hailo benchmark completed but hardware health evidence is invalid" >&2
  exit "$pi_health_status"
fi
