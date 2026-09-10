# Fight-Detection Architecture Contract

## Document ownership and maintenance discipline

This file is the architecture contract for the repository. It describes the **current Supervisor-managed production architecture**, the guarantees inherited from earlier phases, the failure/durability boundaries, the measurement model, and intentionally deferred work.

**Maintenance rule:** coding agents (including Codex) must read this file before architecture-affecting work and **must not modify it**. The project owner and ChatGPT maintain it from committed repository state.

Architecture refreshes are never a narrow “append the latest commit” exercise. Before changing this file, the maintainer must read the whole current document, inspect the current `master` implementation of affected ownership paths, compare the new code commit with the previous architecture baseline, review the earlier phase guarantees it depends on, inspect focused tests as executable contracts, remove stale/contradictory statements, verify failure/health/durability/task-router/deferred-work sections remain consistent, and distinguish measured facts from estimates or future production assumptions.

Current production-code reference commit:

```text
3b9733f20a3ca4d3773c63fed0caba7939f2f27c
fix: harden ordered file EOF lifecycle
```

**Phase 17 is committed on `master` and is the current production architecture baseline.** Phase 17 changes ordered-file completion/lifecycle correctness only; it does not change model thresholds, shared-service topology, queue capacities, UI, Django models, or deployment architecture.

---

# 1. System purpose and direction

The repository is a centralized multi-camera security platform in which Fight Detection and Speed Detection share runtime infrastructure while preserving camera-local temporal state.

```text
Django / application control plane
    -> Runtime Supervisor
        -> one global run_multiprocess parent
            -> one CameraIngest source/decode owner per physical camera
            -> capability-managed shared inference services
            -> camera-local Fight/Speed temporal consumers
            -> runtime-global incident production
    -> durable incident outbox
    -> Django Incident Dispatcher
    -> common Incident / routing / authorization domain
```

The production design is deliberately not “one complete AI pipeline per camera”. Expensive/stateless inference is shared; camera-specific temporal interpretation remains local.

Target scale is large multi-camera deployment, but **no production camera count is asserted by this contract**. Phase-16 benchmarks characterize the current implementation; actual capacity must be measured on the intended hardware and workload.

---

# 2. Non-negotiable invariants

1. Runtime workers must not import or depend on Django ORM.
2. Django/Gunicorn is the control/application plane, not AI child-process owner.
3. Runtime Supervisor owns the global production AI runtime lifecycle.
4. `run_multiprocess` owns the multiprocessing topology below the Supervisor.
5. One physical camera has one intended `CameraIngest` source/decode owner in the Supervisor-managed production runtime.
6. Fight and Speed on one physical camera share that ingest and one desired camera entry.
7. Expensive/stateless inference models are shared services, not model-per-camera instances.
8. Fight temporal tracking/pair/ROI/event state and Speed tracking/calibration/speed state remain camera-local where history matters.
9. Shared inference services are capability-aware and run only while the desired camera set requires them.
10. Desired capability changes do not require a global runtime restart solely because requirements changed.
11. Camera work is fenced by stable slot + generation. Speed adds consumer epoch. Recoverable shared services add service epoch where required.
12. Old/stale work fails closed and cannot become a current incident after camera/service reconfiguration.
13. Live and file workloads intentionally use different backpressure/recovery semantics.
14. Correctness must not depend on OS `Queue.qsize()` or `Queue.empty()` observations. Parent-owned scheduler counters/acks may be correctness inputs when their semantics are controlled.
15. Queueing, telemetry, retry and operational scans remain bounded.
16. Windows `spawn` compatibility is a first-class constraint.
17. Runtime incident truth crosses into Django through the durable incident outbox; runtime workers do not create Incident ORM rows.
18. Fight and Speed share the same Django Incident/routing domain.
19. Optional service absence is healthy when the service is not required.
20. Synthetic camera-equivalents are not production inference capacity; measurements from one GPU are not linearly extrapolated to another.
21. Benchmark code must not mutate production thresholds, source ownership, queue semantics or recovery behavior merely to improve results.
22. PostgreSQL, Docker, Nginx, deployment/service packaging and UI redesign are frozen/deferred unless explicitly promoted.
23. Shared-memory frame transport remains deferred until measurement shows multiprocessing frame transport is a meaningful bottleneck.
24. **Ordered non-looping file EOF is correctness state, not telemetry.** The authoritative EOF fact is a generation-local multiprocessing event owned by the current camera runtime and published by `CameraIngest` before any EOF consumer signal.
25. A dead required file consumer is a clean drain only when authoritative EOF has been reached and the process exited cleanly. Pre-EOF or non-zero exits remain failure/fail-closed behavior.
26. Clean EOF must not increment camera generation, reopen the source, replay the file, or synthesize watchdog recovery.

---

# 3. Current top-level production flow

```text
Django / Web / DB
  |
  | desired camera state + control requests
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
CameraIngest -> camera_worker -> shared Person
 -> camera-local person/pair/ROI/temporal state
 -> shared Pose when configured/required
 -> shared Stage3/X3D
 -> Incident worker / IncidentAggregator
 -> evidence + durable outbox
 -> Django Incident Dispatcher
 -> Incident(type=FIGHT) -> routing / ACK / escalation / resolve
```

