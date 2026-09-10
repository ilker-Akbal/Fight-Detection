# Fight-Detection Architecture Contract

## Document ownership and maintenance discipline

This file is the architecture contract for the repository. It describes the **current Supervisor-managed production architecture**, the guarantees inherited from earlier phases, ownership and failure boundaries, durability rules, health/recovery semantics, benchmark interpretation, measured characterization evidence, and intentionally deferred work.

**Maintenance rule:** coding agents (including Codex) must read this file before architecture-affecting work and **must not modify it**. The project owner and ChatGPT maintain it from committed repository state.

Architecture refreshes are not a narrow “append the latest commit” exercise. Before changing this file, the maintainer must read the whole current contract, inspect the current `master` implementation of affected ownership paths, compare the new code with the prior architecture baseline, inspect focused tests as executable contracts, remove stale or contradictory claims, verify health/recovery/durability/task-router/deferred-work sections remain consistent, and keep measured facts separate from estimates or future production assumptions.

Current production-code reference commit:

```text
fefedcc81095a5b808f048d1bc7daa13e04c0b1f
Finalize Phase 19 shared worker telemetry and Windows health reliability
```

**Phase 19 + Phase 19.1 are committed on `master` and form the current production architecture baseline.** They preserve the shared Fight + Speed ownership model while hardening normal shared-worker finalization, final telemetry retention, Windows health-snapshot replacement behavior, and shared-service failure-cause reporting. They do not change detection/calibration thresholds, CameraIngest ownership, fair-admission correctness, authoritative ordered-file EOF, incident durability ownership, Django models, frontend behavior, or production batching defaults.

Validation reported for this baseline:

```text
full pytest: 214 passed, 1 skipped
compileall fight benchmarks tests: passed
git diff --check: passed
post-19.1 Mixed-8 OFF real inference: HEALTHY
post-19.1 Mixed-8 Batch-2 real inference: HEALTHY
health_snapshot_write_failed in those two final runs: 0 / 0
```

---

# 1. System purpose and direction

The repository is a centralized multi-camera security platform in which Fight Detection and Speed Detection share runtime infrastructure while preserving camera-local temporal state.

```text
Django / application control plane
    -> Camera Registry Reconciler
    -> Runtime Supervisor
        -> one global run_multiprocess parent
            -> one CameraIngest source/decode owner per physical camera
            -> capability-managed shared inference services
            -> camera-local Fight/Speed temporal consumers
            -> runtime-global Reporter + Incident processing
            -> bounded best-effort health/performance/attribution reporting
    -> durable incident outbox
    -> Django Incident Dispatcher
    -> common Incident / routing / authorization domain
```

The production design is deliberately **not** “one complete AI pipeline per camera”. Expensive/stateless inference is shared across cameras; temporal interpretation that depends on one camera’s history remains camera-local.

Target scale is large multi-camera deployment, but this contract does **not** assert a production camera-count guarantee. The current RTX 3050 measurements are characterization evidence for specific local-file workloads, not an RTX 5090 estimate, not a sustained RTSP SLA, and not evidence that 200/300 real cameras fit one node.

---

# 2. Non-negotiable invariants

1. Runtime workers must not import or depend on Django ORM.
2. Django/Gunicorn is the application/control plane, not AI child-process owner.
3. Runtime Supervisor owns the global production AI runtime lifecycle.
4. `run_multiprocess` owns the multiprocessing topology below the Supervisor.
5. One physical camera has one intended `CameraIngest` source/decode owner in the Supervisor-managed production runtime.
6. Fight and Speed on the same physical camera share one desired camera entry and one CameraIngest decode path.
7. `camera_worker` and `speed_worker` must not independently reopen a centralized production source.
8. Expensive/stateless inference models are shared services, not model-per-camera instances.
9. Fight temporal tracking/pair/ROI/event state and Speed tracking/calibration/speed state remain camera-local where history matters.
10. Shared inference services are capability-aware and run only while the desired camera set requires them.
11. Desired capability changes do not require a global runtime restart solely because capability requirements changed.
12. Camera work is fenced by stable slot + generation. Speed adds consumer epoch. Recoverable shared services add service epoch where required.
13. Fight durable publication additionally uses a service-incarnation publication floor.
14. Old/stale work fails closed and cannot become a current incident after camera/service reconfiguration.
15. Live and file workloads intentionally use different backpressure/recovery semantics.
16. Correctness must not depend on OS `Queue.qsize()` or `Queue.empty()` observations. Parent-owned scheduler accounting may be correctness input only where its semantics are controlled.
17. Queueing, telemetry, retries, health scans and operational cleanup remain bounded.
18. Windows `spawn` compatibility is a first-class constraint.
19. Runtime incident truth crosses into Django through the durable incident outbox; runtime workers do not create Incident ORM rows.
20. Fight and Speed share the same Django Incident/routing/authorization domain.
21. Optional service absence is healthy when that service is not required.
22. Runtime-global Incident/Reporter failure remains explicit; these processes are not silently reconstructed as optional capability services.
23. Synthetic camera-equivalents are not production inference capacity.
24. Measurements from one GPU/workload must not be linearly extrapolated to another GPU/workload.
25. Benchmark code must not mutate production thresholds, source ownership, queue semantics or recovery behavior merely to improve results.
26. PostgreSQL, Docker, Nginx, deployment/service packaging and UI redesign remain frozen/deferred unless explicitly promoted.
27. Shared-memory frame transport remains deferred until controlled measurement demonstrates transport is a material bottleneck.
28. **Ordered non-looping file EOF is correctness state, not telemetry.** The authoritative EOF fact is a generation-local multiprocessing Event owned by the current camera runtime and published by CameraIngest before consumer EOF signals.
29. A dead required file consumer is a clean drain only when authoritative EOF has been reached and that process exited with code `0`.
30. Clean EOF must not increment camera generation, reopen the source, replay the file, or synthesize watchdog recovery.
31. Attribution/performance telemetry is observation only. It must never drive health, admission, generation, service recovery, EOF, durable incident publication, or benchmark classification.
32. Missing/disabled/no-sample metrics remain unavailable/null; they must not be fabricated as zero.
33. Normal graceful finalization is distinct from failure recovery. A failed or poisoned service incarnation must not be reused merely to obtain final metrics.
34. Final telemetry is best-effort observability, not incident durability and not an ordered-file correctness signal.
35. Health snapshot publication failure is best-effort/non-fatal, but the error must remain observable; snapshot retry policy must stay bounded and must not hide persistent filesystem errors.
36. Failure reporting should preserve the cause observed **before teardown**. Exit codes caused by later forced termination must not be misrepresented as the original failure cause.
37. Person microbatching remains configurable but **OFF by default** unless a future architecture decision explicitly changes that default after target-workload validation.

---

# 3. Current top-level production flow

```text
Django / Web / DB
  |
  | persisted camera configuration + desired state
  v
CameraRegistryReconciler
  |
  v
Runtime Supervisor
  |
  | desired_cameras.json
  | run_config.supervisor-<run_id>.json
  v
fight.pipeline_mp.run_multiprocess
  |
  +--> Reporter                              runtime-global
  +--> Incident worker / IncidentAggregator runtime-global
  |
  +--> SharedServices                        parent-owned capability lifecycle
  |      |
  |      +--> Fight bundle, when required
  |      |      +--> shared Person worker
  |      |      +--> Person result router
  |      |      +--> shared Pose + router when configured
  |      |      +--> shared Stage3/X3D when configured
  |      |
  |      +--> Vehicle bundle, when required
  |             +--> shared Vehicle worker/model
  |
  +--> CameraRuntimeManager
         |
         +--> CameraIngest(camera N)
         |      +--> Fight frame/EOF channel     optional
         |      +--> Speed frame/EOF channel     optional
         |      +--> Preview frame/EOF channel
         |
         +--> camera_worker(camera N)            Fight only
         +--> speed_worker(camera N)             Speed only
         +--> camera_preview(camera N)
```

Fight path:

```text
CameraIngest
 -> camera_worker
 -> shared Person
 -> camera-local person/pair/ROI/temporal state
 -> shared Pose when configured/required
 -> shared Stage3/X3D when configured/required
 -> Incident worker / IncidentAggregator
 -> evidence + durable outbox
 -> Django Incident Dispatcher
 -> Incident(type=FIGHT)
 -> routing / ACK / escalation / resolve
```

