#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# != 1 )); then
  echo "usage: $0 BATCH_NAME" >&2
  exit 2
fi
batch_name=$1
if [[ ! "$batch_name" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "invalid batch name" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
batch_root="$output_root/$batch_name"
manifest="$batch_root/manifest.tsv"
summary_csv="$batch_root/summary.csv"
summary_json="$batch_root/summary.json"
status_file="$batch_root/status.tsv"
attempt_log="$batch_root/attempts.tsv"
expected_front=${GARMIN_EXPECTED_FRONT_FILES:-0}
expected_rear=${GARMIN_EXPECTED_REAR_FILES:-0}
front_pid=""
rear_pid=""
composition_pid=""
monitor_pid=""
current_source=""
batch_start=$(date +%s)
power_csv="$batch_root/power.csv"
power_log="$batch_root/power-monitor.log"
detection_complete="$batch_root/detection.complete"

write_status() {
  local state=$1 message=${2:-}
  local temporary="$status_file.tmp.$$"
  printf 'state\t%s\ntimestamp\t%s\nsource\t%s\nmessage\t%s\n' \
    "$state" "$(date --iso-8601=seconds)" "$current_source" "$message" >"$temporary"
  mv -- "$temporary" "$status_file"
}

stop_all() {
  local pid
  for pid in "$front_pid" "$rear_pid" "$composition_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$front_pid" "$rear_pid" "$composition_pid"; do
    [[ -z "$pid" ]] || wait "$pid" 2>/dev/null || true
  done
  docker rm -f garmin-overtakes-gpu-front garmin-overtakes-gpu-rear \
    garmin-overtakes-composer >/dev/null 2>&1 || true
  docker ps -aq --filter 'name=^garmin-overtakes-composer-' \
    | xargs -r docker rm -f >/dev/null 2>&1 || true
  if [[ -n "$monitor_pid" ]] && kill -0 "$monitor_pid" 2>/dev/null; then
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true
  fi
}

finish() {
  local status=$?
  trap - EXIT
  stop_all
  if (( status == 0 )); then
    current_source=""
    write_status complete "parallel detection and combined clips validated"
  else
    write_status failed "parallel batch exited with status $status"
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT TERM

if [[ -f "$manifest" ]]; then
  GARMIN_ALLOW_ADDITIONAL_SOURCES=1 "$script_dir/preflight-evox3.sh"
else
  "$script_dir/preflight-evox3.sh"
fi
mkdir -p "$batch_root"
exec 9>"$batch_root/runner.lock"
if ! flock -n 9; then
  echo "batch $batch_name is already running" >&2
  exit 1
fi

if [[ ! -f "$manifest" ]]; then
  manifest_tmp="$manifest.tmp.$$"
  printf 'camera\tdate\tsource\tduration_seconds\tsize_bytes\tresult_dir\tbenchmark_dir\n' \
    >"$manifest_tmp"
  for camera in front rear; do
    [[ "$camera" == "front" ]] && camera_dir=$front_dir || camera_dir=$rear_dir
    while IFS= read -r -d '' source_host; do
      date_name=$(basename -- "$(dirname -- "$source_host")")
      filename=$(basename -- "$source_host")
      stem=${filename%.mp4}
      source_container="/videos/${source_host#"$source_mount"/}"
      probe=$(docker run --rm --volume "$source_mount":/videos:ro \
        --entrypoint ffprobe "$docker_image" \
        -v error -show_entries format=duration,size -of csv=p=0 "$source_container")
      IFS=, read -r duration_seconds size_bytes <<<"$probe"
      result_dir="$batch_root/$camera/$date_name/$stem"
      benchmark_dir="$output_root/benchmarks/$batch_name-$camera-$date_name"
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$camera" "$date_name" "$source_container" "$duration_seconds" \
        "$size_bytes" "$result_dir" "$benchmark_dir" >>"$manifest_tmp"
    done < <(find "$source_mount/$camera_dir" -type f -name '*.mp4' -print0 | sort -z)
  done
  rows=$(($(wc -l <"$manifest_tmp") - 1))
  front_rows=$(awk -F '\t' 'NR > 1 && $1 == "front" {count++} END {print count + 0}' "$manifest_tmp")
  rear_rows=$(awk -F '\t' 'NR > 1 && $1 == "rear" {count++} END {print count + 0}' "$manifest_tmp")
  if (( expected_front > 0 && front_rows != expected_front )); then
    echo "manifest contains $front_rows front files; expected $expected_front" >&2
    exit 1
  fi
  if (( expected_rear > 0 && rear_rows != expected_rear )); then
    echo "manifest contains $rear_rows rear files; expected $expected_rear" >&2
    exit 1
  fi
  echo "manifest created with $rows files ($front_rows front, $rear_rows rear)"
  mv -- "$manifest_tmp" "$manifest"
fi
rm -f -- "$detection_complete"

python3 "$script_dir/summarize_batch.py" --manifest "$manifest" \
  --output-csv "$summary_csv" --output-json "$summary_json" >/dev/null
write_status running "starting concurrent front and rear detection lanes"

: >"$power_log"
amd-smi metric -p -u --csv -w 1 --file "$power_csv" --overwrite \
  >/dev/null 2>"$power_log" &
monitor_pid=$!

"$script_dir/batch-camera-evox3.sh" "$batch_name" front "$manifest" &
front_pid=$!
"$script_dir/batch-camera-evox3.sh" "$batch_name" rear "$manifest" &
rear_pid=$!
"$script_dir/compose-ready-evox3.sh" "$batch_name" "$manifest" \
  "$detection_complete" &
composition_pid=$!

front_done=0
rear_done=0
while (( ! front_done || ! rear_done )); do
  sleep 15
  if ! kill -0 "$composition_pid" 2>/dev/null; then
    composition_status=0
    wait "$composition_pid" || composition_status=$?
    composition_pid=""
    echo "composition worker exited before detection completed with status $composition_status" >&2
    exit 1
  fi
  if (( ! front_done )) && ! kill -0 "$front_pid" 2>/dev/null; then
    front_status=0
    wait "$front_pid" || front_status=$?
    front_done=1
    front_pid=""
    if (( front_status != 0 )); then
      echo "front lane failed with status $front_status" >&2
      exit "$front_status"
    fi
  fi
  if (( ! rear_done )) && ! kill -0 "$rear_pid" 2>/dev/null; then
    rear_status=0
    wait "$rear_pid" || rear_status=$?
    rear_done=1
    rear_pid=""
    if (( rear_status != 0 )); then
      echo "rear lane failed with status $rear_status" >&2
      exit "$rear_status"
    fi
  fi
  write_status running "front and rear detection lanes active"
done

python3 "$script_dir/summarize_batch.py" --manifest "$manifest" \
  --output-csv "$summary_csv" --output-json "$summary_json" >/dev/null

touch -- "$detection_complete"
write_status running "detection complete; waiting for remaining combined clips"
composition_status=0
wait "$composition_pid" || composition_status=$?
composition_pid=""
if (( composition_status != 0 )); then
  echo "composition worker failed with status $composition_status" >&2
  exit "$composition_status"
fi

current_source=""
python3 - "$batch_root" "$attempt_log.tmp.$$" <<'PY'
import csv
import sys
from pathlib import Path

batch_root = Path(sys.argv[1])
output_path = Path(sys.argv[2])
fieldnames = ["timestamp", "state", "lane", "date", "source", "detail"]
rows = []

for lane in ("front", "rear"):
    path = batch_root / f"attempts-{lane}.tsv"
    if not path.exists():
        continue
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            rows.append(
                {
                    "timestamp": row.get("timestamp", ""),
                    "state": row.get("state", ""),
                    "lane": lane,
                    "date": "",
                    "source": row.get("source", ""),
                    "detail": row.get("detail", ""),
                }
            )

combined_path = batch_root / "attempts-combined.tsv"
if combined_path.exists():
    with combined_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            rows.append(
                {
                    "timestamp": row.get("timestamp", ""),
                    "state": row.get("state", ""),
                    "lane": "combined",
                    "date": row.get("date", ""),
                    "source": row.get("source", ""),
                    "detail": row.get("detail", ""),
                }
            )

rows.sort(key=lambda row: (row["timestamp"], row["lane"], row["source"]))
with output_path.open("w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
PY
mv -- "$attempt_log.tmp.$$" "$attempt_log"

batch_end=$(date +%s)
if [[ -n "$monitor_pid" ]] && kill -0 "$monitor_pid" 2>/dev/null; then
  kill "$monitor_pid" 2>/dev/null || true
  wait "$monitor_pid" 2>/dev/null || true
  monitor_pid=""
fi
python3 "$script_dir/summarize_power.py" --csv "$power_csv" \
  --start "$batch_start" --end "$batch_end" --output "$batch_root/batch-power.json" \
  >/dev/null
python3 "$script_dir/summarize_batch.py" --manifest "$manifest" \
  --output-csv "$summary_csv" --output-json "$summary_json" >/dev/null
