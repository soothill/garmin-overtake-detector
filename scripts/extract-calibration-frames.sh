#!/usr/bin/env bash
set -euo pipefail

if (( $# != 5 )); then
  echo "usage: $0 SOURCE.mp4 OUTPUT_DIR PREFIX INTERVAL_SECONDS COUNT" >&2
  exit 2
fi

source_video=$1
output_dir=$2
prefix=$3
interval=$4
count=$5

mkdir -p "$output_dir"
for ((index = 0; index < count; index++)); do
  timestamp=$((index * interval + interval / 2))
  printf -v output_name '%s/%s-%03d.jpg' "$output_dir" "$prefix" "$index"
  ffmpeg -hide_banner -loglevel error -ss "$timestamp" -i "$source_video" \
    -frames:v 1 -vf 'scale=1280:-2' -q:v 3 -y "$output_name"
done
