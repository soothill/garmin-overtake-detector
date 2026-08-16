FROM rocm/dev-ubuntu-24.04:7.14.0-full

LABEL org.opencontainers.image.source="https://github.com/soothill/garmin-overtake-detector" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.description="Hardened paired-camera overtake detection for AMD Strix Halo"

ARG ROCM_WHEEL_INDEX=https://repo.amd.com/rocm/whl/gfx1151/
ARG NUMPY_VERSION=2.5.2
ARG TORCH_VERSION=2.11.0+rocm7.13.0
ARG TORCHVISION_VERSION=0.26.0+rocm7.13.0
ARG ULTRALYTICS_VERSION=8.4.117
ARG OPENCV_VERSION=5.0.0.93
ARG LAP_VERSION=0.5.12

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    YOLO_CONFIG_DIR=/tmp/Ultralytics

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ffmpeg fonts-dejavu-core libglib2.0-0 libgl1 tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --break-system-packages --no-cache-dir \
      --index-url "$ROCM_WHEEL_INDEX" \
      --extra-index-url https://pypi.org/simple \
      "numpy==$NUMPY_VERSION" \
      "torch==$TORCH_VERSION" \
      "torchvision==$TORCHVISION_VERSION"

RUN python3 -m pip install --break-system-packages --no-cache-dir \
      "ultralytics==$ULTRALYTICS_VERSION" \
      "opencv-python==$OPENCV_VERSION" \
      "numpy==$NUMPY_VERSION"

RUN apt-get update \
    && apt-get install -y --no-install-recommends mesa-va-drivers vainfo \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --break-system-packages --no-cache-dir \
      "lap==$LAP_VERSION"

RUN mkdir -p /models /app \
    && cd /models \
    && python3 -c "from ultralytics import YOLO; YOLO('yolov8s.pt')"

COPY overtake_pipeline.py /app/overtake_pipeline.py
COPY summarize_batch.py validate_batch_result.py validate_combined_result.py \
  calibrate_pair_offset.py compose_paired_events.py review_skipped_events.py \
  recompose_combined_layout.py /app/
COPY check_pi_health.py npu_detect_frames.py platform_video_benchmark.py \
  prepare_platform_review.py sample_pi_pmic.py select_benchmark_pair.py \
  summarize_platform_power.py summarize_platform_results.py \
  wait_for_amd_idle.py /app/
COPY tests /app/tests

RUN cd /app && python3 -m unittest discover -s tests -v

WORKDIR /app
ENTRYPOINT ["python3", "/app/overtake_pipeline.py"]
