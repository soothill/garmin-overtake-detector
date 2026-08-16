# Operations

## Status

```bash
systemctl --user status garmin-overtakes-gpu-all.service
journalctl --user -u garmin-overtakes-gpu-all.service -n 100
cat ~/garmin-overtake-detector/output/paired-parallel-v1/status.tsv
cat ~/garmin-overtake-detector/output/paired-parallel-v1/summary.json
```

Per-lane files are `status-front.tsv`, `status-rear.tsv`,
`attempts-front.tsv` and `attempts-rear.tsv`. Composition adds
`status-combined.tsv`, one status file per worker and `attempts-combined.tsv`.

## Resume

The batch is resumable. A camera source is skipped only when `run.json`,
`benchmark.json`, complete `progress.json` and valid `validation.json` all
exist. A combined date is skipped only when its current validation passes.

Restart the same unit after fixing a transient problem:

```bash
systemctl --user restart garmin-overtakes-gpu-all.service
```

Do not delete good evidence. Failed attempts are retained beneath
`failed-attempts/` for diagnosis.

## New source files

An existing manifest is authoritative and protects against silently changing a
running batch. For a genuinely new batch, choose a new `GARMIN_BATCH_NAME` or
remove only the new batch's unstarted manifest before launch. Never alter the
source archive from the processing pipeline.

If the archive deliberately retains a recording that was superseded by a
longer copy, list the container path in a TSV and set
`GARMIN_SOURCE_EXCLUDE_FILE` in the environment file:

```text
source\treason
/videos/rct715/YYYY-MM-DD/old.mp4\treplaced by a longer recording of the same footage
```

Excluded files remain untouched in the read-only archive. A new batch copies
the exclusion list to `excluded-sources.tsv`, and expected source counts apply
to the authoritative inventory after exclusions.

After a successful main batch, systemd automatically starts the skipped-event
review. That service publishes validated rear-only fallback clips through the
same mirror used for paired clips.

## Worker tuning

- Front and rear detection use one GPU lane each.
- `GARMIN_COMPOSITION_WORKERS=3` was a good balance on the 16-core/32-thread
  Ryzen AI MAX+ 395 while detection was active.
- Eight workers gave the best tested throughput for a composition-only layout
  migration after detection had finished.

More workers can increase NFS and encode pressure without improving throughput.
Measure GPU activity, CPU load, free memory and network traffic before raising
the defaults.

## Disk planning

Combined 45-second 2560x720 clips can be hundreds of megabytes each. Retaining
rollback layouts temporarily doubles combined-media storage. The normal batch
floor is 100 GiB; layout migration defaults to 350 GiB.
