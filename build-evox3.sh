#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
docker build --tag garmin-overtakes:rocm7.14-yolov8s "$script_dir"
