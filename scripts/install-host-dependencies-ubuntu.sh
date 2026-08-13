#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $(id -u) == 0 ]]; then
  echo "run this as your normal user; it invokes sudo where required" >&2
  exit 2
fi

sudo apt-get update
sudo apt-get install -y \
  docker.io ffmpeg jq nfs-common openssh-client python3 rsync \
  tesseract-ocr util-linux vainfo
sudo systemctl enable --now docker
sudo usermod -aG docker,render,video "$USER"
sudo loginctl enable-linger "$USER"

cat <<'EOF'
Host packages are installed.

You must also install an AMD kernel driver/ROCm host stack that supports your
GPU and provides /dev/kfd, /dev/dri/renderD128 and amd-smi. Reboot or log out
and back in after the group change, then run ./install.sh.
EOF
