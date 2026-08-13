#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# < 1 || $# > 2 )); then
  echo "usage: $0 SERVER:/EXPORTED/PATH [MOUNT_POINT]" >&2
  exit 2
fi
source_export=$1
mount_point=${2:-/mnt/garmin}
[[ "$source_export" =~ ^[A-Za-z0-9._:-]+:/[A-Za-z0-9._/+:-]+$ ]] || { echo "source must look like server:/path" >&2; exit 2; }
[[ "$mount_point" == /mnt/* && "$mount_point" != /mnt ]] || { echo "mount point must be a specific path beneath /mnt" >&2; exit 2; }
[[ "$mount_point" != *[[:space:]]* ]] || { echo "mount points containing whitespace are unsupported" >&2; exit 2; }

options=ro,hard,vers=4.2,proto=tcp,nosuid,nodev,noexec,_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=10min,x-systemd.mount-timeout=30s
line="$source_export $mount_point nfs $options 0 0"

sudo apt-get update
sudo apt-get install -y nfs-common
sudo install -d -o root -g root -m 0755 "$mount_point"
sudo cp --archive /etc/fstab "/etc/fstab.backup.$(date +%Y%m%dT%H%M%S)"
if awk -v mount="$mount_point" '$2 == mount {found=1} END {exit !found}' /etc/fstab \
    && ! grep -Fqx -- "$line" /etc/fstab; then
  echo "$mount_point already has a different fstab entry; edit it explicitly" >&2
  exit 1
fi
if ! grep -Fqx -- "$line" /etc/fstab; then
  printf '%s\n' "$line" | sudo tee -a /etc/fstab >/dev/null
fi
sudo systemctl daemon-reload
unit=$(systemd-escape --path --suffix=automount "$mount_point")
sudo systemctl start "$unit"
find "$mount_point" -maxdepth 1 -mindepth 1 -print -quit >/dev/null
findmnt "$mount_point"
