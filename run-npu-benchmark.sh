#!/usr/bin/env bash
set -euo pipefail

ryzen_ai_root=${RYZEN_AI_ROOT:-$HOME/.local/share/lemonade-linux-hybrid/ryzenai-1.7.1}
if [[ ! -x "$ryzen_ai_root/bin/python" ]]; then
  echo "Ryzen AI 1.7.1 Python environment is unavailable" >&2
  exit 1
fi

source /opt/xilinx/xrt/setup.sh >/dev/null
ryzen_ai_native="$ryzen_ai_root/lib/python3.12/site-packages/voe/lib"
ryzen_ai_ort="$ryzen_ai_root/lib/python3.12/site-packages/onnxruntime/capi"
export LD_LIBRARY_PATH="$ryzen_ai_native:$ryzen_ai_ort:/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
exec "$ryzen_ai_root/bin/python" "$(dirname "$0")/npu_benchmark.py" "$@"
