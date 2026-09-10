# Capacity benchmarks (Phase 16)

The Phase-16 harness is measurement infrastructure: its own implementation did not alter production models, thresholds, queue semantics, recovery, incident routing, or source ownership. Later production phases may harden runtime correctness independently; the harness should always exercise the current production runtime unchanged. Synthetic camera-equivalents are **not** inference capacity. RTX 3050 measurements are **not** an RTX 5090 production camera-count estimate.

## Commands

Run from the repository root in the environment used for production inference. Real mode requires the existing runtime dependencies and lightweight `psutil` (normally installed with Ultralytics). NVIDIA telemetry is optional. No UI, dispatcher, database changes, or new service packaging is needed.

```powershell
python -m benchmarks --help
python -m benchmarks control_plane --counts 50 100 200 300 --workload mixed --warmup-sec 1 --duration-sec 5 --seed 17
python -m benchmarks real_inference --counts 1 2 --workload fight --config <existing-effective-config.json> --source fight/sample_2.mp4 --warmup-sec 5 --duration-sec 180
python -m benchmarks real_inference --counts 1 --workload speed --config <existing-speed-enabled-config.json> --warmup-sec 5 --duration-sec 180
python -m benchmarks real_inference --counts 1 --workload mixed --config <existing-speed-enabled-config.json> --warmup-sec 5 --duration-sec 180
```

`mixed` enables both Fight and Speed on every logical camera, sharing that camera's single CameraIngest decode. It uses the first Speed-enabled camera template and its existing media/calibration. Speed-only uses the same selection; Fight-only uses the first Fight-enabled template. An optional local `--source` override must remain compatible with Speed calibration. Remote sources/endpoints and credential fields are rejected. No automatic remote model acquisition is provided by the harness; provision the configured weights before running.

Each logical camera gets a distinct ID and a distinct hardlink (copy fallback) to the same input file. This intentionally measures shared-content, ordered-file workloads, not the diversity, pacing or frame loss of many live RTSP streams. The original media is never modified. Do not run alongside a production runtime: the harness refuses an already-running runtime and locks out duplicate harnesses; operators must also avoid starting production during a measurement.

`--counts` accepts any distinct positive counts up to the existing registry limit; there is no benchmark-specific camera ceiling. Grow real counts manually while the machine remains usable. Existing slot reservations are retained, or expanded to the requested count when necessary, and reported. No automatic stress sweep.

`--gpu-device <NVIDIA-index-or-UUID>` explicitly selects NVIDIA telemetry and sets CUDA_VISIBLE_DEVICES for the child runtime. The existing model configuration must use a compatible logical CUDA device (normally 0 after visibility remapping). Without this option the environment/model device choices remain unchanged and telemetry reports all visible NVIDIA devices; the harness never infers which GPU ran inference from utilization alone. `nvidia-smi` calls have a two-second timeout; missing tools, unsupported fields and failures become unavailable data.

## Scope and outputs

Use `--output benchmarks/results/<new-name>` to choose a new, git-ignored directory; existing directories are never overwritten. Defaults use a UTC timestamp.

- `benchmark_summary.json`: atomic summary with separate `real_inference` and `control_plane` arrays plus OS/Python/CPU/RAM/GPU/repository metadata.
- `runs.jsonl`: fsynced completed run records, independent of summary replacement.
- `system_samples.csv`: streamed, flushed system samples with JSON GPU columns.
- Real-run subdirectories: isolated Supervisor state/logs, effective configs, local source links, runtime reports and durable incident outbox. No dispatcher consumes benchmark outbox events and no benchmark Incident is inserted in Django.

Runtime outputs/outbox are isolated. Model-specific debug destinations in supplied model YAML files remain inherited, just like detection settings; inspect those settings before benchmarking. Generated results can be large. There is no automatic evidence deletion or cleanup. Remove a finished benchmark directory only after its Supervisor/runtime has stopped and its evidence is no longer needed.