Speed path:

```text
CameraIngest
 -> speed_worker
 -> camera-local ROI/calibration/motion/tracking
 -> shared Vehicle inference
 -> camera-local speed/violation decision
 -> evidence + durable outbox
 -> Django Incident Dispatcher
 -> Incident(type=SPEED)
 -> routing / ACK / escalation / resolve
```

There is no production-parallel Speed incident database or separate Speed dispatcher.

---

# 4. Ownership planes

## 4.1 Django/application plane owns

- `streams.Camera` and persisted camera configuration,
- `SpeedCameraConfig`,
- physical `Location` hierarchy,
- security units, coverage and user assignments,
- Incident ORM rows and audit/routing state,
- desired-camera publication,
- Supervisor start/stop requests,
- operator-facing status/preview/action endpoints,
- ORM-aware retention/evidence-reference protection,
- independent Incident Dispatcher service loop.

Django may observe runtime state and consume runtime-produced preview/evidence. It must not become a hidden second AI runtime or source owner.

## 4.2 Runtime Supervisor owns

Primary implementation:

```text
fight/runtime_supervisor/core.py::RuntimeSupervisor
```

It owns exactly one common `fight.pipeline_mp.run_multiprocess` parent in normal Supervisor mode. Supervisor states are:

```text
STOPPED STARTING RUNNING STOPPING FAILED BACKOFF
```

Local Supervisor state under `.runtime_supervisor/` is operational data, not repository source.

## 4.3 Runtime parent owns

Primary implementation:

```text
fight/pipeline_mp/run_multiprocess.py
```

The dynamic parent owns:

- spawn context,
- stable slots and generation arrays,
- Speed consumer epochs,
- Fight publication floor,
- Reporter and Incident workers,
- `SharedServices`,
- `CameraRuntimeManager`,
- desired-state polling/reconcile,
- health registry/watchdog/snapshot publication,
- fair admissions,
- file EOF/drain/finalization,
- performance-summary construction,
- global exit semantics.

Phase 19 changes normal shutdown ordering inside this ownership boundary; it does not move ownership into Django or into per-camera workers.

---

# 5. Desired camera state

Primary files:

```text
fight/runtime_supervisor/camera_state.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
```

Schema version is currently `1`. Canonical camera fields include:

```text
camera_id
source
name
enabled
use_fight_detection
use_speed_detection
speed_config
```

A camera is active when:

```text
enabled == true
AND (use_fight_detection == true OR use_speed_detection == true)
```

Supported modes are:

```text
Fight-only
Speed-only
Fight + Speed
neither / not started
```

Desired state has a monotonic revision; stale/equal revisions must not create duplicate lifecycle work. `speed_paused` is durable desired intent and stopping Speed is not equivalent to stopping Fight/common runtime.

Speed runtime configuration is bounded to required fields such as speed limit/tolerance, calibration path/revision, ROI and evidence flags. Duplicate active physical-source ownership is rejected by design.

`MAX_CAMERAS = 512` is a schema/registry validation bound, **not** a claim that 512 real cameras fit one machine.

---

# 6. CameraIngest and physical source ownership

Primary implementation:

```text
fight/pipeline_mp/camera_ingest.py
```

```text
                     +--> Fight consumer
Physical source ---> CameraIngest
                     +--> Speed consumer
                     +--> Preview
```

`camera_worker` and `speed_worker` must not independently reopen the centralized production source. Live reconnect ownership belongs to CameraIngest.

File/live publishing intentionally differs:

- ordered-file Fight/Speed delivery waits/defer rather than silently dropping required work,
- live freshness may supersede stale work,
- preview is a bounded latest-view concern,
- live reconnect is bounded/exponential and ingest-owned.

## 6.1 Authoritative ordered-file EOF — Phase 17

For a non-looping local file, each `CameraRuntime` owns a fresh `multiprocessing.Event` named conceptually `file_eof_event`. CameraIngest sets the Event **before** sending consumer EOF signals.

```text
legitimate non-looping read EOF
 -> set generation-local file_eof_event
 -> deliver EOF signal(s)
 -> consumers may finish/exit
 -> manager/watchdog may classify clean drain
```

The bounded HealthEvent channel and all attribution/reporting channels are best-effort observations. None of them owns EOF correctness.

The EOF Event belongs to one camera-runtime incarnation. A real restart receives a fresh Event; publication from an old generation cannot authorize clean completion of the replacement generation.

## 6.2 Ingest attribution — Phase 18

When performance metrics are enabled, CameraIngest can report bounded measurements for:

```text
read_ms
fight_enqueue_ms
speed_enqueue_ms
preview_enqueue_ms
fanout_ms
per-branch offered/enqueued/dropped counters
```

`read_ms` can include unsuccessful/EOF reads. “Enqueued” means publication succeeded at that boundary; it does not prove a downstream latest-frame consumer ultimately processed the frame.

Attribution does not add a second source owner, does not copy frames solely for telemetry, and does not alter file/live publication policy.

## 6.3 Browser preview ownership

Common-runtime preview is consumed from runtime-produced atomically replaced JPEG output. Supervisor-mode views do not open `camera.source` with a second `cv2.VideoCapture`. Direct-source calibration/legacy fallback is acceptable only in an explicitly non-Supervisor stopped legacy path.

---

# 7. Dynamic camera lifecycle

Primary implementation:

```text
fight/pipeline_mp/camera_lifecycle.py::CameraRuntimeManager
```

Per-camera runtime state includes desired definition, stable slot/generation, stop controls, Fight/Preview/Speed channels, generation-local file EOF Event, Speed epoch/failure/recovery state, Fight-service waiting state, lifecycle/file completion and restart counters.

Normal composition:

```text
Fight-only:   CameraIngest + camera_worker + preview
Speed-only:   CameraIngest + speed_worker + preview
Fight+Speed:  CameraIngest + camera_worker + speed_worker + preview
```

Source/capability/serialized Speed-config changes may require a camera-local restart; cosmetic metadata should not. Camera removal invalidates generation before teardown. Re-add/restart advances generation. Shared-service recovery can temporarily withdraw only affected consumers where architecture permits.

## 7.1 Ordered-file completion

Fight clean drain requires:

```text
file_eof_reached(item)
AND Fight consumer dead
AND Fight consumer exitcode == 0
```

Speed clean drain requires:

```text
file_eof_reached(item)
AND Speed consumer dead
AND Speed consumer exitcode == 0
```

Whole-camera terminal file completion requires clean ingest EOF plus completion of every required Fight/Speed consumer. A mixed camera cannot be marked cleanly complete merely because one branch finished.

A Speed worker that exits before authoritative EOF — including exit code `0` — is not clean completion. Existing `speed_failed` semantics latch, Speed consumer epoch is invalidated, the file is not replayed, and a later ingest EOF cannot rewrite that run as clean success.

A Fight worker dead before authoritative EOF, a non-zero Fight exit, or abnormal ingest exit remains failure/fail-closed according to existing policies. Clean EOF itself never advances generation or reopens the source.

## 7.2 Shared-service recovery locality

- Vehicle recovery withdraws affected Speed consumers while Fight/ingest/preview can remain intact where safe.
- Fight-bundle recovery withdraws affected Fight camera runtimes before transport replacement.
- A mixed Fight+Speed camera currently incurs a camera-local restart and brief Speed interruption during Fight recovery.
- Speed-only cameras remain untouched by Fight-bundle recovery.

That mixed-camera interruption is an optimization opportunity, not a correctness failure.

---

# 8. Identity and stale-work fencing

Base camera identity:

```text
(camera_id, slot_id, generation)
```

Slots are parent-owned reservations. Restart/removal invalidates the old generation. Delayed Person/Pose/etc. results and stale camera health events are rejected.

Speed adds:

```text
consumer_epoch
```

Stopping/replacing a Speed consumer advances its epoch. Vehicle work and durable Speed publication carry generation + consumer epoch.

Recoverable shared services add:

```text
service_epoch
```

Stale health/results from prior service incarnations cannot overwrite current state. Vehicle recovery replaces Vehicle request/result transport. Fight recovery replaces the Fight transport graph.

Fight additionally uses `fight_publication_floor`. Stage3 results carry the Fight service epoch into Incident/Aggregator. The parent raises the publication floor before failed Fight transport is touched, preventing old-incarnation segments/results from becoming current durable incidents.

