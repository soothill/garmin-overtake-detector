# Installation

## 1. Prerequisites

- Ubuntu 24.04
- AMD GPU exposed as `/dev/kfd` and `/dev/dri/renderD128`
- A compatible AMD kernel driver/ROCm host installation
- Docker
- At least 100 GiB free for normal batches; substantially more if retaining
  old layouts or large archives
- A read-only local or NFS camera source

Use [AMD's current installation documentation](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/)
for the host driver and ROCm components. The container pins its own user-space libraries, but it still
depends on the host kernel driver, KFD device and render node.

Install ordinary host packages:

```bash
./scripts/install-host-dependencies-ubuntu.sh
```

Log out and back in after the script adds the current user to the `docker`,
`render` and `video` groups. Confirm:

```bash
docker info >/dev/null
test -r /dev/kfd && test -w /dev/kfd
test -r /dev/dri/renderD128 && test -w /dev/dri/renderD128
amd-smi metric -p -u --csv | head
```

## 2. Install the project

The systemd units use `~/garmin-overtake-detector`. Clone there, or run the
installer from another checkout and it will create that path as a symlink.

```bash
./install.sh
```

Use `./install.sh --skip-build` if you only want to install the configuration
and units. The normal install builds `garmin-overtakes:rocm7.14-yolov8s` and
runs the complete test suite inside the image.

## 3. Configure

Edit:

```text
~/.config/garmin-overtake-detector/environment
```

The important settings are:

| Setting | Default | Purpose |
|---|---:|---|
| `GARMIN_SOURCE_MOUNT` | `/mnt/garmin` | read-only host source mount |
| `GARMIN_FRONT_DIR` | `varia-vue` | front-camera directory |
| `GARMIN_REAR_DIR` | `rct715` | rear-camera directory |
| `GARMIN_BATCH_NAME` | `paired-parallel-v1` | output batch name |
| `GARMIN_COMPOSITION_WORKERS` | `3` | concurrent date composers |
| `GARMIN_MINIMUM_FREE_GIB` | `100` | preflight free-space floor |
| `GARMIN_EXPECTED_*_FILES` | `0` | optional exact inventory guards |

Keep the expected counts at zero for the first run. Once the archive is stable,
set them to exact values so preflight can detect a missing mount or source.

## 4. Mount the source

Follow [NFS.md](NFS.md), or provide another read-only NFS4 mount with the same
directory shape. Local bind-mounted storage requires adapting the NFS-specific
preflight check.

## 5. Validate

```bash
./preflight-evox3.sh
```

Preflight checks the inventory, mount safety, free space, Docker image, GPU
access, PyTorch ROCm path, source readability and AMD SMI telemetry.

## 6. Start and monitor

```bash
systemctl --user start garmin-overtakes-gpu-all.service
journalctl --user -fu garmin-overtakes-gpu-all.service
```

The unit is intentionally not enabled at boot. A batch can consume many hours
of GPU, CPU, disk and network capacity, so start it explicitly.

## 7. Optional services

After a batch completes:

```bash
# Review skipped rear events and produce clearly labelled rear-only clips
systemctl --user start garmin-overtakes-review-skipped.service

# Recreate a validated existing set with the current front-left/rear-right layout
systemctl --user start garmin-overtakes-layout-recompose.service
```

Configure the upload-only mirror before enabling its timer. See
[MIRRORING.md](MIRRORING.md).

For common failure modes, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
