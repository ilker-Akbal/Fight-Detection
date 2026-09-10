# Capacity characterization and bottleneck benchmarks

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

Treat completed run outputs as immutable evidence: use a new output directory
for every experiment, including retries. Active runs still append their streams
and atomically update summaries; this is not filesystem-enforced immutability.
Do not stage generated results, source links, runtime state or the harness lock.

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

Unavailable metrics remain null or explicitly unavailable, not fabricated. Phase 18 adds Vehicle/local timings and Speed completed-frame counters (see below), but pure IPC copy cost, GPU kernel duration, reliable end-to-end incident latency and steady-window camera FPS remain unavailable. Missing or zero-sample latency distributions do not establish zero latency. Failed/early runs may lack camera or worker summaries; zero decoded totals in those runs are only the sum of available reports, not proof that decoding never began.

## Phase 19: shared-worker finalization and batching readiness

Normal dynamic-runtime close (including clean file EOF), and capability removal
after required Fight drain, now send inference sentinels before setting the bundle
stop event. Person/Pose require those sentinels to leave their loops and publish
worker/batch summaries. Person, Pose, Stage3 and Vehicle get one shared **8-second
grace budget per bundle**; result routers and Reporter stay available during it.
Normal process joins allow their Reporter feeder buffers to flush. The existing
bounded terminate/kill fallback handles unhealthy workers; failure/recovery
withdrawal skips this grace and retains its existing fencing and forced teardown.

The dynamic parent finishes producers before sending Reporter's sentinel (bounded
retry), then joins Reporter so its final file flush precedes performance summary
construction. This does not make telemetry an incident durability or EOF signal.
Full/unusable report transport, forced termination, Reporter/disk failure or grace
expiry can still leave telemetry unavailable; no client timings are substituted
for missing worker inference distributions.

Person/Pose summaries and real benchmark `stages.<stage>.latency` retain worker
`result_enqueue_ms`, `worker_timings.all_requests`, `worker_timings.steady_state`
and `batch` (configuration, counts, size, collection wait, inference and existing
steady-state distributions). Worker views remain distinct from pooled raw client
samples; no percentile averaging is performed. **Person batching remains disabled
by default** (size 1, max wait 0); only an explicit configuration enables it.
No GPU capacity claim or batching speedup is implied by these regression tests.

## Phase 19.1: Windows snapshots and failure identity

HealthSnapshotStore writes complete temporary JSON before atomic replacement.
Windows readers/scanners can temporarily deny replacement of an open destination.
Only replacement errors with `winerror` 5/32/33 receive bounded retries: four
attempts, with 20/40/80 ms delays, at most 140 ms added backoff. Persistent
permission errors and other disk failures still surface; the old snapshot is
not unlinked or replaced with partial JSON. Snapshot publication remains
best-effort/non-fatal; exhausted failures now include `errno` and `winerror`.

Vehicle recovery status preserves `component`, `reason`, `component_failure`,
`service_epoch`, retry count and the pre-teardown process exit code. An alive
inference-stalled process has no original exit code; its later forced-kill code
must not be reported as the cause. Fight health failures retain equivalent
component/exit identity. Recovery budgets, fail-closed file behavior and Fight
isolation are unchanged.

## Phase 18: bottleneck attribution

The real runtime `performance_summary.json` and real benchmark result now contain
`attribution`. It is separate from synthetic results and does not participate in
HEALTHY/PRESSURED/SATURATED/INCOMPLETE classification.

Enable with the existing `performance_metrics_enabled` runtime flag (real benchmarks
already enable it). Existing `performance_metrics_sample_every` and
`performance_metrics_max_samples` bound each timing collector. Reports use the
existing bounded Reporter queue and `put_nowait`. The runtime setting
`performance_attribution_report_interval_sec` defaults to **30 seconds** and is
clamped to a **5-second minimum**; invalid/non-finite values use the default.
Each producer independently allows its first report immediately, then at most one
periodic attempt per interval (failed attempts are rate-limited too). The final
`force=True` attempt bypasses the interval. Rows include the normal status `ts`.
Failures are swallowed; a later successful report
includes `reports_dropped`. If the final report is lost or a process is killed, the
last observed report may be partial or null. No added telemetry drives health,
admission, generation, service recovery or authoritative file EOF.

Metrics (milliseconds unless a counter):