Speed path:

```text
CameraIngest -> speed_worker
 -> camera-local motion/tracking/calibration
 -> shared Vehicle inference
 -> camera-local speed/violation decision
 -> evidence + durable outbox
 -> Django Incident Dispatcher
 -> Incident(type=SPEED) -> routing / ACK / escalation / resolve
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

Django may observe runtime state and consume runtime-produced preview/evidence. It must not become a hidden second AI runtime/source owner.

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

The dynamic path owns spawn context, slot-generation and Speed-epoch arrays, Fight publication floor, Incident/Reporter workers, `SharedServices`, `CameraRuntimeManager`, desired-state polling/reconcile, health/watchdog/snapshot, fair admissions, file EOF/drain/finalization and global exit semantics.

---

# 5. Desired camera state

Primary files:

```text
fight/runtime_supervisor/camera_state.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
```

Schema version is currently `1`. Canonical camera fields:

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
enabled == true AND (use_fight_detection == true OR use_speed_detection == true)
```

Supported modes are Fight-only, Speed-only, Fight+Speed, or neither/not-started. Desired state has a monotonic revision; stale/equal revisions must not create duplicate lifecycle work. `speed_paused` is durable desired intent and stopping Speed is not equivalent to stopping Fight/common runtime.

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

`camera_worker` and `speed_worker` must not independently reopen the centralized production source. Live reconnect ownership belongs to `CameraIngest`.

File/live publishing intentionally differs:

- file Fight/Speed delivery is ordered/bounded and waits/defer rather than silently discarding required work,
- live freshness may supersede stale work,
- preview is a bounded latest-view concern,
- live reconnect is bounded/exponential and ingest-owned.

## 6.1 Authoritative ordered-file EOF publication — Phase 17

For a non-looping local file, each `CameraRuntime` owns a fresh `multiprocessing.Event` (`file_eof_event`). `CameraIngest` sets that event **before** sending `CameraIngestSignal(detail="eof")` to Fight, Speed or Preview consumers.

This ordering is a correctness invariant:

```text
read reaches legitimate non-looping file EOF
 -> set generation-local file_eof_event
 -> deliver EOF signal(s)
 -> consumers may exit
 -> manager/watchdog may classify clean drain
```

The bounded `HealthEvent` channel still emits EOF telemetry, but health telemetry is best-effort and **does not own EOF correctness**.

The event belongs to the `CameraRuntime` incarnation. A genuine restart gets a new runtime/event; a late old-generation event cannot authorize completion of the replacement generation.

## 6.2 Browser preview ownership

Common-runtime preview is consumed from atomically replaced JPEG output. Supervisor-mode views do not open `camera.source` with a second `cv2.VideoCapture`. Direct-source calibration/legacy fallback is acceptable only in an explicitly non-Supervisor stopped legacy path.

---

# 7. Dynamic camera lifecycle

Primary implementation:

```text
fight/pipeline_mp/camera_lifecycle.py::CameraRuntimeManager
```

Per-camera runtime state includes desired camera definition, stable slot/generation, stop controls, Fight/Preview/Speed channels, generation-local file EOF event, Speed epoch/failure/recovery state, Fight-service waiting state, lifecycle/file completion and restart counters.

Normal composition:

```text
Fight-only:   CameraIngest + camera_worker + preview
Speed-only:   CameraIngest + speed_worker + preview
Fight+Speed:  CameraIngest + camera_worker + speed_worker + preview
```

Source/capability/serialized Speed-config changes may require a camera-local restart; cosmetic metadata should not.

Camera removal invalidates generation before teardown. Re-add/restart advances generation. Shared-service recovery can temporarily withdraw only affected consumers where architecture permits.

## 7.1 Ordered-file completion rule — Phase 17

`CameraRuntimeManager.file_eof_reached(item)` is true only for a local file, `loop_file_sources == false`, and the current runtime's authoritative EOF event being set.

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

The whole camera becomes terminal `file_done/STOPPED/file_eof` only after ingest has cleanly exited after authoritative EOF and every required Fight/Speed consumer has completed. A mixed Fight+Speed camera therefore cannot become cleanly complete merely because Fight finished.

A Speed worker that dies before authoritative EOF — even with exit code `0` — is not clean completion. Existing `speed_failed` semantics are latched, the Speed epoch is invalidated, the file is not replayed, and later ingest EOF cannot rewrite that run as clean success. Terminal `file_done` may still be reached for accounting, while dynamic runtime finalization preserves failed-run status (existing Speed file failure exit semantics).

A Fight worker dead before authoritative EOF, a non-zero Fight exit, or an abnormal ingest exit remains failure/restart/fail-closed behavior according to existing live/file policies.

Clean EOF itself never increments generation or reopens the source.

## 7.2 Shared-service recovery locality

- Vehicle recovery withdraws affected Speed consumers while Fight/ingest/preview can remain intact where safe.
- Fight-bundle recovery withdraws affected Fight camera runtimes before transport replacement. A mixed Fight+Speed camera currently incurs a camera-local restart and brief Speed interruption; Speed-only cameras remain untouched.

That mixed-camera interruption is an optimization opportunity, not a correctness failure.

