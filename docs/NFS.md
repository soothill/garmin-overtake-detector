# Read-only NFS source

The strongest safety boundary in this project is a read-only NFS export mounted
read-only again on the processing host. Containers receive a third read-only
bind at `/videos:ro`.

## Server

On the NAS, export only the camera directory to the processing machine's fixed
address:

```bash
./scripts/configure-nfs-export.sh /srv/garmin 192.0.2.10 ro
```

The script backs up `/etc/exports` and creates an entry equivalent to:

```exports
/srv/garmin 192.0.2.10(ro,sync,no_subtree_check,root_squash)
```

Use the real private address of the processing machine; `192.0.2.10` is a
documentation-only example.

## Client

On the processing host:

```bash
./scripts/configure-nfs-client.sh nas.example:/srv/garmin /mnt/garmin
```

This installs `nfs-common`, backs up `/etc/fstab`, creates the mount point and
adds an on-demand systemd automount with:

```text
ro,hard,vers=4.2,proto=tcp,nosuid,nodev,noexec,_netdev,nofail,
x-systemd.automount,x-systemd.idle-timeout=10min,x-systemd.mount-timeout=30s
```

`hard` avoids silent short reads or corrupted processing when the server pauses.
The automount prevents an unavailable NAS delaying boot. Do not use `soft` for
source media.

Verify:

```bash
find /mnt/garmin -maxdepth 1 -mindepth 1 -print -quit
findmnt -t nfs4 /mnt/garmin
```

For a gigabit path, `rsize=1048576` and TCP normally provide near line-rate
sequential reads. If several workers exceed the link, a 2.5 GbE path is more
useful than speculative NFS option changes.
