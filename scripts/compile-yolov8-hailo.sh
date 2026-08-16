#!/usr/bin/env bash
set -euo pipefail

if (( $# != 5 )); then
  echo "usage: $0 SOURCE_WEIGHTS.pt SOURCE.onnx CALIBRATION_DIR OUTPUT.hef METADATA.json" >&2
  exit 2
fi

source_weights=$1
source_onnx=$2
calibration_dir=$3
output_hef=$4
metadata=$5
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

command -v hailomz >/dev/null || {
  echo "hailomz is unavailable; install Hailo Dataflow Compiler 3.x and Model Zoo 2.x for Hailo-8L" >&2
  exit 1
}
[[ -r "$source_weights" ]] || { echo "source weights are unreadable: $source_weights" >&2; exit 1; }
[[ -r "$source_onnx" ]] || { echo "source ONNX is unreadable: $source_onnx" >&2; exit 1; }
[[ -d "$calibration_dir" ]] || { echo "calibration directory is missing: $calibration_dir" >&2; exit 1; }

calibration_count=$(find "$calibration_dir" -maxdepth 1 -type f \( \
  -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \
\) -print | wc -l)
minimum_calibration=${HAILO_MINIMUM_CALIBRATION_IMAGES:-512}
if (( calibration_count < minimum_calibration )); then
  echo "need at least $minimum_calibration calibration images, found $calibration_count" >&2
  exit 1
fi

work_dir=$(mktemp -d)
cleanup() {
  rm -rf -- "$work_dir"
}
trap cleanup EXIT INT TERM

(
  cd "$work_dir"
  hailomz compile yolov8s \
    --ckpt "$source_onnx" \
    --calib-path "$calibration_dir" \
    --hw-arch hailo8l
)

compiled_hef=$(find "$work_dir" -maxdepth 2 -type f -name '*.hef' -print -quit)
[[ -n "$compiled_hef" ]] || { echo "hailomz did not produce an HEF" >&2; exit 1; }
mkdir -p -- "$(dirname -- "$output_hef")" "$(dirname -- "$metadata")"
install -m 0644 "$compiled_hef" "$output_hef"

if command -v hailortcli >/dev/null; then
  hailortcli parse-hef "$output_hef"
fi
python3 "$script_dir/write-hailo-model-metadata.py" \
  --source-weights "$source_weights" \
  --source-onnx "$source_onnx" \
  --calibration-dir "$calibration_dir" \
  --hef "$output_hef" \
  --output "$metadata"