---

# 8. Identity and stale-work fencing

Base camera identity:

```text
(camera_id, slot_id, generation)
```

Slots are parent-owned reservations. Restart/removal invalidates old generation. Delayed Person/Pose/etc. results and stale camera health events are rejected.

Speed adds `consumer_epoch`; stopping/replacing a Speed consumer increments the slot epoch. Vehicle work and durable Speed publication carry generation + consumer epoch.

Recoverable shared services add `service_epoch`; stale health/results from prior incarnations cannot overwrite current state. Vehicle recovery replaces request/result transport. Fight recovery replaces the Fight transport graph.

Fight additionally uses `fight_publication_floor`. Stage3 results carry service epoch into Incident/Aggregator. The parent raises the publication floor before failed Fight transport is touched; old-incarnation segments/results cannot later durably publish as current incidents.

The Phase-17 file EOF event is also incarnation-local: it is created with the camera runtime and is not reused across genuine restart generations.

---

# 9. Shared inference services and capability lifecycle

Primary implementation:

```text
fight/pipeline_mp/shared_services.py::SharedServices
```

Required capabilities:

```text
fight   = any enabled desired camera with use_fight_detection
vehicle = any enabled desired camera with use_speed_detection
```

Fight bundle:

```text
person
person_router
pose + pose_router   if runtime.use_pose
stage3               if runtime.use_stage3
```

Vehicle bundle:

```text
vehicle
```

Runtime-global Incident and Reporter sit outside these bundles. First demand hot-starts the required bundle; last demand removal can stop it after bounded idle grace without restarting unrelated capabilities. Current default shared-service idle grace is 5 seconds. Configured-off Pose/Stage3 is expected absence, not health failure.

---

# 10. Fight inference ownership

Person:

```text
camera_worker[N] -> fair per-slot Person admission
 -> one shared Person model/worker -> result router -> per-slot result channel
```

Pose:

```text
camera_worker[N] -> selected ROI -> fair Pose admission
 -> one shared Pose model/worker -> router -> camera-local PoseGate/history
```

Stage3:

```text
camera_worker[N] -> bounded Stage3 admission
 -> one shared Stage3/X3D worker -> Incident worker -> IncidentAggregator
```

IncidentAggregator must not instantiate a second local Person/Pose model. Shared Stage3 output is service-incarnation-tagged for Fight publication fencing.

---

# 11. Speed inference ownership

Primary runtime worker:

```text
fight/pipeline_mp/speed_worker.py
```

`speed_worker` owns camera-local ROI/calibration, motion gate, tracker, speed estimator, violation decision/cooldown and evidence buffer/writer. It does **not** own a YOLO Vehicle model.

```text
speed_worker[N]
 -> fair/bounded per-slot Vehicle admission
 -> shared vehicle_service_main
 -> one lazily created VehicleDetector/model
 -> per-slot VehicleResult
 -> speed_worker[N]
```

Vehicle result payloads do not send full frame pixels back. Vehicle inference/model errors are service failures, not “no detections”. Normal Django Speed control uses the common Runtime Supervisor; older standalone Speed helpers are compatibility/history, not production ownership.

---

# 12. Shared-service recovery and failure isolation

## 12.1 Vehicle

Confirmed Vehicle failure:

```text
mark unavailable
 -> withdraw affected Speed consumers
 -> invalidate Speed consumer epochs
 -> close failed Vehicle transport
 -> replace request/result transport
 -> increment Vehicle service epoch
 -> bounded exponential-backoff replacement
 -> resume eligible LIVE Speed consumers
```

Defaults:

```text
VEHICLE_SERVICE_RESTART_LIMIT=3
VEHICLE_SERVICE_RESTART_BACKOFF_SEC=2
VEHICLE_SERVICE_RESTART_MAX_BACKOFF_SEC=30
```

Vehicle exhaustion remains isolated from healthy Fight where possible. File Speed is never replayed as clean work after partial failure.

## 12.2 Fight bundle

Recoverable components:

```text
person person_router pose pose_router stage3
```

Confirmed failure recovers the whole Fight inference bundle because transport/result ownership forms one incarnation boundary:

```text
raise fight_publication_floor
 -> invalidate affected generations
 -> mark Fight cameras waiting
 -> withdraw affected readers/runtimes
 -> close old Fight transport
 -> bounded backoff
 -> fresh Fight bundle + service_epoch
 -> resume eligible LIVE affected cameras
```

Defaults:

```text
FIGHT_SERVICE_RESTART_LIMIT=3
FIGHT_SERVICE_RESTART_BACKOFF_SEC=2
FIGHT_SERVICE_RESTART_MAX_BACKOFF_SEC=30
```

Fight file touched by shared Fight failure is incomplete/no-replay. Unsafe withdrawal, retry exhaustion, initial Fight startup failure and runtime-global Incident/Reporter failure remain fail-closed/fatal boundaries.

## 12.3 Runtime-global failures

Incident and Reporter are runtime-global. Their confirmed death remains a global runtime failure/stop boundary rather than being reconstructed as optional Fight services.

---

# 13. Django Speed bridge

Primary facade:

```text
Fight_backend_project/backend_frontend_project/services/speed_bridge/speed_runner.py
```