Phase-18 attribution summaries are incarnation-aware using generation, Speed consumer epoch and/or service epoch where applicable. Summary loading must not merge incompatible percentile populations as though they were one current incarnation.

---

# 9. Shared inference services and capability lifecycle

Primary implementation:

```text
fight/pipeline_mp/shared_services.py::SharedServices
```

Required capabilities are derived from active desired cameras:

```text
fight   = any enabled camera requiring Fight
vehicle = any enabled camera requiring Speed
```

Fight bundle:

```text
person
person_router
pose + pose_router   when runtime.use_pose
stage3               when runtime.use_stage3
```

Vehicle bundle:

```text
vehicle
```

Runtime-global Incident and Reporter sit outside these capability bundles.

Behavior:

```text
first demand -> hot-start required bundle
continued demand -> reuse same service incarnation
last demand removed -> idle grace / required drain
normal withdrawal -> bounded graceful finalization
unrelated capability changes -> no global runtime restart
```

Default shared-service idle grace is 5 seconds. Configured-off Pose/Stage3 is expected absence, not a health failure.

The Vehicle worker receives the existing Reporter channel and Vehicle service epoch for attribution only; Reporter does not become Vehicle-owned and telemetry does not become recovery correctness state.

---

# 10. Phase 19 — graceful shared-worker finalization

Phase 19 fixes a lifecycle/observability gap: Person/Pose inference loops require queue sentinel termination to publish final worker/batch summaries. Merely setting a stop Event and quickly forcing process teardown could lose normal-shutdown telemetry even when inference itself completed correctly.

Primary implementation:

```text
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/performance.py
benchmarks/real_inference.py
```

## 10.1 Normal withdrawal semantics

`SharedServices.FINALIZE_TIMEOUT_SEC` is currently:

```text
8.0 seconds
```

This is **one shared grace budget per service bundle**, not eight seconds per worker.

For normal graceful withdrawal:

```text
bundle no longer required / clean runtime close
 -> start bounded sentinel publication to alive admission workers
 -> Person/Pose/etc. may exit through normal loop termination
 -> join admission workers within one common deadline
 -> set bundle stop Event
 -> bounded terminate/kill fallback for anything still alive
 -> close old transport
```

Sentinel publication occurs through a daemon helper thread so a poisoned/unusable admission lock cannot block the parent indefinitely. The parent waits only the remaining bounded deadline. Failure to deliver/finish does not authorize unbounded waiting or unsafe transport reuse.

The normal finalization path is specifically for healthy/ordinary lifecycle withdrawal. Failure recovery calls `_stop(..., graceful=False)` and **does not** enter this drain merely to save metrics.

## 10.2 Reporter ordering

On a clean dynamic-runtime exit:

```text
camera producers finish
 -> shared services close gracefully
 -> shared workers publish their final summary rows
 -> Reporter sentinel is sent with bounded helper
 -> Reporter exits / final file flush completes
 -> performance_summary.json is constructed
```

`run_multiprocess` uses:

```text
services.close(graceful = exit_code == 0)
```

A non-clean/failure exit does not pretend to be a normal telemetry-drain path.

The Reporter stop Event is not sufficient by itself to end Reporter before producers are done; tests explicitly verify the Reporter remains available until sentinel ordering permits final worker summaries to be written.

## 10.3 Final performance-summary retention

`build_performance_summary` now preserves, where supplied by workers:

```text
queue_wait_ms
inference_ms
result_enqueue_ms
steady_state
warmup_requests
batch
worker_timings.all_requests
worker_timings.steady_state
```

The worker timing views remain distinct from aggregated client timing views. Percentiles are not merged/averaged into a fabricated pooled distribution.

`benchmarks.real_inference.stage_latency_summary()` exports the existing latency distributions plus `batch` and `worker_timings` so real benchmark output retains the normal-finalization worker evidence.

## 10.4 Bounded failure behavior

If sentinel publication, normal worker exit, report transport or final reporter flush cannot complete, forced teardown remains bounded. Telemetry may be unavailable or partial; this is an observability limitation, not permission to weaken incident durability, EOF correctness or failure fencing.

---

# 11. Fight inference ownership

Person:

```text
camera_worker[N]
 -> fair per-slot Person admission
 -> one shared Person model/worker
 -> Person result router
 -> per-slot result channel
```

Pose:

```text
camera_worker[N]
 -> selected ROI
 -> fair Pose admission
 -> one shared Pose model/worker
 -> Pose router
 -> camera-local PoseGate/history
```

Stage3:

```text
camera_worker[N]
 -> bounded/fair Stage3 admission
 -> one shared Stage3/X3D worker
 -> Incident worker
 -> IncidentAggregator
```

IncidentAggregator must not instantiate a second local Person/Pose model. Shared Stage3 output is service-incarnation-tagged for Fight publication fencing.

Fight attribution can measure frame-delivery age, Person/Pose call wall time, Stage3 admission/enqueue wall time, completed frames and camera-local processing wall time. Camera-local timing subtracts actual measured nested shared-call elapsed time; it is not derived by subtracting unrelated percentiles.

---

# 12. Speed inference ownership

Primary runtime worker:

```text
fight/pipeline_mp/speed_worker.py
```

`speed_worker` owns camera-local:

- ROI/calibration,
- motion gate,
- tracker,
- speed estimator,
- violation decision/cooldown,
- evidence buffer/writer.

It does **not** own a YOLO Vehicle model.

```text
speed_worker[N]
 -> fair/bounded per-slot Vehicle admission
 -> shared vehicle_service_main
 -> one lazily created VehicleDetector/model
 -> per-slot VehicleResult
 -> speed_worker[N]
```

Vehicle result payloads do not send full frame pixels back. Vehicle inference/model errors are service failures, not “no detections”. Normal Django Speed control uses the common Runtime Supervisor; older standalone Speed helpers are compatibility/history, not production ownership.

Vehicle attribution includes accepted/completed/inference/stale counters and timings such as admission-inclusive wait, model initialization, detector-call wall time and result enqueue time. `queue_wait_inclusive_ms` begins before admission and therefore combines admission/defer, transport and service queueing; it must not be relabeled pure post-enqueue wait.

Speed-local attribution includes Vehicle call/round-trip/enqueue timings, frame-delivery age, processor initialization, preprocessing, tracking, speed decision, visualization/evidence and local processing.

---

# 13. Shared-service recovery and failure isolation

## 13.1 Vehicle

Confirmed Vehicle failure:

```text
observe failure cause
 -> capture pre-teardown Vehicle exit code if one already exists
 -> mark Vehicle unavailable
 -> withdraw affected Speed consumers
 -> invalidate Speed consumer epochs
 -> close failed Vehicle transport
 -> bounded backoff
 -> fresh Vehicle transport + fresh service epoch
 -> resume eligible LIVE Speed consumers
```

Defaults:

```text
VEHICLE_SERVICE_RESTART_LIMIT=3
VEHICLE_SERVICE_RESTART_BACKOFF_SEC=2
VEHICLE_SERVICE_RESTART_MAX_BACKOFF_SEC=30
```

File Speed is never replayed as clean work after partial service failure. Vehicle exhaustion remains isolated from healthy Fight where possible.

## 13.2 Fight bundle

Recoverable components:

```text
person
person_router
pose
pose_router
stage3
```

Confirmed Fight failure recovers the whole Fight inference bundle because transport/result ownership forms one service-incarnation boundary:

```text
observe component failure identity
 -> raise fight_publication_floor
 -> invalidate affected generations
 -> mark Fight cameras waiting
 -> withdraw affected Fight readers/runtimes
 -> close old Fight transport
 -> bounded backoff
 -> fresh Fight bundle + fresh service_epoch
 -> resume eligible LIVE affected cameras
```

Defaults:

```text
FIGHT_SERVICE_RESTART_LIMIT=3
FIGHT_SERVICE_RESTART_BACKOFF_SEC=2
FIGHT_SERVICE_RESTART_MAX_BACKOFF_SEC=30
```

Fight file touched by a shared Fight failure is incomplete/no-replay. Unsafe withdrawal, retry exhaustion, initial Fight startup failure and runtime-global Incident/Reporter failure remain fail-closed/fatal boundaries.

## 13.3 Runtime-global processes

