FROM python:3.12-bookworm

ARG QUARK_VERSION=0.12.post1
ARG TORCH_VERSION=2.9.1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
      "torch==${TORCH_VERSION}" \
      --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install --no-cache-dir \
      "amd-quark==${QUARK_VERSION}" \
      "onnxruntime==1.23.2" \
      "opencv-python-headless==4.13.0.92"

ENV TORCH_EXTENSIONS_DIR=/work/torch-extensions
WORKDIR /work
