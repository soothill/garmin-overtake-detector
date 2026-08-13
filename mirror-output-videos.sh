#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/lib/runtime-config.sh"
SOURCE_DIR="${GARMIN_OUTPUT_SOURCE:-$output_root}"
PRODUCTION_BATCH="${GARMIN_OUTPUT_BATCH:-paired-parallel-v1}"
DESTINATION="${GARMIN_OUTPUT_DESTINATION:-}"
SSH_KEY="${GARMIN_OUTPUT_SSH_KEY:-$HOME/.ssh/garminoutput_mirror_ed25519}"
BANDWIDTH_KIB="${GARMIN_OUTPUT_BWLIMIT_KIB:-50000}"
LOCK_FILE="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/garmin-output-video-mirror.lock"
FILE_LIST=""

if [[ -z "$DESTINATION" ]]; then
    printf 'GARMIN_OUTPUT_DESTINATION must be set before mirroring\n' >&2
    exit 64
fi

cleanup() {
    [[ -z "$FILE_LIST" ]] || rm -f -- "$FILE_LIST"
}
trap cleanup EXIT

if [[ ! -d "$SOURCE_DIR" ]]; then
    printf 'Source directory does not exist: %s\n' "$SOURCE_DIR" >&2
    exit 66
fi

if [[ ! -r "$SSH_KEY" ]]; then
    printf 'Mirror SSH key is not readable: %s\n' "$SSH_KEY" >&2
    exit 77
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    printf 'A video mirror is already running; nothing to do.\n'
    exit 0
fi

FILE_LIST=$(mktemp)
python3 - "$SOURCE_DIR" "$PRODUCTION_BATCH" >"$FILE_LIST" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
combined = root / sys.argv[2] / "combined"
if combined.is_dir():
    for validation in sorted(combined.glob("*/validation.json")):
        try:
            payload = json.loads(validation.read_text(encoding="utf-8"))
            combined_payload = json.loads(
                (validation.parent / "combined.json").read_text(encoding="utf-8")
            )
            valid = (
                payload.get("valid") is True
                and payload.get("alignment_method") == "vehicle_handoff_clock_v2"
                and payload.get("layout") == "front-left_rear-right"
                and combined_payload.get("layout") == "front-left_rear-right"
            )
        except (OSError, json.JSONDecodeError):
            valid = False
        if not valid:
            continue
        for clip in sorted((validation.parent / "clips").glob("*.mp4")):
            if clip.is_file():
                print(clip.resolve().relative_to(root))

review_dir = root / sys.argv[2] / "reviewed-skipped"
validation_path = review_dir / "validation.json"
review_path = review_dir / "review.json"
if validation_path.is_file() and review_path.is_file():
    try:
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        report = json.loads(review_path.read_text(encoding="utf-8"))
        valid = (
            validation.get("valid") is True
            and validation.get("review_method") == "rear_vehicle_review_v1"
            and report.get("review_method") == "rear_vehicle_review_v1"
        )
    except (OSError, json.JSONDecodeError):
        valid = False
        report = {}
    if valid:
        for event in report.get("events") or []:
            if event.get("contains_vehicle") is not True:
                continue
            clip = Path(str(event.get("clip") or ""))
            if clip.parts[:2] == ("/", "output"):
                clip = root.joinpath(*clip.parts[2:])
            try:
                relative = clip.resolve().relative_to(root)
            except ValueError:
                continue
            if clip.is_file():
                print(relative)
PY

if [[ ! -s "$FILE_LIST" ]]; then
    printf 'No validated combined videos are ready to publish.\n'
    exit 0
fi

rsync \
    --recursive \
    --times \
    --whole-file \
    --prune-empty-dirs \
    --safe-links \
    --partial \
    --partial-dir=.rsync-partial \
    --delay-updates \
    --bwlimit="$BANDWIDTH_KIB" \
    --human-readable \
    --stats \
    --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
    --files-from="$FILE_LIST" \
    --relative \
    --rsh="ssh -i $SSH_KEY -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15" \
    "$SOURCE_DIR/" \
    "$DESTINATION"
