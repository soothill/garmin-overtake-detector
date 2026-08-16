#!/usr/bin/env bash

# Shared defaults for both interactive commands and systemd services. Every
# value may be overridden by the environment file installed by install.sh.
# shellcheck disable=SC2034  # Variables are consumed by the scripts that source this file.
project_root=${GARMIN_PROJECT_ROOT:-$HOME/garmin-overtake-detector}
output_root=${GARMIN_OVERTAKES_OUTPUT:-$project_root/output}
source_mount=${GARMIN_SOURCE_MOUNT:-/mnt/garmin}
front_dir=${GARMIN_FRONT_DIR:-varia-vue}
rear_dir=${GARMIN_REAR_DIR:-rct715}
docker_image=${GARMIN_DOCKER_IMAGE:-garmin-overtakes:rocm7.14-yolov8s}
source_exclude_file=${GARMIN_SOURCE_EXCLUDE_FILE:-}

source_is_excluded() {
  local source_path=$1
  [[ -n "$source_exclude_file" ]] || return 1
  awk -F '\t' -v source="$source_path" '
    /^[[:space:]]*(#|$)/ {next}
    NR == 1 && $1 == "source" {next}
    $1 == source {found = 1; exit}
    END {exit !found}
  ' "$source_exclude_file"
}
