#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# != 3 )); then
  echo "usage: $0 DIRECTORY CLIENT_IP ro|rw" >&2
  exit 2
fi
directory=$(realpath -e -- "$1")
client_ip=$2
mode=$3
[[ "$mode" == ro || "$mode" == rw ]] || { echo "mode must be ro or rw" >&2; exit 2; }
[[ "$directory" == /* && "$directory" != / ]] || { echo "refusing unsafe export path" >&2; exit 2; }
[[ "$directory" != *[[:space:]]* ]] || { echo "export paths containing whitespace are unsupported" >&2; exit 2; }
[[ "$client_ip" =~ ^[0-9a-fA-F:.]+$ ]] || { echo "invalid client address" >&2; exit 2; }

line="$directory $client_ip($mode,sync,no_subtree_check,root_squash)"
sudo apt-get update
sudo apt-get install -y nfs-kernel-server
sudo cp --archive /etc/exports "/etc/exports.backup.$(date +%Y%m%dT%H%M%S)"
# A path may be exported to more than one explicitly named client.  Keep each
# client on its own auditable line instead of broadening access to a subnet.
if ! sudo grep -Fqx -- "$line" /etc/exports; then
  printf '%s\n' "$line" | sudo tee -a /etc/exports >/dev/null
fi
sudo exportfs -ra
sudo exportfs -v