```text
start_speed_pipeline
 -> clear speed_paused
 -> reconcile desired cameras
 -> start/reuse common Supervisor runtime

stop_speed_pipeline
 -> persist speed_paused=true
 -> remove Speed demand
 -> Fight remains independently available
```

`CommonRuntimeProcess` is a read-only compatibility facade and must not become a second process owner.

---

# 14. Fair scheduling and bounded capacity

Primary implementation:

```text
fight/pipeline_mp/scheduling.py::FairRequestQueue
```

Shared fair stages are Person, Pose, Stage3 and Vehicle. Per-slot bounded pending work plus round-robin dispatch prevents one hot camera from monopolizing a shared FIFO.

Capacity/accounting includes capacity, pending-per-camera, outstanding, accepted, rejected-capacity, deferred-file, dropped-live, stale-generation, dispatches, high-water and per-slot counters.

Live may shed stale work explicitly; ordered files wait/defer. Dropped/stale work must not be converted into a synthetic negative detection. OS queue `qsize()/empty()` are telemetry/convenience only unless a custom parent-owned structure explicitly defines correctness semantics.

---

# 15. Live vs ordered-file semantics

## LIVE / RTSP

Priority:

```text
freshness > completeness
```

Expected: bounded queues, stale live frame/inference shedding, ingest-owned reconnect, bounded camera-local retries, eligible live Speed resume after Vehicle recovery, eligible live Fight resume after Fight recovery, unrelated services surviving local/recoverable faults.

## FILE

Priority:

```text
correctness + ordering > freshness
```

Expected:

- ordered work waits/defer instead of silently dropping required inference,
- admitted Stage3 drains before final completion,
- non-looping EOF is authoritative only through the generation-local EOF event,
- EOF marker is set before consumer EOF delivery,
- required consumer exit is clean only with authoritative EOF + `exitcode == 0`,
- clean EOF never causes watchdog restart, generation advance, source reopen or replay,
- mixed Fight+Speed waits for all required consumers,
- pre-EOF/non-zero Speed failure remains latched/fail-closed and is not later rewritten as clean success,
- Fight file affected by shared Fight failure remains incomplete/no-replay,
- benchmark deadline truncation is `INCOMPLETE`, never successful throughput.

---

# 16. Health architecture

Primary files:

```text
fight/pipeline_mp/health.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/core.py
```

```text
child workers -> bounded HealthEvent queue -> runtime parent
 -> HealthRegistry -> RuntimeWatchdog -> atomic runtime_health.json
 -> Supervisor health projection
```

Health is bounded and uses monotonic time where appropriate. Camera components include ingest, Fight worker, preview and Speed worker; shared records include Person/router, Pose/router, Stage3, Incident and Vehicle. Reporter remains runtime-global process-liveness critical even though it need not appear as a normal inference health record.

Capability metadata includes `required`, `service_state`, `service_epoch`, `restart_count`; optional disabled services are healthy `service_disabled`.

## 16.1 Phase-17 EOF/exit classification

Health snapshots now carry authoritative `file_eof` state projected from `CameraRuntimeManager`. `RuntimeWatchdog` observes per-camera process liveness **and process exit codes**, then reads manager status/EOF state before evaluation.

A dead Fight consumer is exempt from `FAILED/process_dead` only for authoritative file EOF with clean exit code `0`. Missing/unknown/non-zero exit status is failure. A legitimately drained file consumer can remain `ONLINE/file_draining` until manager convergence without a heartbeat and without watchdog restart.

This is intentionally separate from best-effort EOF health events. Watchdog protection is not globally weakened and timeouts were not increased to hide the race.

Speed failure state is still evaluated separately; `speed_failed` degrades/fails the Speed branch as before.

---

# 17. Incident durability boundary

Primary files:

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/speed_worker.py
```

Runtime produces evidence and durable incident envelopes before Django ingestion. Core semantics: append-only JSONL, serialized writers, flush/fsync before success publication, partial-tail preservation, persistence failure surfacing, evidence durability before incident publication, and stale generation/epoch guards.

Fight adds service-incarnation publication-floor fencing. Speed durable publication uses generation + consumer epoch. Runtime never directly creates Incident ORM rows.

---

# 18. Django incident/application domain

Primary models include `Incident`, routing rules/routes, audit events, ingest cursor and ingest records. Types include `FIGHT`, `SPEED`, `OTHER`.

```text
durable runtime outbox
 -> run_incident_dispatcher
 -> incidents.services.ingest
 -> Incident ORM row
 -> location/security routing
 -> ACK / resolve / escalation / audit
```

Dispatcher is an application service independent of global AI runtime ownership. Evidence deletion must respect Incident references/retention protections.

---

# 19. Location and authorization

Primary application entities:

```text
adminx.Location
adminx.SecurityUnit
adminx.SecurityUnitCoverage
adminx.UserSecurityAssignment
streams.Camera
```

```text
User -> SecurityUnit assignment -> Coverage -> Location hierarchy
 -> Camera.location -> incident/preview/action visibility
