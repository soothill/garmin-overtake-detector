# GPU, NPU and Hailo platform benchmark

## Purpose

The harness answers a practical question: which deployable platform can work
through the same paired-camera recording with the least measured energy, and
how long does it take?  It is intentionally separate from production clip
composition.  The timed workload includes:

1. decoding both original camera files at five frames per second;
2. scaling to 640 pixels wide and letterboxing to 640x640;
3. vehicle detection;
4. one shared deterministic tracker;
5. the production trajectory rules; and
6. atomic JSON reports.

Front and rear are processed sequentially on every platform.  File staging,
model installation and final clip composition are outside the timer.  Inputs
must be read-only and the result records their size, timestamp, ownership and
mode before and after the run.

The default precondition is a cold client file cache.  The runner uses
`posix_fadvise(..., DONTNEED)` on both clean read-only files before starting
the idle baseline.  This prevents a second backend on a high-memory host from
inheriting the first backend's cached ride.  Set `PLATFORM_BENCH_CACHE_MODE=warm`
only when all platforms can be warmed equivalently; that pre-read is excluded
from the timer and recorded in `cache-precondition.json`.

## Fairness and limitations

The workload around the detector is identical.  Two result sets are retained:
the original deployable models, and a controlled GPU/NPU run which starts from
the exact same YOLOv8s weights.

| Backend | Model | Runtime |
|---|---|---|
| Radeon GPU | YOLOv8s FP16 | ROCm PyTorch / Ultralytics |
| Ryzen AI NPU | exact GPU YOLOv8s source, Quark XINT8 | Vitis AI Execution Provider |
| Hailo-8L | quantized YOLOv8s HEF | HailoRT |

The GPU/NPU pair has exact source-weight provenance.  The Hailo artifact has
the same YOLOv8s architecture, but its vendor HEF does not expose the original
training-weight hash.  The result remains a **platform workflow comparison**,
not an isolated-silicon comparison.  Always compare vehicle-detection and
candidate-event counts alongside energy.  A backend which saves energy by
missing much of the workload has not produced an equivalent result.

The decoder contract is explicitly RGB24.  The GPU adapter converts that RGB
array once to the BGR array expected by the Ultralytics Python API; Ultralytics
then performs its normal model-facing RGB conversion.  The NPU and Hailo
adapters receive RGB directly.  Result metadata records all three stages.

Every backend applies 0.10 as the low-confidence continuation threshold, 0.20
as the threshold for starting a vehicle track, and class-wise NMS at IoU 0.50.
The Hailo adapter must successfully override the HEF's embedded NMS values at
runtime or the benchmark aborts.  The shared tracker associates all four COCO
vehicle classes together and assigns the final class by confidence-weighted
majority, preventing car/truck class changes from splitting one vehicle.
Trajectory decisions use a three-frame rolling median for area, horizontal
position and bottom-edge position once a track contains at least five samples.
This prevents one malformed endpoint box from deciding growth, centreline or
radial-motion checks while preserving the original rules for very short tracks.

Power boundaries are also explicit:

- AMD SMI supplies APU socket, GPU and NPU power.  Gross and idle-adjusted
  energy are reported.  Before work begins, the AMD runner requires a clean
  30-sample idle window across all three channels.  The default mean/peak
  limits are 30/40 W package, 2/5 W GPU and 0.5/1 W NPU.  It saves the accepted
  interval in `idle-gate.json` and aborts after 15 minutes rather than using a
  contaminated baseline.  The limits are environment variables when a host's
  measured idle state has been characterized differently.
- On Raspberry Pi 5, `vcgencmd pmic_read_adc` supplies the sum of internal PMIC
  output rails.  It includes the processor, memory and 3.3 V system rail used
  by the Hailo module, but excludes PSU conversion loss and some 5 V loads.
- This Hailo carrier exposes the HailoRT power commands but its firmware
  rejects them as unsupported.  The harness does not replace a missing
  measurement with a datasheet typical value.
- The Hailo runner records Pi voltage, temperature and clock state before and
  after the benchmark.  It searches the kernel journal for any new
  undervoltage or throttling event and exits unsuccessfully after preserving
  the timing and power reports if one occurred.  Historical sticky bits alone
  do not invalidate a later clean run.
- A definitive whole-system ranking requires the same external AC or DC wall
  meter for all three runs.  Its CSV must contain `timestamp,power_watts`.

