# Hailo-8L model build

Hailo-8 and Hailo-8L require the Hailo Model Zoo 2.x line together with the
Hailo Dataflow Compiler 3.x line. HailoRT alone can execute an HEF but cannot
compile an ONNX graph. Obtain the compiler through the Hailo Developer Zone
and install it on a supported x86-64 Linux build host.

Export the exact GPU weights with an ONNX opset accepted by the installed
compiler:

```bash
python3 scripts/export-yolov8-onnx.py \
  --model /models/yolov8s.pt \
  --output "$HOME/models/yolov8s/yolov8s-hailo.onnx" \
  --opset 11
```

Use at least 512 camera-domain calibration images from rides outside the
benchmark pair, balanced across front/rear cameras, lighting and weather. Then
compile and preserve the generated provenance record:

```bash
scripts/compile-yolov8-hailo.sh \
  /models/yolov8s.pt \
  "$HOME/models/yolov8s/yolov8s-hailo.onnx" \
  "$HOME/models/yolov8s/calibration-512" \
  "$HOME/models/yolov8s/yolov8s-hailo8l.hef" \
  "$HOME/models/yolov8s/yolov8s-hailo8l.metadata.json"
```

Copy both HEF and metadata to the Hailo runtime host. Set
`PLATFORM_BENCH_HAILO_MODEL` to that HEF. The benchmark refuses a Hailo run if
HailoRT cannot override the score and IoU thresholds to the common protocol.

Do not describe a vendor HEF with unknown training-weight provenance as an
exact-weight comparison. It remains useful as a deployable workflow result,
but must be labelled separately.
