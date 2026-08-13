# Ryzen AI NPU experiment

The repository includes `npu_benchmark.py`, `npu_detect_frames.py`,
`run-npu-benchmark.sh` and `benchmark-npu-evox3.sh` to preserve the evaluated
NPU path. They are not called by the production batch.

Set `RYZEN_AI_ROOT` to a working Ryzen AI/Lemonade runtime directory and run a
small representative benchmark before considering integration. Runtime setup,
model conversion and supported operators change more quickly than the GPU
container, so this project deliberately does not automate installation of the
vendor NPU stack.

The decision boundary is end-to-end efficiency, not the accelerator's quoted
TOPS. Include decode, resize, tensor conversion, unsupported-operation fallback,
tracking and clip encoding in any comparison. On the tested EVO-X3, ROCm GPU
inference plus VAAPI was the more complete and efficient production route.
