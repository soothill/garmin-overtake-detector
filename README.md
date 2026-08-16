# Garmin Overtake Detector

Turn long front- and rear-camera cycling recordings into short, synchronized
vehicle-pass clips on an AMD Strix Halo workstation.

The pipeline detects vehicle trajectories independently in both cameras,
matches the same physical vehicle moving from rear to front, and creates a
single 2560x720 H.264/AAC clip with **front on the left and rear on the right**.
It is designed for unattended archives: inputs stay read-only, front and rear
detection run in parallel, composition starts as soon as a date is ready, and
every result must validate before it can be published.

> This is a community project, not a Garmin or AMD product. It was developed
> for Garmin Varia Vue and Varia RCT715 files, but the detector works from MP4
> video and can be adapted to other paired cameras.

## What it does

- Detects cars, motorcycles, buses and trucks with YOLOv8s and BoT-SORT.
- Uses trajectory rules to distinguish likely passes from cross traffic and
  vehicles moving in the wrong direction.
- Processes front and rear recordings concurrently on a Radeon 8060S GPU.
- Matches rear disappearances to front appearances using vehicle-event
  sequences plus OCR of the cameras' burned-in clocks.
- Handles camera clock bias and missing recording sections with a per-event
  media offset instead of forcing equal displayed timestamps.
- Creates 45-second clips by default: 20 seconds before the rear event and 25
  seconds after it.
- Runs three composition workers concurrently and begins composing a date as
  soon as both camera results validate.
- Writes atomic reports, progress heartbeats, retry histories and validation
  evidence; a five-minute watchdog catches hung work.
- Optionally reviews defensible rear-only events and mirrors only validated
  media to another server.

## Hardware and software target

The tested target is Ubuntu 24.04 on a Ryzen AI MAX+ 395 / Radeon 8060S
(`gfx1151`) with 128 GB unified memory, ROCm 7.14 host support, Docker and a
VAAPI-capable render node. The pinned container uses:

- ROCm 7.14 base image
- PyTorch 2.11.0 ROCm wheel for `gfx1151`
- Ultralytics 8.4.117 and YOLOv8s
- FFmpeg/VAAPI for clip encoding
- Tesseract for burned-in clock OCR

Other AMD GPUs may work after changing the ROCm wheel index and validating
the Docker image. NVIDIA and CPU backends are not implemented in this branch.

The Ryzen AI NPU scripts are retained as an experimental benchmark path. The
production detector uses the GPU because the complete detection/tracking stack
and hardware video path were faster and more mature on this machine.

## Quick start

Clone to the recommended location:

```bash
git clone https://github.com/soothill/garmin-overtake-detector.git \
  "$HOME/garmin-overtake-detector"
cd "$HOME/garmin-overtake-detector"
```

Install Ubuntu host packages, then log out and back in after the group change:

```bash
./scripts/install-host-dependencies-ubuntu.sh
```

Install the user services and build the tested container:

```bash
./install.sh
```

Mount the camera archive read-only and arrange it as:

```text
/mnt/garmin/
├── varia-vue/              # front camera
│   └── YYYY-MM-DD/*.mp4
└── rct715/                 # rear camera
    └── YYYY-MM-DD/*.mp4
```

Edit `~/.config/garmin-overtake-detector/environment`, then run:

```bash
./preflight-evox3.sh
systemctl --user start garmin-overtakes-gpu-all.service
journalctl --user -fu garmin-overtakes-gpu-all.service
```

Results appear beneath:

```text
~/garmin-overtake-detector/output/paired-parallel-v1/
```

See [Installation](docs/INSTALLATION.md) for the full host, GPU and service
setup, and [NFS setup](docs/NFS.md) for the read-only source mount.

## Output safety model

The source archive is mounted into every container as `/videos:ro`. Reports,
temporary files, models and generated clips are written to a separate local
output directory. The preflight refuses to proceed unless the source is an
active read-only NFS4 mount and the output remains inside its configured safety
root.

A camera result is complete only when its source duration, detector settings,
reports, benchmark, ownership and final heartbeat validate. A combined date is
publishable only when it has:

- `validation.json` with `valid=true`
- `alignment_method=vehicle_handoff_clock_v2`
- `layout=front-left_rear-right`
- a front track ID for every accepted handoff
- a clock residual no larger than 1.5 seconds

Ambiguous events are skipped instead of being joined to an unrelated front
vehicle. See [Architecture](docs/ARCHITECTURE.md) and
[Operations](docs/OPERATIONS.md).