| Component | New attribution |
|---|---|
| Vehicle service | `requests_accepted`, `requests_completed`, `inferences_completed`, `stale_generation`, `stale_live`; `queue_wait_inclusive_ms`, `model_initialize_ms`, `inference_ms`, `result_enqueue_ms` |
| Speed client | `requests_accepted`, `results_received`; `vehicle_enqueue_ms`, `vehicle_round_trip_ms`, `vehicle_call_ms` |
| CameraIngest | `read_ms`, `fight_enqueue_ms`, `speed_enqueue_ms`, `preview_enqueue_ms`, `fanout_ms`; per-branch `offered`, `enqueued`, `dropped` frame counts |
| Fight consumer | `frames_completed`, `frame_delivery_age_ms`, `local_processing_ms`, `person_call_ms`, `pose_call_ms`, `stage3_enqueue_ms` |
| Speed consumer | `frames_received`, `frames_completed`, `frame_delivery_age_ms`, `processor_initialize_ms`, `local_processing_ms`, `preprocess_ms`, `tracking_ms`, `speed_decision_ms`, `visualization_evidence_ms` |
| Preview consumer | `frames_received`, `frame_delivery_age_ms` |

Interpretation limits:

- Vehicle queue wait starts **before admission**, so it includes file defer,
  enqueue, multiprocessing transport and service queueing. It is not a pure
  post-enqueue queue wait. The existing capture/staleness clock is unchanged.
- Vehicle `requests_accepted` counts generation-valid work handled by the service.
  `requests_completed` counts only results successfully published to the per-slot
  queue, including explicit stale-live outcomes; abandoned delivery is not completion.
  `inferences_completed` counts only successful detector calls, even if delivery
  is later abandoned. Stale-generation work is not completed.
  `result_enqueue_ms` records one total interval per successfully published result,
  including all `queue.Full` retry waits; abandoned delivery has no observation.
  Scheduler admission counts remain in the
  unchanged common `capacity` report for Person/Pose/Stage3/Vehicle.
- Vehicle `inference_ms` is the detector-call wall time, including its internal
  preprocessing/postprocessing and excluding separately measured model setup.
  There is no CUDA synchronization added and no GPU-kernel-time claim.
- Client RTT covers admission through the matching result (including stale
  outcomes). Client-call timing additionally covers failed/stopped calls; local
  timings subtract the actual nested call elapsed time, not sampled percentiles.
- Fight local timing excludes Person/Pose calls and Stage3 admission wait. Other
  camera-local work, evidence and status I/O remain included. Speed local timing
  excludes Vehicle calls and external FPS pacing, includes preprocessing,
  tracking/decision, visualization and evidence/persistence. Local durations can
  include interrupted frame attempts; completed counters count only returns.
- Ingest read timing includes unsuccessful/EOF reads. Enqueued means a successful
  publication, **not a consumed frame**; latest queues may later evict it. Offered
  counts exclude absent/disabled branches and exclude EOF signals. Fan-out timing
  covers the existing sequential branch publication calls, including blocking.
- Frame delivery age uses the existing capture-complete monotonic timestamp. It
  combines earlier fan-out waits, queue backlog, IPC/deserialization and receive
  bookkeeping. Pure IPC cost cannot be isolated without additional boundaries;
  no frame copy or shared-memory transport was introduced.
- Reports retain bounded tails and are per process incarnation (generation /
  Speed epoch / Vehicle service epoch). Status scans retain only the latest new
  attribution summaries with a registry-sized cap, not the periodic history.
  Camera summaries are not pooled across cameras and percentiles are never
  merged by averaging. A separately labeled mean of run-level statistics is not
  a pooled percentile. Disabled/missing/no-sample metrics remain null. Existing status and
  incident history/cursor behavior is not compacted or rewritten.

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

That acceptance is now complete: `rtx3050-fight-12-phase17` finished HEALTHY at
76.80 aggregate FPS with `10836 = 903 * 12` frames, no recovery/replay/restart and
Person/Pose/Stage3 counts 5004/3732/72. Person/Pose queue p95 was approximately
206.9/132.1 ms. These are healthy historical characterization results, not a
supported production camera count; the failed pre-fix run remains diagnostic only.

Use a clean inference environment for future measurements, preserve result metadata, and run the same harness with comparable workloads on the production-target GPU. No RTX 5090 camera-count extrapolation from RTX 3050 measurements is justified.

## Characterization evidence and calculations