`summarize_platform_results.py` only declares an energy ranking when at least
two complete power files use the same measurement scope.  This permits a valid
GPU-versus-NPU package-energy comparison on one APU while correctly refusing a
formal three-way ranking when the Hailo run uses the Pi's internal PMIC rails.
An AMD power result is incomplete without a valid `idle-gate.json`; a Hailo
health failure likewise invalidates its power evidence.
Its event agreement is relative to the GPU run by default, within two seconds;
that is a consistency measure, not manually labelled ground truth.

## Representative pair selection

Choose a normal full ride rather than the shortest physical file.  Record:

- front/rear path, duration and byte size;
- why the pair is representative;
- source file metadata before the first run;
- model hashes or exact model release; and
- any production result used as a quality reference.

The first reference protocol uses a roughly 95-minute file from each camera,
totalling about 3.17 source-hours.  The production GPU result for that pair
contained 55 front and 68 rear candidates, but the benchmark uses a common
tracker, so that reference is a sanity check rather than an exact expected
count.

The selector makes this choice deterministic.  It searches dates that contain
both cameras and chooses the pair whose mean duration is closest to the target,
then prefers the closest front/rear duration match:

```bash
python3 select_benchmark_pair.py --source-root /mnt/garmin \
  --target-minutes 90 --output results/RUN/pair.json
```

Pass `--date YYYY-MM-DD` to reproduce a named ride.  Keep `pair.json` with the
benchmark evidence; it records paths, durations, byte sizes, codecs and frame
dimensions.
If FFprobe only exists in the media container, export
`PLATFORM_BENCH_SOURCE_ROOT`, `PLATFORM_BENCH_WORK_ROOT` and
`PLATFORM_BENCH_MEDIA_IMAGE`, then add
`--ffprobe ./scripts/container-ffprobe.sh`.
Dates with more than one file for either camera are rejected by default because
similar durations do not prove that the recordings overlap.  Confirm the files
first and pass `--allow-multiple-per-camera`, or supply the two paths directly
to the host runner.

## Host preparation

The existing ROCm container supplies the GPU runtime and media tools.  The NPU
host needs AMD's Ryzen AI 1.7.1 environment and XRT.  To retain the original
vendor-model baseline, point it at that graph.  For the controlled run, build
the Quark XINT8 graph from the exact GPU weights as described in
[NPU.md](NPU.md), then set:

```bash
export PLATFORM_BENCH_NPU_MODEL="$HOME/models/yolov8s/yolov8s-xint8.onnx"
export PLATFORM_BENCH_NPU_CACHE="$HOME/platform-benchmark/npu-cache"
export PLATFORM_BENCH_NPU_CACHE_KEY=paired-yolov8s-xint8-v1
```

The Hailo host needs HailoRT, its Python package, FFmpeg and the YOLOv8s HEF.
Verify before a run:

```bash
hailortcli fw-control identify
hailortcli parse-hef /usr/local/hailo/resources/models/hailo8l/yolov8s.hef
ffmpeg -hide_banner -hwaccels
```

Mount the archive read-only on both hosts.  Grant each host separately rather
than exporting to a whole subnet:

```bash
# On the NAS, once per client address
./scripts/configure-nfs-export.sh /hdd2/garmin CLIENT_IP ro

# On each processing host
./scripts/configure-nfs-client.sh NAS_HOST:/export/garmin /mnt/garmin
```

For Hailo, using the read-only NFS source avoids copying tens of gigabytes to a
microSD card.  Staging, if required, must finish and be verified before the
timed run.  The runner refuses writable inputs by default; a separately
verified staged copy needs `PLATFORM_BENCH_ALLOW_WRITABLE_SOURCE=1`, while the
original archive must remain untouched.

## Run one platform

Set roots rather than granting the runner broad filesystem access:

```bash
export PLATFORM_BENCH_SOURCE_ROOT=/mnt/garmin
export PLATFORM_BENCH_WORK_ROOT="$HOME/platform-benchmark"
export PLATFORM_BENCH_IDLE_SECONDS=30
```

FFmpeg chooses its decoder thread count automatically by default.  A
power-constrained carrier can set `PLATFORM_BENCH_DECODER_THREADS`, but that
becomes part of the recorded platform configuration and must be disclosed in
the result.  Do not accept a run that reports new undervoltage events.

Then run each backend on its own host, sequentially:

```bash
./scripts/benchmark-platform-host.sh gpu \
  /mnt/garmin/varia-vue/YYYY-MM-DD/front.mp4 \
  /mnt/garmin/rct715/YYYY-MM-DD/rear.mp4 \
  "$PLATFORM_BENCH_WORK_ROOT/results/RUN/gpu"

./scripts/benchmark-platform-host.sh npu \
  /mnt/garmin/varia-vue/YYYY-MM-DD/front.mp4 \
  /mnt/garmin/rct715/YYYY-MM-DD/rear.mp4 \
  "$PLATFORM_BENCH_WORK_ROOT/results/RUN/npu"

./scripts/benchmark-platform-host.sh hailo \
  /mnt/garmin/varia-vue/YYYY-MM-DD/front.mp4 \
  /mnt/garmin/rct715/YYYY-MM-DD/rear.mp4 \
  "$PLATFORM_BENCH_WORK_ROOT/results/RUN/hailo"
```

For a smoke test only, set `PLATFORM_BENCH_DURATION=30`.  Never compare a
short warm-up-dominated test as the final efficiency result.

Each output contains `result.json`, per-camera run/event/track reports,
`cache-precondition.json`, `power.csv`, `power.json`, `power-monitor.log` and
`run.log`.  AMD outputs also contain `idle-gate.json`; Hailo outputs contain
`pi-health-before.json` and `pi-health-after.json`.  Existing
`result.json` evidence is never overwritten.
The result includes the detector model's SHA-256 hash; the GPU result also
records the parameter dtype observed after inference.

## External wall meter

Run the meter continuously across the idle and processing interval and export
UTC epoch samples:

```csv
timestamp,power_watts
1786600000.0,7.42
1786600001.0,7.51
```

Set `PLATFORM_BENCH_EXTERNAL_POWER_CSV` for the Hailo host.  For a formal
three-way whole-system result, summarize equivalent meter files for GPU and
NPU with:

```bash
python3 summarize_platform_power.py \
  --format external --csv meter.csv \
  --run-start START_EPOCH --run-end END_EPOCH \
  --external-scope whole_system_wall --output power-wall.json
```

## Aggregate and interpret

```bash
python3 summarize_platform_results.py \
  --result gpu:results/RUN/gpu/result.json \
  --result npu:results/RUN/npu/result.json \
  --result hailo:results/RUN/hailo/result.json \
  --power gpu:results/RUN/gpu/power.json \
  --power npu:results/RUN/npu/power.json \
  --power hailo:results/RUN/hailo/power.json \
  --health hailo:results/RUN/hailo/pi-health-after.json \
  --output-json results/RUN/comparison.json \
  --output-csv results/RUN/comparison.csv
```

Inspect at least:

- total wall minutes and real-time factor;
- gross and idle-adjusted Wh;
- Wh per source-hour;
- telemetry coverage;
- front/rear vehicle detections and candidate events;
- event agreement against the named reference backend;
- unchanged source evidence; and
- model/runtime versions.

Generate the detailed class, confidence, track-fragmentation and event-timing
comparison with:

```bash
python3 analyze_platform_detection.py \
  --result gpu:results/RUN/gpu/result.json \
  --result npu:results/RUN/npu/result.json \
  --result hailo:results/RUN/hailo/result.json \
  --output results/RUN/detection-analysis.json
```

### Detection agreement and parity

The aggregate JSON contains the matched and unmatched event evidence for each
camera.  With exactly three platforms it also calculates a **two-of-three
consensus**: an event must be reported by at least two backends to enter that
set.  This removes some reference-model bias, but it is still a proxy rather
than labelled truth.  Review unmatched timestamps against the source video
before calling them false positives or misses.
The report also divides wall time and measured energy by the consensus events
that each platform actually supported.  This quality-adjusted view prevents a
backend from appearing efficient merely because it returned less useful work.

Build a blind review set after all three results exist. This includes every
disagreement component and an evenly distributed sample of events reported by
all three platforms:

```bash
python3 prepare_platform_review.py \
  --result gpu:results/RUN/gpu/result.json \
  --result npu:results/RUN/npu/result.json \
  --result hailo:results/RUN/hailo/result.json \
  --source front:/mnt/garmin/varia-vue/DATE/front.mp4 \
  --source rear:/mnt/garmin/rct715/DATE/rear.mp4 \
  --output-dir results/RUN/review --extract-clips
```

Review clips without using the `platforms` field as a quality cue. Fill
`vehicle_present` and `overtake_pass` with `yes`, `no`, or `uncertain`, then
calculate precision, recall and F1 within the reviewed candidate universe:

```bash
python3 score_platform_labels.py \
  --labels results/RUN/review/labels.csv \
  --output results/RUN/review/scores.json
```

For faster first-pass review, render three timepoints per event into labelled
contact sheets. Use the original clips whenever the three frames are
ambiguous:

```bash
python3 render_platform_review_sheets.py \
  --labels results/RUN/review/labels.csv \
  --source front:/mnt/garmin/varia-vue/DATE/front.mp4 \
  --source rear:/mnt/garmin/rct715/DATE/rear.mp4 \
  --selection-reason disagreement \
  --output-dir results/RUN/review/sheets
```

These scores do not measure passes missed by all three platforms. Add randomly
sampled source intervals before claiming population-level recall.

Even with exact source weights, quantization, execution-provider partitioning,
preprocessing, output decoding and non-maximum suppression can move an event
across the common threshold.  To make output quality comparable:

1. create a small human-labelled validation set from representative day,
   night, rain, close-pass and partial-occlusion footage;
2. start every compiler from the same trained model architecture and weights;
3. use the same resize, colour conversion, class mapping and NMS rules;
4. use quantization-aware training and a calibration set drawn from the camera
   footage for the NPU and Hailo exports;
5. sweep confidence thresholds per backend against the labelled set, choosing
   thresholds that meet the same recall target rather than forcing the same
   numerical threshold; and
6. send borderline or single-backend events through a common second-pass model
   when equal recall matters more than minimum energy.

Report both the fixed-threshold result (hardware/workflow comparability) and
the calibrated-threshold result (deployment quality parity).  Do not tune on
the benchmark pair and then present that same pair as independent validation.

Repeat each run at least three times, alternate platform order, and use the
median for a publication-quality conclusion.  One complete run is useful for
engineering direction, but it does not quantify run-to-run variance.

## Measured hardened parity engineering result

One complete run used a roughly 95-minute file from each camera: 3.168 source
hours in total. The fixed protocol was 5 fps, 640-pixel detection width,
640x640 model input, 0.10 continuation confidence, 0.20 track-start
confidence, 0.50 IoU and a 1.4-second track gap. Both AMD runs passed a clean
30-sample idle gate and used the same APU package power boundary.

| Platform | Wall time | Real-time factor | Gross energy | Wh/source-hour | Candidates | Two-of-three consensus coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Radeon GPU YOLOv8s | 18.43 min | 10.389x | 20.89 Wh package | 6.593 | 158 | 91.14% |
| Ryzen AI NPU YOLOv8s XINT8 | 27.52 min | 6.909x | 40.39 Wh package | 12.749 | 180 | 92.41% |
| Hailo-8L YOLOv8s HEF | 63.41 min | 2.998x | 4.83 Wh Pi PMIC rails | 1.525 | 176 | 94.30% |

For the directly comparable GPU/NPU result, the GPU finished 33.02% sooner and
used 48.28% less gross package energy (50.42% less after subtracting each idle
baseline). It was therefore both faster and more energy-efficient for this
end-to-end workload. The Hailo energy is deliberately excluded from that
ranking because its internal Pi rail measurement is not the same boundary.

The three backends produced 158 two-of-three consensus event clusters. GPU,
NPU and Hailo supported 144, 146 and 149 respectively. A blind review of all
110 disagreements plus 20 sampled unanimous events produced 107 yes/no labels
and left 23 uncertain. In that deliberately difficult review set, GPU, NPU
and Hailo F1 was 0.683, 0.708 and 0.615. Many Hailo-only events were parked
vehicles, cross traffic or duplicate tracks; its higher candidate total did
not mean better pass accuracy.

The NPU core averaged only 1.95 W, while the complete APU package averaged
88.06 W. ONNX Runtime also reported that some shape/post-processing nodes were
assigned to CPU. This shows that the standard ONNX deployment leaves
substantial work around the compiled XINT8 subgraph. A raw-head,
compiler-friendly export and reduced CPU post-processing are the next
efficiency experiments. Direct NPU watts must not be reported as
whole-workflow efficiency.

Confidence-only tuning on this same review set suggested approximately 0.75
for GPU and 0.78 for NPU, improving precision with small recall losses. It did
little for Hailo. These are exploratory overfit values and are not deployment
defaults; validate them on a separately labelled ride. Hailo also needs an
exact-source-weight HEF before remaining detector differences can be
attributed to hardware or quantization rather than the vendor model.

The full result also needs repeated, alternating-order trials before quoting a
confidence interval.