Incident and Reporter remain runtime-global. Their confirmed death is a global runtime failure/stop boundary rather than an optional service recovery event. Phase-18/19 telemetry reuses Reporter; it does not create another durable truth path.

---

# 14. Health architecture

Primary files:

```text
fight/pipeline_mp/health.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/core.py
```

```text
child workers
 -> bounded HealthEvent queue
 -> runtime parent
 -> HealthRegistry
 -> RuntimeWatchdog
 -> runtime_health.json
 -> Supervisor health projection
```

Health is bounded and uses monotonic time where appropriate. Camera components include ingest, Fight worker, preview and Speed worker. Shared records include Person/router, Pose/router, Stage3, Incident and Vehicle. Reporter remains runtime-global liveness-critical even though it need not appear as a normal inference health record.

Capability metadata includes:

```text
required
service_state
service_epoch
restart_count
```

Optional disabled services are represented as healthy `service_disabled` rather than failures.

## 14.1 EOF/exit classification

Health snapshots project authoritative file EOF state from CameraRuntimeManager. RuntimeWatchdog observes per-camera process liveness **and exit code** before classifying dead required file consumers.

A dead Fight consumer is exempt from `FAILED/process_dead` only when authoritative EOF exists and exit code is `0`. Missing/unknown/non-zero exit status remains failure. A legitimately drained file consumer may stay in a draining state until manager convergence without provoking a false restart.

The same core clean-drain rule applies to Speed, with Speed-specific `speed_failed` handling preserved.

## 14.2 Attribution is not health

Attribution uses best-effort Reporter messages, not HealthEvent correctness state. It does not participate in watchdog decisions, EOF ownership or benchmark HEALTHY/PRESSURED/SATURATED/INCOMPLETE classification.

---

# 15. Phase 19.1 — Windows health snapshot reliability

Primary implementation:

```text
fight/pipeline_mp/health.py::HealthSnapshotStore
fight/pipeline_mp/run_multiprocess.py
```

Windows can reject atomic replacement of an open destination when a reader/scanner does not share delete/replace access. A real Windows regression test reproduced this behavior with a reader holding the current snapshot open.

The snapshot writer still writes complete JSON to the temporary path before replacement. Only the replacement operation is retried.

Retry policy:

```text
retry only OSError where winerror is 5, 32 or 33
attempt 1 -> immediate replace
failure -> sleep 20 ms
attempt 2 -> replace
failure -> sleep 40 ms
attempt 3 -> replace
failure -> sleep 80 ms
attempt 4 -> replace or raise
maximum added retry sleep = 140 ms
```

Important guarantees:

- the old complete snapshot remains intact until a replacement succeeds;
- the temporary snapshot contains a complete JSON serialization before replace;
- the same bounded replacement sequence does not unlink the old destination first;
- unrelated errors are not broadly retried;
- persistent access/permission failure still escapes after the fourth attempt;
- disk/full/missing-path style errors remain visible rather than being hidden by a generic retry loop;
- a later successful write can recover after an earlier exhausted transient failure.

The dynamic runtime catches snapshot-publication exceptions at the best-effort health-output boundary and emits a status row including:

```text
detail = health_snapshot_write_failed
error = exception class
errno
winerror
```

That snapshot publication failure remains non-fatal by design. It does **not** convert an otherwise healthy runtime into a forced restart simply because the observational file could not be replaced at that moment.

This is not a general filesystem durability redesign. The snapshot writer preserves the existing atomic-replace level of semantics; Phase 19.1 specifically hardens transient Windows replacement denial.

---

# 16. Phase 19.1 — shared-service failure identity preservation

Prior recovery status could collapse useful information into a generic `vehicle_service_restarting` detail after the runtime had already decided why the service failed. Phase 19.1 preserves the observed failure identity through teardown/recovery reporting.

Vehicle status now retains, where applicable:

```text
detail
component = vehicle
reason
component_failure = vehicle_<reason>
retries
service_epoch
exit_code
```

Distinct reasons include paths such as:

```text
process_dead
heartbeat_timeout
inference_stall
start_failed
replacement_start_failed
```

For a process that is already dead when health classification occurs, the existing process exit code is captured **before** withdrawal/forced teardown.

For an `inference_stall`, the worker may still be alive when the failure is decided. Its pre-teardown `exit_code` is therefore `None`. A later terminate/kill exit code is a consequence of recovery and must not be reported as the original stall cause.

Fight shared-service failure reporting similarly preserves the failing component and pre-teardown exit identity where available. The Fight-specific `reason` may still reflect architecture-level failure semantics such as file-incomplete/recovery-exhausted, while `component_failure` preserves the component-level health trigger.

These changes are diagnostic/observability hardening only. Recovery budgets, file no-replay rules, transport replacement, generation/service-epoch fencing and Fight/Speed isolation are unchanged.

---

# 17. Fair scheduling and bounded capacity

Primary implementation:

```text
fight/pipeline_mp/scheduling.py::FairRequestQueue
```

Shared fair stages are Person, Pose, Stage3 and Vehicle. Per-slot bounded pending work plus round-robin dispatch prevents one hot camera from monopolizing a single shared FIFO.

Capacity/accounting includes:

```text
capacity
pending_per_camera
outstanding
accepted
rejected_capacity
deferred_file
dropped_live
stale_generation
dispatches
high_water
per-slot counters
```

Live workloads may shed stale work explicitly. Ordered files wait/defer. Dropped/stale work must not be converted into a synthetic negative detection.

OS queue `qsize()`/`empty()` observations are telemetry/convenience only unless a custom parent-owned structure explicitly defines controlled correctness semantics.

Attribution augments this accounting but does not replace scheduler counters and cannot alter admission outcomes.

---

# 18. Live vs ordered-file semantics

## LIVE / RTSP

Priority:

```text
freshness > completeness
```

Expected behavior:

- bounded queues,
- stale live frame/inference shedding,
- ingest-owned reconnect,
- bounded camera-local retries,
- eligible live Speed resume after Vehicle recovery,
- eligible live Fight resume after Fight recovery,
- unrelated services survive local/recoverable faults where safe.

## FILE

Priority:

```text
correctness + ordering > freshness
```

Expected behavior:

- ordered work waits/defer instead of silently dropping required inference,
- admitted Stage3 work drains before normal completion where required,
- non-looping EOF is authoritative only through the generation-local EOF Event,
- EOF Event is set before consumer EOF delivery,
- required consumer exit is clean only with authoritative EOF + exit code `0`,
- clean EOF never causes watchdog restart, generation advance, source reopen or replay,
- mixed Fight+Speed waits for all required consumers,
- pre-EOF/non-zero Speed failure remains latched/fail-closed,
- Fight file affected by shared Fight failure remains incomplete/no-replay,
- benchmark deadline truncation is `INCOMPLETE`, never successful throughput.

Telemetry loss, snapshot replacement failure or stale attribution must not modify these rules.

---

# 19. Performance and attribution model

Primary files:

```text
fight/pipeline_mp/attribution.py
fight/pipeline_mp/performance.py
benchmarks/real_inference.py
benchmarks/telemetry.py
```

The runtime separates several measurement populations rather than flattening them into one ambiguous latency:

- camera-local call timings,
- shared-worker queue/inference/result-enqueue timings,
- all-request worker distributions,
- steady-state worker distributions,
- batch-level distributions,
- per-camera attribution,
- process/host/GPU samples.

Phase 19 explicitly retains worker-originated timing/batch views through clean shutdown. These views must remain labeled according to their population.

Examples of important distinctions:

- `queue_wait_inclusive_ms` is not pure post-enqueue queue delay;
- frame-delivery age is not pure IPC-copy latency;
- detector-call wall time is not CUDA-kernel duration;
- a mean of multiple run-level p95 values is not a pooled p95;
- per-request inference and per-batch inference are different populations;
- missing samples are unavailable, not zero.

Pure IPC-copy time, synchronized CUDA-kernel duration, reliable incident end-to-end latency and true sustained live per-camera FPS remain unavailable at current instrumentation boundaries.

---

# 20. Person microbatching contract

Microbatch support exists, but production defaults remain conservative:

```text
person_batch_enabled = false
person_batch_size = 1
person_batch_max_wait_ms = 0
```

Batching is a configurable execution profile, not a correctness requirement and not a universal optimization.