## Measured reference run

On the tested EVO-X3, 66 files containing 135.84 source hours completed in
16.493 aggregate detector worker-hours—8.236x real time, or 7.285 worker-minutes
per source hour. Front and rear lanes achieved 8.158 and 6.162 minutes per
source hour respectively. A gigabit NFS path delivered about 117 MB/s buffered
sequential read throughput and was not the single-lane bottleneck.

The reference archive yielded 633 defensible synchronized clips. A separate
review retained 651 rear-only vehicle clips rather than manufacturing unsafe
front matches. These numbers describe one archive and are not accuracy claims
for other roads, camera positions or lighting.

See [Performance notes](docs/PERFORMANCE.md) for interpretation and tuning.

## GPU, NPU and Hailo efficiency harness

`platform_video_benchmark.py` runs one front/rear pair through a shared
decoder, deterministic tracker, trajectory evaluator and report writer.  It
supports the ROCm GPU, Ryzen AI NPU and Hailo-8L backends.  The accompanying
host runner records wall-clock time, AMD SMI package/accelerator power, or
Raspberry Pi PMIC output-rail power with an idle baseline.

The harness supports both a deployable-workflow comparison and a controlled
GPU/NPU comparison.  The controlled run exports the exact GPU YOLOv8s weights,
quantizes them to XINT8 with AMD Quark and executes that graph through Ryzen
AI.  Detection and event totals are included so power cannot be interpreted
without output quality.  See
[Platform efficiency benchmark](docs/PLATFORM-BENCHMARK.md) for setup,
measurement boundaries and reproducible commands. See the
[Hailo-8L model build](docs/HAILO.md) for compiling the exact GPU weights into
an HEF and preserving calibration and model hashes.

The hardened parity rerun on one complete 3.168-source-hour paired recording
uses RGB24, common 0.10 continuation/0.20 track-start confidence, 0.50 NMS,
class-agnostic vehicle association and robust trajectory geometry. Radeon GPU
finished in 18.43 minutes and used 20.89 Wh of APU package energy. Same-source
YOLOv8s XINT8 on the NPU took 27.52 minutes and 40.39 Wh on the identical
boundary. The GPU was 1.493 times faster and used 48.28% less gross package
energy. The NPU accelerator itself averaged only 1.95 W; CPU-assigned graph
work and data movement still dominate the complete NPU workflow.

Hailo-8L took 63.41 minutes and measured 4.83 Wh across Raspberry Pi PMIC
output rails. That smaller internal boundary is directional and is not ranked
against APU package power. Candidate totals were 158 GPU, 180 NPU and 176
Hailo; across 158 two-of-three consensus events their coverage was 91.14%,
92.41% and 94.30%. A blind, disagreement-heavy review set found that Hailo's
extra candidates more often included parked vehicles, cross traffic and
duplicate tracks, so higher candidate count was not higher pass accuracy.
See the benchmark guide for the review protocol, held-out threshold
calibration and the exact-weight Hailo build path.

## Useful commands

```bash
# Inspect current batch state
cat ~/garmin-overtake-detector/output/paired-parallel-v1/status.tsv
cat ~/garmin-overtake-detector/output/paired-parallel-v1/summary.json

# Run tests on the host
python3 -m unittest discover -s tests -v

# Run a short parallel smoke test using container paths from your archive
./parallel-smoke-evox3.sh \
  /videos/varia-vue/YYYY-MM-DD/front.mp4 \
  /videos/rct715/YYYY-MM-DD/rear.mp4

# Enable the optional validated-output mirror after configuring it
systemctl --user enable --now garmin-output-video-mirror.timer
```

## Privacy

Vehicle footage may contain faces, number plates, locations and timestamps.
The repository deliberately contains no ride footage, frames, GPS coordinates,
private hostnames or production manifests. Review local law and your publishing
policy before retaining or sharing generated clips. See
[Privacy and responsible use](docs/PRIVACY.md).

## Licensing

This project is licensed under the [GNU AGPL-3.0](LICENSE). It installs
Ultralytics, which offers AGPL-3.0 and commercial licensing options; users are
responsible for selecting and complying with the licensing terms appropriate
to their deployment. No model weights are stored in this repository—the pinned
YOLOv8s weights are downloaded when the container is built.

## Project status

This is a working reference implementation extracted from a production batch,
not a turnkey safety system. Contributions that improve camera portability,
evaluation datasets, redaction and additional hardware backends are welcome.
