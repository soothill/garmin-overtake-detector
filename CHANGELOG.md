# Changelog

## Unreleased

- Add a reproducible paired-video GPU, Ryzen AI NPU and Hailo-8L efficiency
  harness with deterministic pair selection and a shared tracker/evaluator.
- Record cold-cache evidence, wall time, model hashes, accelerator telemetry,
  idle-adjusted energy and cross-platform event agreement.
- Report matched/unmatched events and a reference-independent two-of-three
  detection consensus for quality-parity review.
- Reject contaminated AMD idle baselines and Hailo runs with new undervoltage
  or throttling events.

## 1.0.0 - 2026-08-13

- Publish the hardened AMD GPU front/rear detection pipeline.
- Add concurrent camera lanes and three-worker streaming composition.
- Add physical vehicle-handoff alignment with camera clock-bias handling.
- Enforce front-left/rear-right 2560x720 combined output.
- Add resumable jobs, atomic evidence, watchdogs, bounded retries and validation.
- Add reviewed rear-only fallback clips without unsafe front-camera substitution.
- Add read-only NFS, systemd, validated-output mirror and installation tooling.
- Include experimental Ryzen AI NPU benchmarks and measured Strix Halo notes.