The current RTX 3050 Mixed-8 evidence shows Batch-2 / 5 ms can reduce Person queue pressure and improve aggregate throughput in that particular traffic workload. It does **not** establish Batch-2 as a global production default, an RTX 5090 optimum, or the best setting for Fight-heavy workloads where Pose/Stage3 are active.

Batch-4 was evaluated and reduced Person queue pressure further, but increased Vehicle contention/latency and produced slightly lower total mixed-system throughput than Batch-2 in the comparison run. The architecture therefore keeps the knob available and the default OFF.

---

# 21. Incident durability boundary

Primary files:

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/speed_worker.py
```

Runtime produces evidence and durable incident envelopes before Django ingestion.

Core semantics include:

- append-only JSONL incident outbox,
- serialized writers,
- flush/fsync before successful publication where designed,
- partial-tail preservation,
- persistence failure surfacing,
- evidence durability before incident publication,
- stale generation/epoch guards,
- Fight service-incarnation publication-floor fencing,
- Speed generation + consumer-epoch fencing.

Runtime never directly creates Incident ORM rows. Reporter/attribution/health snapshots are not substitutes for durable incident truth.

---

# 22. Django incident/application domain

Primary application entities include common Incident, routing rules/routes, audit events, ingest cursor and ingest records. Incident types include:

```text
FIGHT
SPEED
OTHER
```

```text
durable runtime outbox
 -> run_incident_dispatcher
 -> incidents.services.ingest
 -> Incident ORM row
 -> location/security routing
 -> ACK / resolve / escalation / audit
```

Dispatcher is an application service independent of global AI runtime ownership. Evidence deletion must respect Incident references and retention protections.

---

# 23. Location and authorization

Primary application entities:

```text
adminx.Location
adminx.SecurityUnit
adminx.SecurityUnitCoverage
adminx.UserSecurityAssignment
streams.Camera
```

```text
User
 -> SecurityUnit assignment
 -> Coverage
 -> Location hierarchy
 -> Camera.location
 -> incident/preview/action visibility
```

Fight and Speed share one physical authorization/location domain. Legacy `Camera.faculty` may remain for compatibility; organizational access decisions belong in Django, not model workers.

---

# 24. Runtime durability, retention and generated artifacts

Phase-12 durability remains binding. Supervisor/desired state is durable/atomic where designed, outbox/evidence durability is explicit, cleanup scans are bounded, active/unknown/abnormal runs fail closed against unsafe cleanup, Incident-referenced evidence is protected, evidence retention is indefinite by default unless explicitly configured, disk pressure is observable without restart storms, and long-running operational jobs use singleton service loops/locks.

Generated runtime/benchmark content is not source code. Known local operational paths include:

```text
.runtime_supervisor/
Fight_backend_project/backend_frontend_project/media/camera_uploads/
Fight_backend_project/backend_frontend_project/media/pipeline_runs/.run_locks/
Fight_backend_project/backend_frontend_project/media/runtime_spool/
benchmarks/results/
benchmarks/.capacity.lock
phase*_review.diff
```

Repository ignore rules must keep these generated paths out of source commits. Benchmark evidence directories are local immutable-by-convention experiment outputs: each run gets a new directory; results are not overwritten merely to obtain a cleaner number.

---

# 25. Capacity benchmark subsystem

Primary files:

```text
benchmarks/__main__.py
benchmarks/control_plane.py
benchmarks/real_inference.py
benchmarks/telemetry.py
benchmarks/README.md
```

## 25.1 Measurement taxonomy

```text
REAL INFERENCE
 = real Supervisor/runtime + local ordered-file decode
 + real models/workers + real queue/recovery/health behavior
 + bounded attribution when enabled

CONTROL PLANE / SYNTHETIC
 = real parent-side lifecycle/state/scheduling structures
 + inert/fake execution
 - no model inference
 - no real decode
 - no real inference capacity claim
```

Synthetic camera-equivalents must never be called real inference capacity.

## 25.2 Real-mode isolation

Real mode uses the common RuntimeSupervisor/`run_multiprocess`, preserves model/detection/queue/recovery semantics, uses isolated result/Supervisor/outbox paths, disables automatic whole-runtime restart for measurement, refuses conflicting production runtime/benchmark ownership, rejects credential-bearing remote sources, and does not auto-download missing model weights.

Logical benchmark cameras reusing one source receive distinct hardlinks/copies and identities to satisfy source uniqueness. This is same-content ordered-file stress, not physical RTSP equivalence.

## 25.3 Classification

```text
HEALTHY
PRESSURED
SATURATED
INCOMPLETE
```

`INCOMPLETE` covers deadline/non-zero exit/missing required reports or samples/failed required consumer/observed recovery/failed runtime health/no usable frame summary. `SATURATED`/`PRESSURED` use observed shedding/rejection/queue criteria. GPU utilization alone cannot set saturation. Ordered-file defer/retry is not live drop. Attribution does not alter classification.

---

# 26. Measured characterization evidence

All measurements below are workload/hardware-specific observations, not production promises. Development characterization hardware:

```text
NVIDIA GeForce RTX 3050 Laptop GPU, 6 GB
Intel Core i7-13700H
64 GB RAM
Windows
```

Full-run aggregate processing FPS includes startup/drain/EOF effects and is not steady-state live RTSP FPS.

## 26.1 Synthetic control-plane acceptance

Phase-16 mixed control-plane scenarios at 50/100/200/300 camera-equivalents passed slot/generation/bounded-storage/scheduler-fairness checks. This establishes no immediate 300-entry wall in those synthetic parent-side structures only. It does **not** establish 300-camera inference.

## 26.2 Fight-only real inference

Selected healthy characterization using the Fight ordered-file workload:

```text
cameras   aggregate FPS
2         28.67
4         49.89
8         65.55
12        76.80
```

The post-Phase-17 12-camera run completed:

```text
903 frames per camera
10,836 total frames
Person/Pose/Stage3 completed: 5004 / 3732 / 72
Person queue p95: ~206.9 ms
Pose queue p95: ~132.1 ms
GPU mean: ~57.5%
GPU p95: ~86%
VRAM: ~640 MiB
recovery/replay/restart: none
classification: HEALTHY
```

This is “12-camera workload characterized”, not “12 cameras supported”.

## 26.3 Speed-only attribution reruns — Phase 18

Healthy selected points:

```text
8 cameras
  aggregate FPS: 82.70
  Vehicle requests/results: 1344 / 1344
  Vehicle queue wait mean/p95: 77.19 / 119.28 ms
  Vehicle inference mean/p95: 29.13 / 42.27 ms
  CPU mean: 51.45%
  GPU mean/p95/max: 22.47% / 43.3% / 45%
  recovery: none

12 cameras
  aggregate FPS: 89.25
  Vehicle requests/results: 2016 / 2016
  Vehicle queue wait mean/p95: 125.50 / 256.63 ms
  Vehicle inference mean/p95: 29.89 / 51.55 ms
  CPU mean: 56.69%
  GPU mean/max: 26.01% / 53%
  recovery: none
```

From 8 to 12 cameras, camera count rises 50% while aggregate throughput rises only:

```text
(89.25 / 82.70 - 1) * 100 = 7.9201935%
```

Queue latency rises materially while sampled GPU utilization remains moderate. The evidence is more consistent with shared Vehicle serialization/scheduling, arrival pressure and backpressure than with simple raw-GPU saturation. It does **not** isolate one exclusive cause and does not prove IPC-copy causality.

## 26.4 Mixed Fight + Speed attribution context

Mixed benchmarks use the Speed traffic source/calibration and enable Fight + Speed on every logical camera, sharing one CameraIngest decode. The traffic content did not produce Pose/Stage3 work in the observed Mixed-8 runs, so these runs characterize common ingest + Person + Vehicle + Speed-local contention, **not** full Fight-event/Pose/Stage3 contention.

A healthy Phase-18 Mixed-8 attribution run observed approximately:

```text
aggregate FPS: 47.76
Person requests/results: 2600 / 2600
Vehicle requests/results: 1344 / 1344
Person queue p95: ~205 ms
Vehicle queue mean/p95: ~14.8 / 45.3 ms
Vehicle inference mean/p95: ~24.5 / 34.3 ms
CPU mean: ~45.3%
GPU mean/max: ~33.2% / 73%
recovery/drop/rejection: none at the relevant shared admission boundaries
```

Different instrumentation/finalization changes and run-to-run system conditions mean historical points should not be treated as identical microbatch baselines unless they belong to the explicit comparison pairs below.

---

# 27. Phase 19 Person microbatch characterization

Two healthy OFF vs Batch-2 comparison pairs exist for the same Mixed-8 traffic-style workload.

Pair A:

```text
OFF
  aggregate FPS: 40.99
  wall processing: 132.32 s
  Person queue mean/p95: 171.57 / 197.40 ms
  Person inference mean: 29.67 ms