Summary/export fields redact absolute locations, private URLs and credential keys. Media is identified by SHA-256, size and extension; base config and model files have hashes for comparison. Local effective runtime configs are diagnostic artifacts and contain local paths; review before sharing the entire result directory.

## Measurement definitions

Real mode runs the common Supervisor/runtime, unchanged current Fight/Speed workers and CameraIngest. It preserves detection/calibration settings and ordered-file policy, enables existing bounded performance/health telemetry and directs incident output to the benchmark outbox. It uses clean EOF/drain or a finite deadline, followed by Supervisor-owned shutdown in `finally`. Deadline truncation is INCOMPLETE, never claimed as a successful full-file run. Exit code 1 indicates an incomplete run or failed synthetic correctness checks; code 0 means benchmark validation completed.

`--warmup-sec` excludes initial wall time from host/GPU sampling; `--duration-sec` is the maximum subsequent measurement time. Clean EOF can shorten that interval. Runtime frame totals and effective rates are full-run values (including startup and drain), **not steady-state FPS**. Existing Person/Pose latency `steady_state` excludes the configured first warm-up requests independently of the wall-time sampler. Stage3 distributions follow existing worker telemetry semantics.

Host CPU utilization, runtime process-tree RSS (a sum, not unique physical RAM), harness RSS, host RAM and per-GPU utilization/VRAM have min/mean/p50/p95/max. Percentiles use linear interpolation. Every sampler has a bounded tail (`--max-samples`, default 256); summaries identify observations versus retained samples. CSV streams all system observations without keeping them in memory. Polling/scheduling and bounded GPU queries can overshoot the requested deadline; Supervisor graceful-stop/drain time is additional and reported in wall duration.

Existing reports supply per-camera decoded/Fight-consumed frames, effective FPS, Fight/Speed drops, reconnects, final lifecycle/health and Speed sequence progress. Stage reports supply accepted/dispatches, client results or job completion, health progress and bounded existing latency summaries. Health progress can include a started request; it is not relabeled as completed inference. Capacity reports include accepted, rejected_capacity, deferred_file, dropped_live, stale_generation, high_water and outstanding. Sampled outstanding peaks can miss short bursts; existing high_water counters retain production definitions.

Unavailable metrics remain null or explicitly unavailable, not fabricated: Vehicle latency, reliable end-to-end incident latency, steady-window camera FPS and Speed completed-frame counts are not instrumented by this phase. Missing or zero-sample latency distributions do not establish zero latency. Failed/early runs may lack camera or worker summaries; zero decoded totals in those runs are only the sum of available reports, not proof that decoding never began.

## Classification and interpretation

Classification thresholds are CLI options, written into each real result:

- INCOMPLETE: deadline, nonzero exit, missing required camera reports/samples, failed required consumer, observed recovery, failed runtime health or no usable frames.
- SATURATED: live admission drop ratio >= 10%, or rejection-attempt ratio >= 50% with actual live shedding.
- PRESSURED: live admission drops >= 1%, rejection attempts >= 10%, or sampled outstanding/capacity >= 90%.
- HEALTHY: complete with none of those observed criteria.

Ratios aggregate stage admission attempts, **not unique camera frames**. Ordered file retries can produce many rejection/deferred counters without losing frames; those alone never imply SATURATED. High GPU utilization alone never changes classification. Queue pressure with mean GPU/CPU utilization >= 90% may produce `gpu_bound_candidate`/`cpu_bound_candidate`; otherwise `queue_pressure` or `unknown`. These are diagnostic hints, not proven bottlenecks. A HEALTHY short file run does not establish sustained live capacity or successful activity at every model stage.

## Synthetic scope

