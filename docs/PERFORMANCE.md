# Performance notes

## Reference result

The production reference run used 36 front files and 30 authoritative rear
files, totalling 135.84 hours of source video:

| Lane | Source hours | Worker hours | Minutes/source hour | Real-time factor |
|---|---:|---:|---:|---:|
| Front | 76.383 | 10.386 | 8.158 | 7.354x |
| Rear | 59.457 | 6.107 | 6.162 | 9.736x |
| Aggregate | 135.840 | 16.493 | 7.285 | 8.236x |

Front and rear ran concurrently, so aggregate worker time is not the same as
elapsed batch time. Composition also overlapped detection whenever a paired
date became ready.

## NFS

The tested gigabit network sustained about 117 MB/s buffered sequential reads
with one-megabyte NFS reads and no retransmissions. A representative front file
needed about 37.5 MB/s while processing at 9.37x real time, leaving room for a
rear lane. Three composition workers measured roughly 143 Mbit/s of additional
NAS traffic in the tested workload.

Use `hard`, TCP and large reads. Do not trade integrity for apparent speed with
`soft`. If the aggregate approaches 100 MB/s, upgrade the physical link.

## GPU versus NPU

The NPU path was evaluated but not selected for production. The GPU path could
run the complete Ultralytics detector/tracker, use the mature ROCm PyTorch
stack, and share the AMD media pipeline. NPU conversion/runtime limitations
made it less efficient for this end-to-end workload despite the NPU's appeal
for isolated low-power inference.

Re-evaluate when a supported NPU runtime can execute the complete detection
graph without costly format conversion or CPU fallbacks. The included NPU
scripts are benchmarks, not part of the hardened batch.