Batch-2 / 5 ms
  aggregate FPS: 45.15
  wall processing: 120.12 s
  Person queue mean/p95: 105.22 / 132.13 ms
  actual batch mean: 1.83
  batch inference mean/p95: 41.81 / 56.47 ms
```

Pair B, after Phase 19.1:

```text
OFF
  aggregate FPS: 42.370409
  wall processing: 128.013870 s
  Person queue mean/p95: 166.882547 / 206.519370 ms
  Person inference mean: 24.744726 ms
  batch disabled

Batch-2 / 5 ms
  aggregate FPS: 43.578609
  wall processing: 124.464735 s
  Person queue mean/p95: 102.568315 / 132.865525 ms
  Person inference mean: 45.442546 ms
  actual batch mean/p95: 1.988281 / 2
  batch collect wait mean: 1.205650 ms
  batch inference mean/p95: 47.651872 / 57.641125 ms
```

Arithmetic means across the two comparison pairs using the displayed inputs:

```text
aggregate FPS
  OFF = 41.6802045
  B2  = 44.3643045
  relative change = +6.4397477%

wall processing seconds
  OFF = 130.166935
  B2  = 122.2923675
  relative change = -6.0495912%

Person queue mean
  OFF = 169.2262735 ms
  B2  = 103.8941575 ms
  relative change = -38.6063669%

mean of run-level Person queue p95 values
  OFF = 201.959685 ms
  B2  = 132.4977625 ms
  relative change = -34.3939547%
```

The last statistic is **not a pooled p95**; it is only a descriptive arithmetic mean of two run-level p95 values.

Batch-2 processes approximately two requests per inference batch, so the longer per-batch inference call is expected and cannot be labeled a regression by itself. The relevant tradeoff is aggregate throughput, queue pressure, competing Vehicle latency, and end-to-end system behavior.

Batch-4 / 5 ms was also healthy. It lowered Person queue mean to about 37.51 ms but increased Vehicle queue mean/p95 to about 76.96/168.36 ms versus Batch-2’s about 39.19/103.19 ms in that experiment; total throughput was 44.67 FPS versus Batch-2’s 45.15 FPS. Batch-2 was therefore the better tested tradeoff on this RTX 3050 mixed workload, not a universal optimum.

Production defaults remain OFF.

---

# 28. Post-19.1 real Windows validation

Final Mixed-8 OFF and Batch-2 runs after Phase 19.1 both completed `HEALTHY` with no observed recovery.

For each run, shared-stage accounting recorded:

```text
Person accepted/dispatched: 2600 / 2600
Vehicle accepted/dispatched: 1344 / 1344
Person rejected_capacity: 0
Vehicle rejected_capacity: 0
Person dropped_live: 0
Vehicle dropped_live: 0
Person stale_generation: 0
Vehicle stale_generation: 0
Person restart_count: 0
Vehicle restart_count: 0
```

A scan of the two final runtime status streams found:

```text
health_snapshot_write_failed
  OFF: 0
  Batch-2: 0