Real desired-state validation, CameraRuntimeManager, HealthRegistry and all four FairRequestQueue instances run with in-memory bounded transport and inert process objects. The scenario measures initial validation/reconcile, seeded 25% remove/re-add, capability transition, steady idempotent reconcile, health evaluation and JSON snapshot serialization. It checks unchanged camera identity, stable/unique slots, advanced generations, stale-result fencing and capability transition scope. Each scheduler performs two full round-robin rounds plus deliberate full-slot rejections. No request reaches a model and no fake process target executes.

Synthetic timings exclude model/process-spawn/IPC/decode costs. A 10ms pause between cycles prevents busy loops and is excluded from individual operation timings. RSS delta includes allocator effects and prior runs in the same process: it is approximate, not an isolated per-camera production memory estimate. Repeated telemetry and transitions retain at most `--max-samples`; per-camera state and scheduler storage scale with the explicitly requested count.

## Local validation notes (RTX 3050 laptop, 2026-09-10)

The 50/100/200/300 synthetic mixed scenarios completed with all correctness checks.

An initial one-camera real smoke using the current effective Fight configuration and `sample_2.mp4` exited 10 after native workers reported duplicate `libiomp5md.dll` initialization (OpenMP Error #15). The harness correctly reported that attempt as INCOMPLETE and did not set `KMP_DUPLICATE_LIB_OK=TRUE` or otherwise conceal the native error.

Follow-up diagnosis found both active-environment and base-Anaconda `libiomp5md.dll` locations with identical observed SHA-256 content. Ordinary Torch/OpenCV/NumPy/Ultralytics/sklearn imports and CUDA tensor allocation worked. After clean conda activation (`CONDA_SHLVL=1`) and invocation through `conda run -n torch_gpu --no-capture-output`, real Fight runs completed successfully. The exact OpenMP root cause is **not proven**; do not encode a speculative cause or use the unsafe duplicate-runtime override.

The initial accepted one-camera harness smoke decoded 903 frames at about 14.76 aggregate full-run effective FPS with zero live admission drops/rejection attempts and queue-ratio peak 0.03125.

A later charged-system continuation using the same selected Fight effective config and `fight/sample_2.mp4` recorded:

```text
2 cameras: 28.67 aggregate FPS, HEALTHY
4 cameras: 49.89 aggregate FPS, HEALTHY
8 cameras: 65.55 aggregate FPS, HEALTHY
```

All three had zero admission-drop ratio, zero rejection-attempt ratio and no observed recovery. Person/Pose queue p95 increased substantially by 8 cameras while VRAM remained about 640 MiB and CPU mean remained well below full saturation. This is useful pressure characterization but does not prove a single root cause such as GPU compute or IPC.

A pre-Phase-17 12-camera / 180-second run was INCOMPLETE and is **not a capacity point**. It accepted the expected aggregate Person/Pose/Stage3 work counts but observed two camera-local restarts (`bench-0000` and `bench-0001`) after `ONLINE/file_draining -> FAILED/process_dead`; shared Fight workers did not restart. The watchdog restarted those cameras, advanced generation and replayed their local files. Diagnosis showed an ordered-file EOF/consumer-exit observation race between lifecycle manager and watchdog.

Phase 17 (`3b9733f20a3ca4d3773c63fed0caba7939f2f27c`) hardens this production lifecycle: CameraIngest publishes a generation-local authoritative EOF Event before consumer EOF delivery; clean Fight/Speed file drain requires authoritative EOF plus exit code 0; pre-EOF/non-zero failures remain fail-closed; mixed Fight+Speed waits for all required consumers; clean EOF does not restart, advance generation or replay the source.

Therefore the next manual acceptance is to rerun the **same 12-camera Fight workload with the same 180-second deadline** before interpreting 12-camera throughput. Do not extend the deadline first: if it remains INCOMPLETE, diagnose the new reason so the before/after comparison remains meaningful.

Use a clean inference environment for future measurements, preserve result metadata, and run the same harness with comparable workloads on the production-target GPU. No RTX 5090 camera-count extrapolation from RTX 3050 measurements is justified.