```

Fight and Speed share one physical authorization/location domain. Legacy `Camera.faculty` may remain for compatibility; organizational access decisions belong in Django, not model workers.

---

# 20. Runtime durability, retention and operational state

Phase-12 durability remains binding. Supervisor/desired state is durable/atomic where designed, outbox/evidence durability is explicit, cleanup scans are bounded, active/unknown/abnormal runs fail closed against unsafe cleanup, Incident-referenced evidence is protected, evidence retention is indefinite by default unless explicitly configured, disk pressure is observable without restart storms, and long-running operational jobs use singleton service loops/locks.

Generated runtime/benchmark content is not source code. Known local operational paths include:

```text
.runtime_supervisor/
Fight_backend_project/backend_frontend_project/media/camera_uploads/
Fight_backend_project/backend_frontend_project/media/pipeline_runs/.run_locks/
Fight_backend_project/backend_frontend_project/media/runtime_spool/
benchmarks/results/
benchmarks/.capacity.lock
```

---

# 21. Phase-16 capacity benchmark subsystem

Phase 16 introduced an **offline measurement subsystem**, not another production runtime.

Primary files:

```text
benchmarks/__main__.py
benchmarks/control_plane.py
benchmarks/real_inference.py
benchmarks/telemetry.py
benchmarks/README.md
tests/test_capacity_benchmarks.py
```

## 21.1 Measurement taxonomy

```text
REAL INFERENCE
 = real Supervisor/runtime + local ordered-file decode
 + real models/workers + real queue/recovery/health behavior

CONTROL PLANE / SYNTHETIC
 = real parent-side lifecycle/state/scheduling structures
 + inert/fake execution
 - no model inference
 - no real decode
```

Synthetic camera-equivalents must never be called real inference capacity.

## 21.2 Real-mode isolation

Real mode uses the common `RuntimeSupervisor`/`run_multiprocess`, preserves model/detection/queue/recovery semantics, uses isolated result/Supervisor/outbox paths, `auto_restart=False`, refuses concurrent production runtime or duplicate benchmark harness, rejects remote/credential-bearing sources, and does not auto-download missing weights.

Logical benchmark cameras reusing one source receive distinct hardlinks/copies and identities to satisfy source uniqueness. This is same-content ordered-file stress, not physical RTSP equivalence.

## 21.3 Synthetic scope

Control-plane mode exercises desired-state validation, `CameraRuntimeManager`, slot/generation handling, HealthRegistry/snapshot serialization and all four fair admissions with inert processes. It checks lifecycle churn, generations, stale fencing, capability scope, idempotence, bounded telemetry, round-robin fairness and capacity accounting.

Synthetic timings exclude process spawn, CUDA/model inference, image decode, RTSP pacing/loss and multiprocessing frame-copy cost.

## 21.4 Telemetry/output

Real mode can collect host CPU, process-tree RSS, harness RSS, host RAM, optional NVIDIA utilization/VRAM, camera progress/health, shared-stage capacity and existing bounded latency summaries. `nvidia-smi` failures become unavailable data; GPU utilization alone never proves the bottleneck.

Result roots contain `benchmark_summary.json`, `runs.jsonl`, `system_samples.csv` plus isolated real-runtime artifacts. Unavailable metrics remain null/explicitly unavailable; zero samples must not be interpreted as zero latency.

## 21.5 Classification

```text
HEALTHY
PRESSURED
SATURATED
INCOMPLETE
```

`INCOMPLETE` covers deadline/non-zero exit/missing required reports or samples/failed required consumer/observed recovery/failed runtime health/no usable frame summary. `SATURATED`/`PRESSURED` use observed live shedding/rejection/queue criteria; GPU utilization alone cannot set saturation. Ordered-file defer/retry is not live drop.

---

# 22. Measured capacity/benchmark evidence

All measurements here are workload/hardware-specific observations, not production promises.

## 22.1 Synthetic acceptance

Phase-16 mixed control-plane scenarios at 50/100/200/300 camera-equivalents passed slot/generation/bounded-storage/scheduler-fairness checks.

Representative values:

```text
cameras   initial reconcile   health/snapshot p95   approx RSS delta
50        6.31 ms             3.21 ms               3.37 MiB
100       20.71 ms            6.04 ms               4.06 MiB
200       15.06 ms            11.13 ms              7.90 MiB
300       20.35 ms            8.23 ms               12.05 MiB
```

This proves no immediate 300-entry wall in those synthetic parent-side structures only.

## 22.2 Initial RTX 3050 real Fight harness acceptance

Development GPU: NVIDIA GeForce RTX 3050 6GB Laptop GPU. A one-camera ordered-file smoke completed HEALTHY with 903 decoded frames and ~14.76 aggregate full-run effective FPS. Person/Pose/Stage3 completed 417/311/6 accepted requests/jobs respectively, with no admission drop/rejection and queue-ratio peak 0.03125.

Representative observations from that smoke:

```text
CPU mean ~12.84%, p95 ~19.75%
GPU mean ~13.02%, p95 ~42.5%, max ~67%
VRAM p95/max ~640 MiB
runtime process-tree RSS max ~4.98 GB (decimal reporting in that stored summary)
Person queue p95 ~1.52 ms; round-trip p95 ~28.80 ms
Pose queue p95 ~1.29 ms; round-trip p95 ~33.80 ms
Stage3 queue p95 ~160.27 ms; inference p95 ~624.04 ms (small sample)
```

A second two-camera smoke also completed HEALTHY but its detailed metrics were not promoted at that time.

## 22.3 Later charged-system Fight scaling continuation

Using the same selected Fight effective configuration and `fight/sample_2.mp4`, later runs performed with the laptop connected to power produced:

```text
cameras   aggregate full-run FPS   per-camera effective FPS   classification
2         28.67                    14.33                      HEALTHY
4         49.89                    12.47                      HEALTHY
8         65.55                     8.19                      HEALTHY
```

Selected pressure indicators:

```text
                    2 cam      4 cam       8 cam