The [project overview](../README.md#real-inference-characterization) presents the
selected healthy results on Windows, Intel Core i7-13700H, 64 GB RAM and NVIDIA
GeForce RTX 3050 Laptop GPU (6 GB). These short ordered-file runs characterize
that hardware/workload; they do not guarantee sustained live capacity, detection
accuracy or portability to another GPU. Synthetic 200/300 camera-equivalent tests
are not real inference capacity evidence.

Local evidence directories below are relative to ignored `benchmarks/results/`.
Their `benchmark_summary.json` records were checked against the documented
figures. They are deliberately not committed or linked as distributable assets.

| Evidence directory | Workload / role | Classification |
|---|---|---|
| `phase18-attribution-speed8` | Speed 8, 82.70 FPS | HEALTHY, no recovery |
| `phase18-attribution-speed12` | Speed 12, 89.25 FPS | HEALTHY, no recovery |
| `phase19-mixed8-baseline-r2` | Pair A, OFF | HEALTHY, no recovery |
| `phase19-mixed8-batch2` | Pair A, Batch-2 / 5 ms | HEALTHY, no recovery |
| `phase19-mixed8-batch4` | Batch-4 / 5 ms comparison | HEALTHY, no recovery |
| `phase19-final-mixed8-off` | Pair B, OFF after 19.1 | HEALTHY, no recovery |
| `phase19-final-mixed8-batch2` | Pair B, Batch-2 / 5 ms after 19.1 | HEALTHY, no recovery |
| `rtx3050-fight-12-phase17` | Historical Fight-12 EOF acceptance | HEALTHY, no recovery |

Mixed traffic files contain 678 frames per logical camera: 5424 decoded frames,
2600 Person admissions and 1344 Vehicle admissions per eight-camera run. Pose and
Stage3 did not receive work on this content. Do not compare those runs as though
they exercise full Fight-event inference or use the Fight-only 903-frame source.

Person batching remains configurable and **OFF by default**:

```json
{"person_batch_enabled": false, "person_batch_size": 1, "person_batch_max_wait_ms": 0}
```

The optional characterized experiment changes only those runtime keys in a
separate effective config to `true`, `2`, `5`. It is not a production-wide
recommendation. Batch-4 lowered Person queue mean to 37.51 ms but increased
Vehicle queue mean/p95 to 76.96/168.36 ms versus Batch-2's 39.19/103.19 ms in pair A;
44.67 FPS was slightly below Batch-2's 45.15 FPS. Batch-2 was the better tested
mixed-system tradeoff, not a universal optimum. Pair B also shows increased
Vehicle queue mean (14.04 to 54.75 ms): the smaller overall throughput gain must
be considered alongside the reduced Person queue pressure.

### Calculation convention

Use the supplied comparison inputs exactly as displayed in the root README:
pair A is rounded to two decimals; pair B preserves its supplied precision.
Raw local JSON retains additional precision for pair A (for example, OFF wall
132.324758 s and B2 wall 120.123545 s). Thus these displayed-input calculations
are descriptive summaries, not claims of extra measurement precision.

For each metric, take the arithmetic mean of its two runs per profile, then
compute `100 * (B2_mean / OFF_mean - 1)`. This is a ratio of profile means, not
the average of two percentage changes, and not frames pooled over total time.

| Metric | OFF calculation | B2 calculation | Relative change |
|---|---|---|---:|
| FPS | `(40.99 + 42.370409) / 2 = 41.6802045` | `(45.15 + 43.578609) / 2 = 44.3643045` | +6.4397477% |
| Wall seconds | `(132.32 + 128.01387) / 2 = 130.166935` | `(120.12 + 124.464735) / 2 = 122.2923675` | -6.0495912% |
| Person queue mean ms | `(171.57 + 166.882547) / 2 = 169.2262735` | `(105.22 + 102.568315) / 2 = 103.8941575` | -38.6063669% |
| Mean of run queue p95 values, ms | `(197.40 + 206.51937) / 2 = 201.959685` | `(132.13 + 132.865525) / 2 = 132.4977625` | -34.3939547% |

The last row is explicitly a mean of run-level statistics, **not a combined p95**.
No runtime collector merges percentiles this way. Two pairs do not establish
statistical significance. Per-request and per-batch inference distributions
also have different observation populations; a batch processes approximately
two requests, so a longer batch call alone does not establish a regression.

For Speed 8 to 12, camera growth is `(12 / 8 - 1) * 100 = 50%` and throughput
growth from the displayed values is `(89.25 / 82.70 - 1) * 100 = 7.9201935%`.
The queue/inference/utilization pattern supports shared-service scheduling and
arrival/backpressure explanations more than simple GPU saturation, but does not
isolate IPC-copy cost or prove one exclusive bottleneck.

### Post-19.1 validation evidence

Both final mixed runs had zero `health_snapshot_write_failed` occurrences in
`real-mixed-8/runtime/camera_status.jsonl`. Their final health snapshots reported
zero Person/Vehicle restarts; summaries recorded Person accepted/dispatched
2600/2600 and Vehicle 1344/1344, with zero `rejected_capacity`, `dropped_live` and
`stale_generation` for those stages. Both were HEALTHY without recovery.
This is successful real Windows runtime validation consistent with targeted
sharing-error regression tests, not a guarantee that permission/disk failures
cannot recur. The earlier incomplete `phase19-mixed8-baseline` is excluded from
the performance comparison.