```

This is successful real-runtime validation consistent with the targeted Windows replacement regression tests. It does **not** prove transient sharing failures can never recur; the architecture retains bounded retry plus observable non-fatal failure reporting for that reason.

---

# 29. Historical performance context

Older pre-later-phase dense-file RTX 3050 measurements were approximately:

```text
1 cam ~21.29 FPS
2 cam ~41.56 FPS
4 cam ~59.58 FPS
8 cam ~68.73 FPS
```

They are historical context only. Runtime architecture, workload and instrumentation changed; they must not be compared blindly with current ordered-file runs.

Earlier pre-attribution Speed scaling points also remain historical context. Where newer Phase-18 attribution reruns exist, the newer selected 8/12 points should be preferred for bottleneck interpretation.

---

# 30. Phase 1–12 foundation ledger

- **Phase 1 — Shared Person:** one shared Person model/worker; camera-local Motion/stabilizer/tracking/pair/ROI/event/prebuffer; explicit request identity.
- **Phase 2 — Shared Pose:** one shared Pose service/router; camera-local Pose temporal interpretation; duplicate incident-side model ownership removed later.
- **Phase 3 — Performance observability:** bounded timing/queue/inference/delivery metrics and machine-readable summaries.
- **Phase 4 — Microbatching capability:** latency-bounded batching support while conservative defaults keep batching effectively disabled unless configured.
- **Phase 5 — Centralized CameraIngest:** one source/decode owner feeding Fight/Preview and later Speed; ordered-file vs live freshness policy.
- **Phase 6 — Runtime Supervisor:** standalone authenticated local owner with durable state/PID/config and Windows-aware stop behavior.
- **Phase 7 — Organization/access:** Location/SecurityUnit/Coverage/UserAssignment and `Camera.location`.
- **Phase 8 — Durable incidents/routing:** evidence + outbox -> independent Django dispatcher -> common Incident/routing domain; runtime ORM-free.
- **Post-8 stabilization:** removed duplicate incident inference, hardened browser/media/report/cursor/EOF behavior.
- **Phase 9 — Dynamic lifecycle:** desired revisions, stable slots, camera generations, in-parent add/remove/restart.
- **Phase 10 — Health/watchdog:** bounded health events, HealthRegistry/Watchdog, atomic runtime health snapshots.
- **Phase 11 — Fair scheduling/capacity:** per-slot bounded admission/round-robin fairness; live shedding vs file defer semantics.
- **Phase 12 — Operational durability/retention:** bounded cleanup/retention, locks, disk-pressure health, serialized/fsynced durable writes and resilient service loops.

These remain active assumptions for later phases.

---

# 31. Phase 13 — Shared Speed integration

Code baseline:

```text
418f65bf137cfb31aa92629ac8fe1e03a0a1c54a
feat: integrate speed detection into shared runtime
```

Established Fight-only/Speed-only/Fight+Speed modes, single CameraIngest fan-out, shared Vehicle inference, camera-local Speed state, bounded Vehicle admission, Speed generation+epoch fencing, file fail-closed/live recovery distinction, common durable Speed incident path, common Supervisor control and `speed_paused` desired intent.

Validation at that phase: `135 passed, 1 skipped`.

---

# 32. Phase 14 — Capability lifecycle and Vehicle recovery

Code baseline:

```text
7b395ef94f04b435862ab013f9226b0982de34b6
feat: add capability-aware shared service lifecycle
```

Established `SharedServices`, Fight-only without Vehicle, Speed-only without Fight inference bundle, same-runtime bundle hot start/stop, bounded idle grace, Fight drain before ordinary bundle shutdown, Vehicle crash/stall/start-failure recovery, Vehicle transport replacement/service epoch, bounded retry/backoff, live Speed resume, file no-replay, optional-service health and common-preview source ownership.

Validation at that phase: `148 passed, 1 skipped`.

---

# 33. Phase 15 — Resilient Fight service recovery

Code baseline:

```text
c3c019b2871dcd3891b24ef242a2c5e93fe9212f
feat: add resilient fight service recovery
```

Established bounded in-runtime recovery for Person/routers/Pose/Stage3, whole-Fight transport replacement, generation invalidation before withdrawal, Fight service epochs, Stage3->Incident epoch propagation, publication floor, eligible LIVE resume, FILE fail-closed/no replay, bounded retry/start-failure accounting, fatal exhaustion/unsafe teardown boundaries and preservation of Vehicle/Speed-only identity.

Validation at that phase: `162 passed, 1 skipped`.

---

# 34. Phase 16 — Capacity benchmark harness

Code baseline:

```text
022d2fd5a3a6cef4ece0ac1b7434b9b9493512a0
feat: add capacity benchmark harness
```

Added real-vs-synthetic separation, reuse of Supervisor/runtime in real mode, synthetic reuse of production parent-side structures with inert execution, bounded telemetry, optional NVIDIA fallback, redacted/isolated results, singleton/concurrency protection, explicit classification semantics, no automatic stress sweep, no production behavior mutation and no camera-count claim from registry bounds.

Implementation validation: `170 passed, 1 skipped` plus compileall, Django check, migration check and diff check.

---

# 35. Phase 17 — Ordered-file EOF/watchdog hardening

Code baseline:

```text
3b9733f20a3ca4d3773c63fed0caba7939f2f27c
fix: harden ordered file EOF lifecycle
```

Trigger: a pre-fix 12-camera Fight benchmark exposed a race where ingest had reached EOF and Fight consumers were legitimately draining/exiting, but manager/watchdog observations could classify the clean exit as `process_dead`, restart the camera, advance generation and replay the file.

Phase 17 established:

```text
authoritative generation-local multiprocessing Event for non-looping file EOF
CameraIngest sets EOF Event before consumer EOF delivery
health EOF telemetry remains best-effort
clean Fight/Speed consumer exit requires authoritative EOF + exitcode 0
pre-EOF/non-zero failures remain failures
later EOF cannot rewrite failed Speed file as clean success
mixed Fight+Speed completion waits for every required consumer
clean EOF causes no generation advance/watchdog restart/source reopen/replay
replacement camera runtime gets a fresh EOF Event
```

Reported validation: focused lifecycle/health/speed coverage `77 passed`; full pytest `179 passed, 1 skipped`; compileall and diff check passed. The post-fix Fight-12 real run then completed HEALTHY with exact ordered workload and no replay/recovery.

---

# 36. Phase 18 — Bottleneck attribution telemetry

Code baseline:

```text
823f87da3b4663e915085da8fd2145043e84175a
feat: add bottleneck attribution telemetry
```

Trigger: Fight, Speed and mixed RTX 3050 curves showed diminishing returns, but existing metrics could not distinguish shared inference serialization from decode, multiprocessing transport, preprocessing or camera-local work.

Phase 18 added observation boundaries only:

```text
bounded AttributionMetrics using existing performance sampling controls
nonblocking Reporter publication
per-incarnation generation/consumer_epoch/service_epoch attribution identity
CameraIngest read/fanout/branch publication timings and counters
Fight local/shared-call attribution
Vehicle accepted/completed/inference/stale counters and wall timings
Speed Vehicle-call/local preprocessing/tracking/decision/evidence timings
Speed completed-frame counter
Preview delivery-age/received count
latest-attribution status loading bounded separately from legacy history
real benchmark exports attribution while classification remains unchanged
unavailable metrics remain null instead of guessed
```

Important semantics:

- Vehicle `requests_completed` increments only after successful result publication.
- Vehicle `result_enqueue_ms` is one observation per successfully published result and includes result-queue retry waiting.
- Capture/staleness clocks were not repurposed.
- Camera-local timing subtracts actual nested shared-call elapsed totals.
- Telemetry failure never changes EOF, health, admission, recovery, durable publication or classification.
- Pure IPC-copy time, CUDA kernel time, reliable incident E2E latency and steady-window live FPS remain unavailable.

Validation at final Phase-18 review: focused affected tests `80 passed`; full pytest `196 passed, 1 skipped`; compileall and diff check passed.

---

# 37. Phase 19 — Shared-worker telemetry finalization

Current baseline includes Phase 19 in commit:

```text
fefedcc81095a5b808f048d1bc7daa13e04c0b1f
Finalize Phase 19 shared worker telemetry and Windows health reliability
```

Trigger: normal dynamic service withdrawal could set stop and force teardown before Person/Pose workers received the sentinel required to leave their loop and emit final inference/batch summaries. Benchmark runs could therefore finish successfully yet lose final worker telemetry.

Established guarantees:

```text
normal shared-service withdrawal uses bounded graceful finalization
one 8-second grace budget per bundle
sentinels are offered to alive admission workers
sentinel publication itself cannot block the parent indefinitely
workers get remaining-budget join time
forced terminate/kill fallback remains bounded
failure/recovery withdrawal skips graceful finalization
clean dynamic exit closes shared services before Reporter EOF
Reporter is flushed/stopped before performance summary construction
result_enqueue_ms is retained in performance summary
worker all-request / steady-state distributions are retained separately
batch configuration/count/size/collect/inference distributions are retained
real benchmark latency export includes batch + worker timing views
production Person batching default remains OFF
```

Executable contract coverage includes real `spawn` Person worker + Reporter finalization, capability removal, clean parent close, file-EOF close ordering, metrics-disabled behavior and an unusable-admission/unresponsive-worker bounded-close case.

Phase-19 microbatch experiments then compared OFF, Batch-2 and Batch-4 without changing the production default.

---

# 38. Phase 19.1 — Windows snapshot + failure-reason hardening

Phase 19.1 is part of the same current commit baseline.

Trigger 1: repeated Windows `PermissionError` observations while replacing `runtime_health.json`. A targeted Windows test reproduced the case where an open reader blocks destination replacement.

Trigger 2: Vehicle recovery status exposed `vehicle_service_restarting` but could lose the concrete worker-health reason that caused recovery; teardown could also overwrite interpretation of the original process state.

Established guarantees:

```text
HealthSnapshotStore retries only transient Windows replacement winerrors 5/32/33
four total replace attempts
sleep sequence 20/40/80 ms
maximum retry backoff 140 ms
last complete snapshot remains until successful replace
unrelated/persistent filesystem errors still surface
snapshot publication remains non-fatal/best-effort
snapshot failure status includes errno + winerror

