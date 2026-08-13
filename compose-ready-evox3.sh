#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# != 3 )); then
  echo "usage: $0 BATCH_NAME MANIFEST DETECTION_COMPLETE_MARKER" >&2
  exit 2
fi

batch_name=$1
manifest=$2
detection_complete=$3
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
batch_root="$output_root/$batch_name"
status_file="$batch_root/status-combined.tsv"
attempt_log="$batch_root/attempts-combined.tsv"
worker_count=${GARMIN_COMPOSITION_WORKERS:-3}
max_date_failures=${GARMIN_MAX_COMPOSITION_FAILURES:-3}
alignment_version=vehicle_handoff_clock_v2
declare -a worker_pids worker_dates

if (( worker_count < 1 || worker_count > 8 )); then
  echo "GARMIN_COMPOSITION_WORKERS must be between 1 and 8" >&2
  exit 2
fi

is_valid() {
  local path=$1
  [[ -f "$path" ]] && python3 -c \
    'import json,sys; assert json.load(open(sys.argv[1])).get("valid") is True' \
    "$path" 2>/dev/null
}

is_combined_valid() {
  local path=$1
  [[ -f "$path" ]] && python3 -c \
    'import json,sys; p=json.load(open(sys.argv[1])); assert p.get("valid") is True and p.get("alignment_method")=="vehicle_handoff_clock_v2" and p.get("layout")=="front-left_rear-right"' \
    "$path" 2>/dev/null
}

count_valid() {
  find "$batch_root/combined" -mindepth 2 -maxdepth 2 -name validation.json -print0 \
    | xargs -0 -r -n1 python3 -c \
      'import json,sys
try:
 p=json.load(open(sys.argv[1])); value=p.get("valid") is True and p.get("alignment_method")=="vehicle_handoff_clock_v2" and p.get("layout")=="front-left_rear-right"
except (OSError,ValueError):
 value=False
print(int(value))' \
    | awk '{n+=$1} END {print n+0}'
}

write_status() {
  local state=$1 message=$2 active="" slot temporary="$status_file.tmp.$$"
  for ((slot=1; slot<=worker_count; slot++)); do
    if [[ -n "${worker_pids[$slot]:-}" ]]; then
      active+="${active:+, }worker $slot=${worker_dates[$slot]}"
    fi
  done
  printf 'state\t%s\ntimestamp\t%s\nworkers\t%s\nactive\t%s\nmessage\t%s\n' \
    "$state" "$(date --iso-8601=seconds)" "$worker_count" "$active" "$message" \
    >"$temporary"
  mv -- "$temporary" "$status_file"
}

stop_workers() {
  local slot pid
  for ((slot=1; slot<=worker_count; slot++)); do
    pid=${worker_pids[$slot]:-}
    [[ -z "$pid" ]] || kill -TERM "$pid" 2>/dev/null || true
  done
  for ((slot=1; slot<=worker_count; slot++)); do
    pid=${worker_pids[$slot]:-}
    [[ -z "$pid" ]] || wait "$pid" 2>/dev/null || true
  done
  for ((slot=1; slot<=worker_count; slot++)); do
    docker rm -f "garmin-overtakes-composer-$slot" >/dev/null 2>&1 || true
  done
}

finish() {
  local status=$?
  trap - EXIT
  stop_workers
  if (( status == 0 )); then
    write_status complete "all paired dates composed and validated"
  else
    write_status failed "parallel composition coordinator exited with status $status"
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT TERM

pairs_file="$batch_root/composition-pairs.tsv"
python3 - "$manifest" "$pairs_file.tmp.$$" <<'PY'
import csv,json,os,sys
from pathlib import Path
rows=list(csv.DictReader(open(sys.argv[1]),delimiter="\t"))
by={}
for row in rows:
    if row["camera"] in by.setdefault(row["date"],{}):
        raise SystemExit(f'duplicate {row["camera"]} manifest row for {row["date"]}')
    by[row["date"]][row["camera"]]=row
pairs=[]
for date,cameras in by.items():
    if not {"front","rear"} <= cameras.keys(): continue
    front,rear=cameras["front"],cameras["rear"]
    events=0
    try: events=int(json.loads((Path(rear["result_dir"])/"run.json").read_text()).get("candidate_events",0))
    except (OSError,ValueError): pass
    pairs.append((events,date,front["source"],front["result_dir"],rear["source"],rear["result_dir"]))
pairs.sort(key=lambda row:(-row[0],row[1]))
with open(sys.argv[2],"w",encoding="utf-8") as handle:
    for _,date,fs,fr,rs,rr in pairs: handle.write("\t".join((date,fs,fr,rs,rr))+"\n")
PY
mv -- "$pairs_file.tmp.$$" "$pairs_file"
expected_pairs=$(wc -l <"$pairs_file")
mkdir -p "$batch_root/combined"
write_status running "parallel composition coordinator started"

while true; do
  completed_pairs=$(count_valid)
  if (( completed_pairs == expected_pairs )); then
    break
  fi

  for ((slot=1; slot<=worker_count; slot++)); do
    pid=${worker_pids[$slot]:-}
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
      worker_status=0
      wait "$pid" || worker_status=$?
      worker_pids[slot]=""
      worker_dates[slot]=""
      if (( worker_status == 65 )); then exit 65; fi
    fi
  done

  for ((slot=1; slot<=worker_count; slot++)); do
    [[ -z "${worker_pids[$slot]:-}" ]] || continue
    while IFS=$'\t' read -r date_name front_source front_result rear_source rear_result; do
      is_combined_valid "$batch_root/combined/$date_name/validation.json" && continue
      active=0
      for ((check=1; check<=worker_count; check++)); do
        [[ "${worker_dates[$check]:-}" != "$date_name" ]] || active=1
      done
      (( active )) && continue
      is_valid "$front_result/validation.json" || continue
      is_valid "$rear_result/validation.json" || continue
      failures=0
      if [[ -f "$attempt_log" ]]; then
        failures=$(awk -F '\t' -v wanted="$date_name" -v version="$alignment_version:" \
          'NR>1 && $2=="failed" && $3==wanted && index($5,version)==1 {n++} END {print n+0}' "$attempt_log")
      fi
      if (( failures >= max_date_failures )); then
        write_status blocked "$date_name reached its composition retry limit"
        exit 65
      fi
      "$script_dir/compose-date-evox3.sh" "$batch_name" "$slot" "$date_name" \
        "$front_source" "$front_result" "$rear_source" "$rear_result" &
      worker_pids[slot]=$!
      worker_dates[slot]=$date_name
      break
    done <"$pairs_file"
  done

  active_count=0
  for ((slot=1; slot<=worker_count; slot++)); do
    [[ -z "${worker_pids[$slot]:-}" ]] || ((active_count+=1))
  done
  completed_pairs=$(count_valid)
  write_status running "$completed_pairs/$expected_pairs paired dates validated; $active_count workers active"
  if [[ -f "$detection_complete" ]] && (( active_count == 0 )); then
    echo "detection complete but no composable date can be scheduled" >&2
    exit 1
  fi
  sleep 10
done