Person queue p95     7.78 ms    35.52 ms   123.99 ms
Person RT p95       26.10 ms    57.58 ms   146.55 ms
Pose queue p95       6.39 ms    32.21 ms   100.24 ms
Pose RT p95         26.50 ms    55.19 ms   123.92 ms
Stage3 queue p95   494.03 ms  1120.90 ms  1329.48 ms
GPU mean            18.26%      40.40%      49.47%
GPU p95             56.4%       84.0%       85.0%
GPU max             75.0%       88.0%       94.0%
CPU mean            26.80%      24.92%      30.60%
VRAM max           640 MiB     640 MiB     640 MiB
runtime RSS max      5.28 GiB    6.66 GiB    9.30 GiB
queue ratio peak     0.0625      0.125       0.25
```

All three runs had zero admission-drop ratio, zero rejection-attempt ratio and no observed recovery. Throughput scaled sublinearly and Person/Pose queue wait increased sharply by 8 cameras, while neither CPU mean nor VRAM indicated the first hard limit. This is evidence of growing shared-inference/service demand, **not yet proof of one specific bottleneck such as GPU compute or IPC**.

The full-run FPS includes startup/drain/EOF behavior and is not steady-state live RTSP FPS.

## 22.4 Pre-Phase-17 12-camera diagnostic run

A 12-camera / 180-second Fight run was `INCOMPLETE` and must **not** be used as a capacity point. It accepted the expected aggregate Person/Pose/Stage3 work counts (5004/3732/72), had zero admission-drop/rejection ratios and queue-ratio peak 0.34375, but final camera performance reporting was incomplete and `RecoveryObserved=True`.

Diagnosis showed:

```text
shared Fight worker restarts: none
camera-local restarts: bench-0000 and bench-0001, each restart_count=1
health transition before restart: ONLINE/file_draining -> FAILED/process_dead
watchdog action: camera_watchdog_restart_requested reason=process_dead
generation: 1 -> 2
then file replayed and later reached EOF
```

This exposed the manager/watchdog ordered-file EOF race fixed by Phase 17. Therefore the run is a **correctness diagnostic artifact, not saturation evidence**. A same-config/same-source/same-180-second 12-camera rerun is required after Phase 17 before interpreting 12-camera throughput.

## 22.5 OpenMP environment observation

An earlier first real smoke failed with Intel OpenMP Error #15 (`libiomp5md.dll already initialized`) and was correctly `INCOMPLETE`. Two filesystem locations were observed for `libiomp5md.dll` with identical hashes; normal imports/CUDA allocation worked. Clean activation plus `conda run -n torch_gpu --no-capture-output` later produced healthy runs.

**The exact root cause is not proven.** Do not set `KMP_DUPLICATE_LIB_OK=TRUE`, delete DLLs or modify conda packages based on this evidence alone.

## 22.6 What current measurements do not prove

They do not prove RTX 5090 capacity, 200–300 real RTSP cameras, sustained live steady-state FPS, campus network/jitter behavior, mixed Fight+Speed saturation, whether frame-copy IPC is the next bottleneck, or multi-GPU scaling.

---

# 23. Historical performance context

Older pre-later-phase RTX 3050 dense-file measurements were approximately:

```text
1 cam ~21.29 FPS
2 cam ~41.56 FPS
4 cam ~59.58 FPS
8 cam ~68.73 FPS
```

They are historical context only. Runtime architecture, workload and instrumentation changed; they must not be compared blindly with current Phase-16/17 ordered-file runs.

---

# 24. Production runtime vs legacy paths

This contract describes the Supervisor-managed dynamic runtime. Static/legacy/direct-control helpers may remain but do not define production ownership. Existing Docker/Nginx or standalone Speed files do not authorize architecture changes in those areas.

Before inferring ownership from a helper, determine whether it is reachable from the Supervisor-managed production flow.

---

# 25. Phase 1–12 foundation ledger

- **Phase 1 — Shared Person:** one shared Person model/worker; camera-local Motion/stabilizer/tracking/pair/ROI/event/prebuffer; explicit request identity.
- **Phase 2 — Shared Pose:** one shared Pose service/router; camera-local Pose temporal interpretation; later removed accidental duplicate incident-side model ownership.
- **Phase 3 — Performance observability:** bounded timing/queue/inference/delivery metrics and machine-readable summaries.
- **Phase 4 — Microbatching capability:** latency-bounded FIFO batching support while conservative defaults keep batch size effectively 1 unless configured.
- **Phase 5 — Centralized CameraIngest:** one source/decode owner feeding Fight/Preview, later Speed; ordered-file vs live freshness policy.
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

# 26. Phase 13 — Shared Speed integration

Code baseline:

```text
418f65bf137cfb31aa92629ac8fe1e03a0a1c54a
feat: integrate speed detection into shared runtime
```

Established Fight-only/Speed-only/Fight+Speed modes, single CameraIngest fan-out, shared Vehicle inference, camera-local Speed state, bounded Vehicle admission, Speed generation+epoch fencing, file fail-closed/live recovery distinction, common durable Speed incident path, common Supervisor control and `speed_paused` desired intent.

Validation at that phase: `135 passed, 1 skipped`.

---

# 27. Phase 14 — Capability lifecycle and Vehicle recovery

Code baseline:

```text
7b395ef94f04b435862ab013f9226b0982de34b6
feat: add capability-aware shared service lifecycle
```

Established `SharedServices`, Fight-only without Vehicle, Speed-only without Fight inference bundle, same-runtime bundle hot start/stop, bounded idle grace, Fight drain before ordinary bundle shutdown, Vehicle crash/stall/start-failure recovery, Vehicle transport replacement/service epoch, bounded retry/backoff, live Speed resume, file no-replay, optional-service health and common-preview source ownership.

Validation at that phase: `148 passed, 1 skipped`.

---

# 28. Phase 15 — Resilient Fight service recovery

Code baseline:

```text
c3c019b2871dcd3891b24ef242a2c5e93fe9212f
feat: add resilient fight service recovery
```

Established bounded in-runtime recovery for Person/routers/Pose/Stage3, whole-Fight transport replacement, generation invalidation before withdrawal, Fight service epochs, Stage3->Incident epoch propagation, publication floor, eligible LIVE resume, FILE fail-closed/no replay, bounded retry/start-failure accounting, fatal exhaustion/unsafe teardown boundaries and preservation of Vehicle/Speed-only identity.

Validation at that phase: `162 passed, 1 skipped`.

---

# 29. Phase 16 — Capacity benchmark harness

Code baseline:

```text
022d2fd5a3a6cef4ece0ac1b7434b9b9493512a0
feat: add capacity benchmark harness
```

Phase 16 added benchmark/test infrastructure only. Guarantees include real-vs-synthetic separation, real reuse of Supervisor/runtime, synthetic reuse of production parent-side structures with inert execution, bounded telemetry, optional NVIDIA fallback, redacted/isolated result outputs, benchmark singleton/concurrency protection, explicit classification semantics, no automatic stress sweep, no production threshold/queue/recovery/source-ownership mutation and no camera-count claim from the registry bound.

Implementation validation: `170 passed, 1 skipped` plus compileall, Django check, migration check and diff check.

---

# 30. Phase 17 — Ordered-file EOF/watchdog lifecycle hardening

Code baseline:

```text
3b9733f20a3ca4d3773c63fed0caba7939f2f27c
fix: harden ordered file EOF lifecycle
```

Trigger: the pre-fix 12-camera benchmark exposed a race where ingest had reached EOF and Fight consumers were legitimately draining/exiting, but manager/watchdog observations could classify the clean exit as `process_dead`, restart the camera, advance generation and replay the file.

Phase 17 established:

```text
authoritative generation-local multiprocessing Event for non-looping file EOF
CameraIngest sets EOF event before any EOF consumer delivery
health EOF telemetry remains best-effort and is not correctness state
manager/watchdog clean Fight exit requires EOF marker + exitcode 0
manager clean Speed drain requires EOF marker + exitcode 0
pre-EOF/non-zero Fight exits remain real failure/recovery behavior
pre-EOF Speed zero/non-zero exits remain failure; speed_failed latches and epoch invalidates
later EOF cannot rewrite a failed Speed file as clean success
mixed Fight+Speed file_done waits for every required consumer
clean EOF causes no generation advance, watchdog restart, source reopen or replay
new camera runtime/restart gets a fresh EOF Event; old-generation publication cannot leak
live camera recovery, Vehicle recovery, Fight shared-service recovery and service/generation fencing remain unchanged
```

Tests specifically cover spawn-safe EOF-event ordering, manager-first/watchdog-first interleavings, exit occurring during watchdog observation, clean EOF with stable generation, pre-EOF/non-zero failure preservation, live restart preservation, Speed-only and mixed Fight+Speed completion, Speed zero-exit-before-EOF failure, failure latching after later EOF, and old-generation marker isolation.

Reported validation for the committed Phase-17 worktree:

```text
focused lifecycle/health/speed coverage: 77 passed after final review fix
full pytest: 179 passed, 1 skipped
compileall: passed
git diff --check: passed
Django/models/UI: untouched
```

A post-fix 12-camera real rerun is still required to close the manual acceptance loop.

---

# 31. Task router for coding agents

Read this document first. Inspect only the current relevant ownership domain before editing.

## Supervisor / desired state

```text
fight/runtime_supervisor/core.py
fight/runtime_supervisor/camera_state.py
fight/runtime_supervisor/locking.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/fight_runner.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
```

## Dynamic lifecycle / ordered-file EOF / capability ownership

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

## Source ownership / preview

```text
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_preview.py
fight/pipeline_mp/camera_lifecycle.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/common_preview.py
Fight_backend_project/backend_frontend_project/speed_detection/views.py
```

## Health / recovery

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

## Capacity benchmark / scale characterization

```text
benchmarks/README.md
benchmarks/__main__.py
benchmarks/real_inference.py
benchmarks/control_plane.py
benchmarks/telemetry.py
tests/test_capacity_benchmarks.py

