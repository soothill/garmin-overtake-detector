# Troubleshooting

## Preflight sees `autofs`, not NFS4

Touch the automount before inspecting it:

```bash
find /mnt/garmin -maxdepth 1 -mindepth 1 -print -quit
findmnt -t nfs4 /mnt/garmin
```

## Docker cannot access the GPU

Confirm the current login includes the new groups:

```bash
id
ls -l /dev/kfd /dev/dri/renderD128
docker run --rm --device=/dev/kfd --device=/dev/dri \
  rocm/dev-ubuntu-24.04:7.14.0-full rocminfo
```

Log out and back in after changing group membership. Do not solve this by
running the whole batch as root; output ownership and root-squashed NFS behavior
are designed around an ordinary user.

## A source retries after five minutes

Inspect its `progress.json`, the lane attempt log and the named container:

```bash
docker ps -a --filter name=garmin-overtakes
journalctl --user -u garmin-overtakes-gpu-all.service -n 200
```

The watchdog is intentional. Preserve the archived attempt before changing a
timeout or retry limit.

## Displayed clocks differ in a good combined clip

This is expected. The compositor estimates clock bias and aligns the physical
rear-to-front handoff. Two cameras can show different timestamps at the same
moment. Validate the vehicle sequence, not equal clock text.

## A real vehicle has no combined clip

The front interval may be missing, obscured or ambiguous. Use the reviewed
rear-only workflow rather than lowering handoff safety until unrelated cars
match. For algorithm changes, add a regression test and keep the 1.5-second
residual guard.

## Mirroring fails after an interrupted transfer

An upload-only receiver may still hold its single-writer lock briefly. Confirm
no mirror process remains, wait for the receiver session to exit, then start the
same service again. Partial files are resumable and complete destination files
remain visible until delayed rename.
