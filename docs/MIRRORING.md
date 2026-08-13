# Validated-output mirroring

The optional mirror sends only validated combined clips and validated reviewed
rear-only clips. It does not mirror reports, failed attempts, layout history or
source media.

The receiver should expose a dedicated directory through an SSH key restricted
to `rrsync`. On the receiver, create a media user and add a forced command like
this to its `authorized_keys` entry:

```text
restrict,command="/usr/bin/rrsync /srv/garmin-output" ssh-ed25519 AAAA... processor
```

Confirm the `rrsync` location on your distribution. The destination directory
must be writable by that user but should not contain unrelated media.

On the processing host, set absolute values in the environment file:

```text
GARMIN_OUTPUT_DESTINATION=media@media-server.example:/
GARMIN_OUTPUT_SSH_KEY=/home/processor/.ssh/garminoutput_mirror_ed25519
GARMIN_OUTPUT_BWLIMIT_KIB=50000
```

Then enable the timer:

```bash
systemctl --user enable --now garmin-output-video-mirror.timer
```

The mirror uses partial files and delayed updates, so the receiver continues to
serve the previous complete file while a replacement is transferred. It never
uses `--delete`. If strict destination cleanup is required, audit and remove
stale paths separately rather than granting the processing job deletion power.
