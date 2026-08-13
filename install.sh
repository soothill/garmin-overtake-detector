#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install_root="$HOME/garmin-overtake-detector"
config_dir="$HOME/.config/garmin-overtake-detector"
unit_dir="$HOME/.config/systemd/user"
skip_build=0

if [[ "${1:-}" == "--skip-build" ]]; then
  skip_build=1
elif (( $# )); then
  echo "usage: $0 [--skip-build]" >&2
  exit 2
fi

for command in docker systemctl realpath; do
  command -v "$command" >/dev/null || {
    echo "missing required command: $command" >&2
    echo "run scripts/install-host-dependencies-ubuntu.sh first" >&2
    exit 1
  }
done

if [[ "$script_dir" != "$install_root" ]]; then
  if [[ -e "$install_root" || -L "$install_root" ]]; then
    echo "$install_root already exists and is not this checkout" >&2
    echo "clone the repository there or remove the conflicting path" >&2
    exit 1
  fi
  ln -s "$script_dir" "$install_root"
  echo "linked $install_root -> $script_dir"
fi

mkdir -p "$config_dir" "$unit_dir" "$install_root/output"
if [[ ! -e "$config_dir/environment" ]]; then
  cp "$install_root/config/environment.example" "$config_dir/environment"
  chmod 600 "$config_dir/environment"
  echo "created $config_dir/environment; review it before starting a batch"
fi

find "$install_root" -maxdepth 2 -type f -name '*.sh' -exec chmod 0755 {} +
for unit in "$install_root"/systemd/*.{service,timer}; do
  [[ -e "$unit" ]] || continue
  ln -sfn "$unit" "$unit_dir/$(basename "$unit")"
done
systemctl --user daemon-reload

if (( ! skip_build )); then
  "$install_root/build-evox3.sh"
fi

echo
echo "Installation complete. Next steps:"
echo "  1. Edit $config_dir/environment"
echo "  2. Mount the source archive read-only (docs/NFS.md)"
echo "  3. Run $install_root/preflight-evox3.sh"
echo "  4. Start with: systemctl --user start garmin-overtakes-gpu-all.service"