# Read with the production structures exercised:
fight/runtime_supervisor/camera_state.py
fight/runtime_supervisor/core.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/scheduling.py
fight/pipeline_mp/health.py
```

Do not modify production behavior merely because a benchmark metric is inconvenient. First decide whether the measurement is incomplete, the workload unsuitable, a correctness issue exists, or a real bottleneck is demonstrated.

---

# 32. Cross-phase acceptance checklist

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
[ ] Fight and Vehicle recovery remain bounded and use safe transport replacement
[ ] unsafe teardown prefers fail-closed over duplicate source ownership
[ ] authoritative non-looping file EOF is generation-local correctness state, not best-effort telemetry
[ ] CameraIngest publishes the authoritative EOF marker before any consumer EOF signal
[ ] clean Fight/Speed file consumer exit requires authoritative EOF + exitcode 0
[ ] clean EOF does not restart, advance generation, reopen or replay the source
[ ] mixed Fight+Speed file completion waits for all required consumers
[ ] pre-EOF/non-zero file failures cannot later be relabeled clean
[ ] live freshness remains distinct from ordered-file completeness
[ ] fair per-slot scheduling/capacity remains bounded
[ ] no correctness dependency on OS qsize()/empty()
[ ] health state remains bounded, epoch-aware and does not weaken real process-death detection
[ ] Incident/Reporter runtime-global failure semantics remain explicit
[ ] durable outbox remains runtime->Django truth boundary
[ ] Speed uses Incident(type=SPEED) in the common incident domain
[ ] location/security authorization remains application-layer
[ ] retention/disk-pressure protections remain fail-safe/bounded
[ ] Windows spawn compatibility is tested for multiprocessing changes
[ ] benchmark and production result semantics are not mixed
[ ] synthetic counts are not called real capacity
[ ] one GPU's measurements are not extrapolated into another GPU's camera count
[ ] benchmark code does not tune production behavior
[ ] unavailable metrics are not fabricated
[ ] generated runtime/benchmark/review artifacts are not staged
[ ] UI/PostgreSQL/Docker/Nginx/deployment remain frozen unless explicitly promoted
[ ] shared-memory transport is introduced only after measurement justifies it
```

