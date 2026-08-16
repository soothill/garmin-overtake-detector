# Ryzen AI NPU experiment

The repository includes `npu_benchmark.py`, `npu_detect_frames.py`,
`run-npu-benchmark.sh` and `benchmark-npu-evox3.sh` to preserve the evaluated
NPU path. They are not called by the production batch.

Set `RYZEN_AI_ROOT` to a working Ryzen AI/Lemonade runtime directory and run a
small representative benchmark before considering integration. Runtime setup
and supported operators change more quickly than the GPU container, so the
vendor Ryzen AI/XRT installation remains an explicit prerequisite.

The detector accepts both the older three-output AMD YOLO graphs and standard
Ultralytics one-output graphs shaped like `[1,84,8400]`.  NCHW and NHWC image
inputs are detected from model metadata and recorded in the result.

## Build the same YOLOv8s model used by the GPU

The repository makes the conversion reproducible without publishing third-party
weights.  First export your exact GPU weights:

```bash
python3 scripts/export-yolov8-onnx.py \
  --model /path/to/yolov8s.pt \
  --output "$HOME/models/yolov8s/yolov8s.onnx"
```

Extract calibration frames from rides other than the benchmark and validation
pairs. A balanced 512-frame set uses 256 front and 256 rear images; add other
rides when one recording does not cover the required lighting and weather:

```bash
scripts/extract-calibration-frames.sh \
  FRONT.mp4 "$HOME/models/yolov8s/calibration" front 20 256
scripts/extract-calibration-frames.sh \
  REAR.mp4 "$HOME/models/yolov8s/calibration" rear 20 256
```

Build the pinned AMD Quark environment and quantize the detector body.  The
final YOLO box decoder remains floating point, matching AMD's recommended
object-detection boundary:

```bash
docker build -t garmin-quark-quantizer:0.12 docker/quark-quantizer
docker run --rm \
  -v "$PWD:/work:ro" \
  -v "$HOME/models/yolov8s:/models" \
  garmin-quark-quantizer:0.12 \
  python /work/scripts/quantize-yolov8-npu.py \
    --input /models/yolov8s.onnx \
    --output /models/yolov8s-xint8.onnx \
    --calibration-dir /models/calibration
```

Preserve the generated metadata JSON: it records source and
quantized hashes, calibration filenames and graph settings.  Compile with a
fresh Vitis cache key whenever the model changes.

The decision boundary is end-to-end efficiency, not the accelerator's quoted
TOPS. Include decode, resize, tensor conversion, unsupported-operation fallback,
tracking and clip encoding in any comparison. In the hardened full-pair run,
direct NPU power averaged only 1.95 W but APU package power averaged 88.06 W:
CPU work around the compiled subgraph dominated.  On the tested EVO-X3, ROCm
GPU inference plus VAAPI remained the faster and more efficient production
route even after detection agreement was brought close to the other platforms.