Vehicle recovery keeps component/reason/component_failure
Vehicle captures pre-teardown exit code when available
inference stall keeps exit_code=None if worker was alive at decision time
start_failed and replacement_start_failed remain distinct
Fight worker failure preserves failing component and pre-teardown exit identity
recovery budgets/fencing/ownership semantics are unchanged
```

Regression tests cover Windows replacement success after transient denial, exhausted retry preserving the old JSON, unrelated errors receiving no retry, a real Windows open-reader replacement denial, non-fatal parent reporting, Vehicle process-dead/stall/heartbeat/start/replacement failures, retry exhaustion and Fight component identity.

Current full validation after the final timing-bound test correction:

```text
214 passed, 1 skipped
compileall fight benchmarks tests: passed
git diff --check: passed
```

---

# 39. Task router for coding agents

Read this document first. Inspect the current Supervisor-managed ownership path before editing. Do not infer production architecture from legacy helpers.

## Supervisor / desired state

```text
fight/runtime_supervisor/core.py
fight/runtime_supervisor/camera_state.py
fight/runtime_supervisor/locking.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/fight_runner.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
```

## Dynamic lifecycle / EOF / capability ownership

```text
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/health.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/generation.py
tests/test_camera_ingest.py
tests/test_dynamic_camera_lifecycle.py
tests/test_runtime_health.py
tests/test_speed_integration.py
tests/test_shared_services.py
tests/test_fight_service_recovery.py
tests/test_shared_service_finalization.py
tests/test_health_snapshot_reliability.py
```

## Fight inference

```text
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/scheduling.py
fight/pipeline/incident_aggregator.py
```

## Speed / Vehicle recovery

```text
fight/pipeline_mp/speed_worker.py
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/health.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/common_preview.py
Fight_backend_project/backend_frontend_project/services/speed_bridge/speed_runner.py
Fight_backend_project/backend_frontend_project/speed_detection/views.py
HizTespiti/speed/src/*
HizTespiti/yolo/src/vehicle_detector.py
tests/test_speed_integration.py
tests/test_shared_services.py
```

## Health / snapshot / recovery identity

```text
fight/pipeline_mp/health.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/camera_lifecycle.py
fight/runtime_supervisor/core.py
tests/test_runtime_health.py
tests/test_shared_services.py
tests/test_fight_service_recovery.py
tests/test_health_snapshot_reliability.py
```

## Graceful finalization / Reporter / performance summary

```text
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/reporter.py
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/performance.py
benchmarks/real_inference.py
tests/test_shared_service_finalization.py
```

## Attribution / performance / capacity characterization

```text
fight/pipeline_mp/attribution.py
fight/pipeline_mp/performance.py
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_preview.py
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/speed_worker.py
fight/pipeline_mp/shared_services.py
benchmarks/README.md
benchmarks/__main__.py
benchmarks/real_inference.py
benchmarks/control_plane.py
benchmarks/telemetry.py
tests/test_attribution_telemetry.py
tests/test_capacity_benchmarks.py
tests/test_shared_service_finalization.py
```

Never use attribution as correctness state and never optimize merely because one broad timing looks large. Respect each metric boundary before inferring causality.

## Source ownership / preview

```text
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_preview.py
fight/pipeline_mp/camera_lifecycle.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/common_preview.py
Fight_backend_project/backend_frontend_project/speed_detection/views.py
```

## Fair scheduling / backpressure

```text
fight/pipeline_mp/scheduling.py
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/speed_worker.py
tests/test_capacity_scheduling.py
```

## Incident durability / dispatcher

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/speed_worker.py
incidents/services/ingest.py
incidents/services/retention.py
incidents/models.py
```

## Location / authorization

```text
adminx/models.py
streams/models.py
services/access_scope.py
incidents/models.py
```

## Operational durability / retention

```text
fight/operations.py
fight/retention.py
fight/service_loop.py
fight/runtime_supervisor/core.py
incidents/services/retention.py
```

---

# 40. Cross-phase acceptance checklist

Before accepting architecture-affecting work verify, as applicable:

```text
[ ] ARCHITECTURE.md was read first and coding agent did not edit it
[ ] current Supervisor-managed ownership path was inspected, not inferred from legacy helpers
[ ] one Runtime Supervisor still owns the global AI runtime
[ ] one CameraIngest remains the intended source/decode owner per physical camera
[ ] Fight+Speed on one source still use one camera entry/fan-out
[ ] no runtime Django ORM dependency or model-per-camera regression was introduced
[ ] camera-local temporal/calibration state stayed local
[ ] shared services start only when desired capabilities require them
[ ] capability transitions avoid unnecessary global runtime restart
[ ] optional disabled services remain healthy/service_disabled
[ ] stable slot/generation fencing is preserved
[ ] Speed consumer epoch and shared-service epoch fencing are preserved
[ ] Fight publication floor still blocks old-incarnation durable incidents
[ ] Vehicle recovery remains isolated from Fight where safe
[ ] Fight recovery leaves Speed-only cameras intact
[ ] unsafe teardown prefers fail-closed over duplicate source/transport ownership
[ ] authoritative non-looping file EOF remains generation-local correctness state
[ ] CameraIngest publishes authoritative EOF before consumer EOF signals
[ ] clean Fight/Speed file consumer exit requires authoritative EOF + exitcode 0
[ ] clean EOF does not restart, advance generation, reopen or replay the source
[ ] mixed Fight+Speed file completion waits for all required consumers
[ ] pre-EOF/non-zero file failures cannot later be relabeled clean
[ ] live freshness remains distinct from ordered-file completeness
[ ] fair per-slot scheduling/capacity remains bounded
[ ] no correctness dependency on OS qsize()/empty()
[ ] health state remains bounded and epoch-aware
[ ] shared-worker process death/inference-stall classification is not weakened
[ ] health snapshot retry is limited to documented transient Windows replacement errors
[ ] persistent/unrelated snapshot filesystem failures still surface
[ ] snapshot publication failure remains observational/non-fatal, not hidden
[ ] service failure status retains the pre-teardown cause/identity where available
[ ] forced teardown exit codes are not misreported as original stall causes
[ ] normal shared-service withdrawal has bounded graceful finalization
[ ] failure/recovery withdrawal does not reuse graceful drain to preserve poisoned transport
[ ] final worker summaries are produced before Reporter termination on normal close
[ ] Reporter final flush precedes final performance-summary construction
[ ] worker timing/batch populations remain distinct from client distributions
[ ] attribution remains bounded/nonblocking/best-effort and never becomes correctness state
[ ] incompatible percentile populations are not merged/averaged as pooled percentiles
[ ] successful-result/completed counters preserve documented semantics
[ ] missing/no-sample metrics remain null/unavailable, not fabricated zero
[ ] Incident/Reporter runtime-global failure semantics remain explicit
[ ] durable outbox remains runtime->Django truth boundary
[ ] Speed uses Incident(type=SPEED) in common incident domain
[ ] location/security authorization remains application-layer
[ ] retention/disk-pressure protections remain fail-safe/bounded
[ ] Windows spawn compatibility is tested for multiprocessing changes
[ ] Person batching remains OFF by default unless explicitly promoted after target validation
[ ] benchmark and production result semantics are not mixed
[ ] synthetic counts are not called real capacity
[ ] one GPU's measurements are not extrapolated into another GPU's camera count
[ ] benchmark code does not tune production behavior
[ ] generated runtime/benchmark/review artifacts are not staged
[ ] UI/PostgreSQL/Docker/Nginx/deployment remain frozen unless explicitly promoted
[ ] shared-memory transport is introduced only after controlled attribution justifies it
```

---

# 41. Deferred / frozen work

Backend/runtime work worth promoting deliberately:

- target-hardware RTX 5090 characterization using the actual machine rather than extrapolation;
- sustained live/RTSP tests with representative resolution/FPS/network behavior;
- long soak tests covering camera churn, capability changes, reconnect, recovery and storage growth;
- mixed Fight+Speed workloads that actually exercise Pose and Stage3, not only traffic content;
- repeated OFF/Batch-2 experiments on target workloads if batching is reconsidered for production defaults;
- controlled Vehicle service experiments only if the measured 8→12 queue/throughput knee remains important on target hardware;
- shared-memory transport only if a controlled experiment isolates transport overhead as material;
- model-worker concurrency/partitioning only after service-time/queue evidence justifies it;
- Speed CPU-side optimization only if preprocess/tracking/decision/evidence attribution supports it;
- deliberate spawned-worker chaos/failure tests and longer recovery sequences;
- mixed-camera Fight recovery refinement to reduce brief Speed interruption without weakening fencing;
- representative Fight decision-quality and Speed calibration/accuracy validation;
- realistic duplicate-incident/temporal validation;
- longer-horizon observability/storage sizing/operator tooling;
- multi-GPU partitioning only after single-node bottlenecks are measured on production-class hardware.

Explicitly frozen unless separately promoted:

```text
PostgreSQL migration
Docker redesign
Nginx/media offload
production deployment/service packaging
frontend/dashboard redesign
incident UX redesign
preview/offline UX redesign
```

---

# 42. Capacity qualification strategy after Phase 19.1

The broad RTX 3050 scale sweep and Phase-18 attribution reruns have already answered the first-order questions:

```text
Fight and Speed throughput both show diminishing returns at larger camera counts.
Speed 8 -> 12 is not explained by obvious raw GPU saturation alone.
Mixed workload creates cross-service contention.
Person Batch-2 can reduce Person queue pressure on the tested Mixed-8 traffic workload.
Batch-4 can over-optimize Person while worsening Vehicle contention.
```

Therefore the next question is **not** “how many more logical cameras can this laptop survive?” and not “should the current laptop curve be multiplied for RTX 5090?”.

Future qualification should preserve comparable config/media/hardware/power conditions when comparing implementation changes, and should define an acceptance criterion before increasing scale.

On production-target hardware, evaluate at minimum:

```text
exact source completion / live continuity
aggregate and per-camera service rate
CameraIngest read/fanout/per-branch publication
Person/Pose/Stage3 queue + inference + result enqueue
Vehicle admission-inclusive wait + inference + result enqueue
Fight/Speed camera-local processing boundaries
CPU/RAM + GPU utilization/VRAM
queue high-water / rejection / file defer / live shedding
runtime health / service restart / camera generation / consumer epoch
incident durability + evidence latency where measurable
Reporter/health-snapshot reliability over long duration
```

For live/RTSP qualification, additionally include realistic camera FPS, resolution, network jitter/loss, reconnect behavior and wall-clock latency. Ordered-file full-run FPS alone is not a live service-level metric.

Optimization decision rule:

```text
If shared model service time/queueing dominates:
    evaluate batching, worker concurrency or model partitioning.

If camera-local CPU work dominates:
    optimize that subsystem first.

If transport-related evidence remains dominant after controlling backlog/fanout:
    run a shared-memory transport experiment before adopting it.

If one service harms another in mixed load:
    optimize total-system fairness/throughput, not one queue in isolation.

If target GPU behavior differs materially from RTX 3050:
    prefer target measurements over laptop tuning conclusions.
```

No camera-count claim belongs in this contract without a clearly described real workload, hardware, configuration, duration and acceptance criterion.