---

# 33. Deferred/frozen work

Backend/runtime work worth promoting deliberately:

- post-Phase-17 12-camera rerun with identical Fight config/source/deadline,
- then comparable Speed-only and mixed real-inference characterization,
- RTX 5090 validation on actual target hardware rather than extrapolation,
- sustained live/RTSP scale tests with realistic resolution/FPS/network behavior,
- long soak tests covering camera churn, EOF/reconnect, capability changes and recovery,
- deliberate spawned-worker chaos/failure tests,
- measured CameraIngest decode and NumPy multiprocessing frame-copy/IPC analysis,
- shared-memory transport only if measurement justifies it,
- mixed Fight+Speed recovery refinement to reduce local Speed interruption during Fight recovery,
- representative Fight decision-quality and Speed calibration/accuracy validation,
- realistic incident duplicate/temporal validation,
- longer-horizon observability/storage sizing/operator tooling,
- multi-GPU partitioning only after single-node bottlenecks are measured.

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

# 34. Capacity qualification strategy

Continue evidence-driven characterization only while the machine remains safe and results remain interpretable. Comparable runs must preserve config/media/hardware/power conditions where comparison depends on them.

Record at minimum:

```text
aggregate/full-run rate
per-camera completion/progress
Person/Pose/Stage3/Vehicle admission + latency
CPU/RAM
GPU utilization/VRAM
queue occupancy/high-water/drop/rejection/defer
runtime health/recovery
generation/restart/file completion behavior
```

Immediate next acceptance point after Phase 17:

```text
RTX 3050
Fight-only
same selected effective config
same fight/sample_2.mp4
12 logical cameras
same 180-second deadline
```

The purpose is to verify the pre-fix false EOF/watchdog restart is gone before treating 12 cameras as a performance data point. Expected correctness conditions are `restart_count=0`, no generation advance caused by clean EOF, `RecoveryObserved=False`, no file replay, and clean completion if the workload finishes within the unchanged deadline. If it remains `INCOMPLETE`, diagnose the new reason rather than extending the deadline first.

On production hardware, the question is not “how many times faster is a 5090 than a 3050?” but:

```text
At what camera/workload point does measured service quality become PRESSURED,
SATURATED or INCOMPLETE, and which resource/queue/decode/IPC signal moves first?
```

If model/GPU inference saturates first, optimize scheduling/batching/model partitioning based on that evidence. If CPU/decode/IPC/serialization pressure appears first, target that subsystem. Shared memory or multi-GPU work follows measured causality, not assumption.

No camera-count claim belongs in this contract without a clearly described real workload, hardware, configuration and acceptance criterion.
