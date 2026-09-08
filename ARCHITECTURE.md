# Fight-Detection Architecture Contract

## Document ownership

This file is the architecture contract for the repository.

**Maintenance rule:** coding agents (including Codex) must **read** this file before architecture-affecting work, but must **not edit it unless the project owner explicitly asks them to**. The project owner and ChatGPT maintain this document from the committed repository state.

Current reference commit:

```text
418f65bf137cfb31aa92629ac8fe1e03a0a1c54a
feat: integrate speed detection into shared runtime
```

Phase 13 (Speed integration into the common runtime) is committed on `master`.

---

# 1. Core architecture rules

The system is split into four ownership planes:

```text
Django / Web / DB
    = control + application plane

Runtime Supervisor
    = owner of the AI runtime process

run_multiprocess
    = runtime parent/orchestrator

CameraIngest
    = the intended single physical source/decode owner per camera
```

Primary invariants:

1. Runtime workers must not import Django ORM.
2. Django/Gunicorn must not own AI child processes.
3. Runtime Supervisor owns the global AI runtime lifecycle.
4. One physical camera should have one `CameraIngest` decode owner inside the production runtime.
5. Expensive/stateless inference models are shared workers, not one model per camera.
6. Temporal/tracking/calibration state remains camera-local where correctness requires it.
7. Camera work is identified by stable slot + generation; Speed adds a consumer epoch.
8. Live and file sources intentionally use different backpressure semantics.
9. Runtime incident truth crosses into Django through the durable incident outbox.
10. `Incident(type=FIGHT)` and `Incident(type=SPEED)` use the same Django incident/routing domain.
11. Queue `qsize()` must not be required for correctness.
12. Windows `spawn` compatibility is a first-class constraint.

---

# 2. Top-level runtime flow

```text
Django
  |
  | desired camera state
  v
CameraRegistryReconciler
  |
  v
RuntimeSupervisor
  |
  | desired_cameras.json
  | run_config.supervisor-<run_id>.json
  v
fight.pipeline_mp.run_multiprocess
  |
  +--> shared Person worker
  +--> Person result router
  +--> shared Pose worker (when runtime.use_pose)
  +--> Pose result router
  +--> shared Stage3/X3D worker
  +--> shared Vehicle worker
  +--> Incident worker / IncidentAggregator
  +--> Reporter
  |
  +--> CameraRuntimeManager
         |
         +--> CameraIngest(camera N)
         |      |
         |      +--> fight_queue   (optional)
         |      +--> speed_queue   (optional)
         |      +--> preview_queue
         |
         +--> camera_worker(camera N)   (Fight enabled only)
         +--> speed_worker(camera N)    (Speed enabled only)
         +--> camera_preview(camera N)
```

Fight incident path:

```text
camera_worker
  -> Person / Pose / Stage3
  -> Incident worker
  -> IncidentAggregator
  -> evidence
  -> durable incident outbox
  -> Django Incident Dispatcher
  -> Incident(type=FIGHT)
  -> routing / ACK / escalation / resolve
```

Speed incident path:

```text
speed_worker
  -> shared Vehicle inference
  -> camera-local tracking / calibration / speed estimation
  -> violation evidence
  -> durable incident outbox
  -> Django Incident Dispatcher
  -> Incident(type=SPEED)
  -> routing / ACK / escalation / resolve
```

No second incident database or dispatcher exists for Speed.

---

# 3. Process ownership

## Django owns

- `Camera`, `SpeedCameraConfig`, locations and security assignments.
- Incident DB rows.
- Incident routing rules and user actions.
- Desired camera publication.
- Runtime start/stop requests through the Supervisor client.
- ORM-aware operational cleanup and retention checks.

## Django must not own

- Person/Pose/Stage3/Vehicle inference model processes.
- Runtime camera worker processes.
- Runtime camera source reconnect loops.

## Runtime Supervisor owns

```text
fight/runtime_supervisor/core.py::RuntimeSupervisor
```

It owns exactly one `fight.pipeline_mp.run_multiprocess` parent in normal Supervisor mode.

Supervisor states:

```text
STOPPED
STARTING
RUNNING
STOPPING
FAILED
BACKOFF
```

Local Supervisor state lives under `.runtime_supervisor/` and is operational state, not repository source data.

## Runtime parent owns

```text
fight/pipeline_mp/run_multiprocess.py
```

It owns:

- multiprocessing context (`spawn`),
- shared inference services,
- result routers,
- health queue/registry/watchdog,
- fair scheduling queues,
- `CameraRuntimeManager`,
- desired-state reconciliation,
- file EOF/drain/finalization,
- Reporter and Incident worker.

---

# 4. Desired camera state

Primary files:

```text
fight/runtime_supervisor/camera_state.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
```

Schema version is currently `1`.

Canonical desired camera fields:

```text
camera_id
source
name
enabled
use_fight_detection
use_speed_detection
speed_config
```

A runtime camera is active when:

```text
enabled == true
AND
(use_fight_detection == true OR use_speed_detection == true)
```

Supported modes:

```text
Fight-only
Speed-only
Fight + Speed
neither -> not started
```

Desired state includes a monotonic `revision` and durable `speed_paused` intent.

The registry publishes a Speed branch only when all of these are true:

```text
Camera.is_active
Camera.use_speed_detection
SpeedCameraConfig.enabled
```

Speed config published to runtime contains only bounded required fields:

```text
speed_limit_kmh
tolerance_kmh
calibration_path
roi_enabled
roi_polygon
save_snapshot
save_clip
calibration_revision
```

The calibration revision is based on the calibration file metadata so a calibration update can trigger reconfiguration of the affected camera.

Enabled cameras must have distinct source strings. If Fight and Speed operate on the same physical source, they must be represented as one camera entry with both flags enabled rather than duplicate camera records using the same source.

---

# 5. Dynamic camera lifecycle

Primary implementation:

```text
fight/pipeline_mp/camera_lifecycle.py::CameraRuntimeManager
```

Per-camera runtime object owns:

```text
camera
slot_id
generation
stop_event
fight_queue
preview_queue
speed_queue
speed_stop
speed_epoch
speed_failed
speed_restarts
processes
state
file_done
restart_count
```

Process composition depends on camera capability:

```text
Fight-only:
  ingest + camera_worker + preview

Speed-only:
  ingest + speed_worker + preview

Fight + Speed:
  ingest + camera_worker + speed_worker + preview
```

Camera restart identity currently includes:

```text
source
use_fight_detection
use_speed_detection
serialized speed_config
```

Therefore changing source, detection-mode flags, Speed calibration/config, etc. restarts only the affected camera runtime, not the whole global runtime.

Cosmetic changes should not require a restart.

---

# 6. Slot, generation and Speed epoch

Camera identity is protected by:

```text
(camera_id, slot_id, generation)
```

Rules:

- stable result slots are created before child spawn,
- camera restart increments generation,
- camera removal invalidates the slot before teardown,
- delayed results from old generations are rejected,
- health events are generation-aware,
- per-slot scheduling counters reset for new generations.

Speed adds a second identity dimension:

```text
consumer_epoch
```

Speed worker/service restarts can invalidate old Speed work without requiring a camera generation change.

Vehicle requests/results carry both generation and Speed epoch. Speed health events also carry the consumer epoch. Durable Speed publication is guarded by generation + epoch before appending to the outbox.

Do not replace this with `multiprocessing.Manager`, mutable fork-only routing, or queues sent through other queues.

---

# 7. CameraIngest and source ownership

Primary implementation:

```text
fight/pipeline_mp/camera_ingest.py
```

Intended production topology:

```text
                     +--> Fight consumer
Physical camera ---> CameraIngest
                     +--> Speed consumer
                     +--> Preview
```

`camera_worker` and `speed_worker` do not open the centralized source themselves.

RTSP/live reconnect ownership belongs to `CameraIngest`.

## Important current exception

The Supervisor-aware Speed **calibration frame** endpoint correctly reads the common runtime preview and refuses to open a second source when common runtime ownership is active/unknown.

However, the legacy Speed dashboard streaming path in:

```text
Fight_backend_project/backend_frontend_project/speed_detection/views.py
  -> speed_camera_stream
  -> _mjpeg_frame_generator
  -> _open_camera_source
```

still directly opens `camera.source` with OpenCV.

Therefore the strict "one physical source open" invariant is currently guaranteed by the centralized AI runtime itself, but **is not yet guaranteed if that legacy Django Speed MJPEG stream endpoint is used concurrently with the Supervisor runtime**.

This is known architecture debt and must be removed/repointed to common runtime preview/stream ownership without reintroducing Django source ownership.

---

# 8. Fight inference ownership

## Person

```text
camera_worker[N]
  -> fair per-slot admission
  -> shared Person worker
  -> Person result router
  -> per-slot result channel
  -> camera_worker[N]
```

One shared Person model, not one per camera.

## Pose

```text
camera_worker[N]
  -> ROI
  -> fair per-slot admission
  -> shared Pose worker
  -> Pose result router
  -> per-slot result channel
  -> camera-local PoseGate/history
```

Pose temporal interpretation remains camera-local.

## Stage3 / X3D

```text
camera_worker
  -> Stage3 candidate
  -> fair bounded Stage3 admission
  -> shared Stage3/X3D worker
  -> Incident worker
```

IncidentAggregator must not instantiate a second local Person/Pose model.

---

# 9. Speed inference ownership

Primary implementation:

```text
fight/pipeline_mp/speed_worker.py
```

`speed_worker` owns camera-local state:

- ROI/calibration,
- motion gate,
- tracker,
- speed estimator,
- violation decider,
- evidence buffer/writer.

It does **not** own a YOLO model.

Vehicle inference topology:

```text
speed_worker[N]
  -> FairRequestQueue per stable slot
  -> shared vehicle_service_main
  -> one lazy VehicleDetector model
  -> per-slot VehicleResult queue
  -> speed_worker[N]
```

The Vehicle detector is lazily instantiated on first useful inference request inside the shared Vehicle service.

Vehicle result payloads do not return frame pixels through the result queue.

Speed's old standalone multiprocess runner code still exists for compatibility/history, but the normal Django Speed start/stop bridge now uses the common Runtime Supervisor and does not own a second Speed runtime process.

---

# 10. Speed Django bridge

Primary file:

```text
Fight_backend_project/backend_frontend_project/services/speed_bridge/speed_runner.py
```

Normal behavior:

```text
start_speed_pipeline
  -> require common Supervisor mode
  -> clear speed_paused
  -> reconcile desired cameras
  -> start/use common runtime

stop_speed_pipeline
  -> set speed_paused = true
  -> disable only Speed branches
  -> Fight remains available
```

`CommonRuntimeProcess` is a read-only compatibility facade over common runtime status. It does not own a Popen child.

Status is reconstructed from Supervisor/runtime state rather than depending on the Django process that originally handled the start request.

---

# 11. Fair scheduling and bounded capacity

Primary implementation:

```text
fight/pipeline_mp/scheduling.py::FairRequestQueue
```

Structure:

```text
slot 0 -> bounded FIFO
slot 1 -> bounded FIFO
slot 2 -> bounded FIFO
...
        -> round-robin shared consumer
```

Current fair stages:

```text
Person
Pose
Stage3
Vehicle
```

Counters:

```text
accepted
rejected_capacity
deferred_file
dropped_live
stale_generation
dispatches
high_water
```

A hot camera cannot consume another slot's reserved pending capacity.

Fight and Speed admission paths are separate so one consumer should not permanently starve the other through one common FIFO.

Correctness must not depend on OS queue `qsize()`/`empty()` observations.

---

# 12. Live vs file semantics

## Live / RTSP

Priority:

```text
freshness > completeness
```

Rules:

- bounded queues,
- stale live frames/inference may be explicitly shed,
- shed/stale outcomes are not interpreted as negative detections,
- `CameraIngest` owns reconnect,
- Speed consumer may be restarted locally after failure using cooldown/restart limits while the source stays live.

## File

Priority:

```text
correctness + ordering > freshness
```

Rules:

- ordered work waits/defer rather than silently dropping required work,
- admitted Stage3 work drains before final completion,
- Speed frame/EOF delivery drains before clean completion,
- normal non-looping EOF is not failure,
- a failed Speed file consumer is not replayed as if nothing happened,
- a file run with Speed failure completes as incomplete (`exit 13`) rather than reporting clean success.

---

# 13. Health architecture

Primary implementation:

```text
fight/pipeline_mp/health.py
```

Topology:

```text
child processes
  -> one bounded HealthEvent queue
  -> runtime parent
  -> HealthRegistry
  -> RuntimeWatchdog
  -> atomic runtime_health.json
```

Health uses monotonic clocks for liveness/stall decisions.

Camera components registered in health:

```text
camera_ingest
camera_worker
camera_preview
speed_worker
```

Shared workers currently represented:

```text
person
person_router
pose
pose_router
stage3
incident
vehicle
```

Health stores current state only; transition history is bounded.

Capacity pressure may produce:

```text
DEGRADED / queue_pressure
```

but queue pressure alone must not trigger camera/runtime restart storms.

Disk pressure may degrade runtime health but is not itself a restart reason.

---

# 14. Current failure domains

## Camera / ingest failure

A confirmed camera-local failure can restart the affected camera generation through `CameraRuntimeManager`.

## Preview failure

Preview is non-critical and may restart independently.

## Speed consumer failure

A Speed consumer failure:

```text
-> marks Speed failed for that camera
-> invalidates its consumer epoch
-> leaves Fight/ingest generation intact
```

For live sources, the Speed consumer can be restarted after configured cooldown/retry limits if the shared Vehicle service is available.

For file sources, failed Speed processing is not silently restarted/replayed.

## Critical Fight shared worker failure

Current critical shared-worker set:

```text
person
person_router
pose
pose_router
stage3
incident
```

Confirmed failure of these workers can fail the whole runtime and leave recovery to the Supervisor.

## Vehicle shared worker failure — current behavior

Vehicle is intentionally excluded from the critical shared-worker aggregate.

If the Vehicle worker becomes unavailable:

```text
Fight can continue
runtime becomes DEGRADED rather than FAILED solely because of Vehicle
Speed consumers are withdrawn/disabled
```

**Current limitation:** `run_multiprocess` does not yet recreate/restart the dead shared Vehicle service process. Speed consumer restart only works while the shared Vehicle service itself is available.

This is a primary Phase-14 target.

---

# 15. Current shared-worker startup behavior

This section is intentionally explicit because it is the next architecture debt.

In the current dynamic runtime, the parent eagerly starts:

```text
Person
Person result router
Pose + Pose router (when runtime.use_pose)
Stage3
Incident
Vehicle
Reporter
```

before reconciling the actual desired camera capabilities.

Therefore today:

```text
Fight-only deployment
  -> Vehicle process still starts

Speed-only deployment
  -> Fight shared workers still start
```

The Vehicle model itself is lazy, but the Vehicle service process/queues still exist. Fight shared workers are also created even when no Fight camera is desired.

This is **not** the desired final production state.

Next architecture should derive required shared services from the desired capability set and start/stop/recover them without unnecessary whole-runtime restarts.

---

# 16. Incident durability boundary

Runtime incident output uses the existing durable outbox.

Primary files:

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/speed_worker.py
```

Outbox semantics:

- append-only JSONL,
- serialized writers,
- partial-tail preservation,
- flush/fsync before legacy success publication,
- explicit failure on persistence errors.

Speed event publication creates a normal outbox envelope with:

```text
incident_type = SPEED
camera_id
run_id
external_incident_id
detected_at / finalized_at
evidence_path
speed metadata
  - measured speed
  - limit/tolerance/threshold data
  - generation
  - consumer_epoch
```

Durable publication is generation/epoch guarded so stale Speed work cannot create current incidents after reconfiguration/restart.

---

# 17. Django incident domain

Primary models:

```text
incidents.models.Incident
incidents.models.IncidentRoutingRule
incidents.models.IncidentRoute
incidents.models.IncidentAuditEvent
incidents.models.IncidentIngestCursor
incidents.models.IncidentIngestRecord
```

Incident types:

```text
FIGHT
SPEED
OTHER
```

Runtime does not write these ORM rows directly.

Flow:

```text
durable outbox
  -> run_incident_dispatcher
  -> incidents.services.ingest
  -> Incident
  -> routing / escalation
```

Camera deletion is protected by Incident references.

---

# 18. Location / authorization

Primary models:

```text
adminx.Location
adminx.SecurityUnit
adminx.SecurityUnitCoverage
adminx.UserSecurityAssignment
streams.Camera
```

Conceptual scope:

```text
User
  -> SecurityUnit assignment
  -> SecurityUnitCoverage
  -> Location tree
  -> Camera.location
  -> incident/preview/action visibility
```

Fight and Speed must use the same authorization/location model.

Legacy `Camera.faculty` exists only as compatibility; physical authorization should use `Camera.location` when assigned.

---

# 19. Runtime durability / retention

Phase-12 durability remains part of the architecture contract.

Primary files:

```text
fight/operations.py
fight/retention.py
fight/service_loop.py
incidents/services/retention.py
fight/runtime_supervisor/core.py
```

Important rules:

- atomic Supervisor/desired-state writes,
- outbox/evidence durability is explicit,
- cleanup is bounded,
- current/active/unknown/abnormal runs fail closed against cleanup,
- Incident-referenced evidence is protected,
- evidence is retained indefinitely by default,
- optional evidence cleanup requires consumed durable outbox and offline/exclusive maintenance conditions,
- disk pressure degrades health but does not cause restart storms.

Operational local directories are not source files and must not be committed:

```text
.runtime_supervisor/
Fight_backend_project/backend_frontend_project/media/pipeline_runs/.run_locks/
Fight_backend_project/backend_frontend_project/media/runtime_spool/
```

---

# 20. Current production runtime vs legacy paths

The architecture contract applies to the **Supervisor-managed dynamic runtime**.

Legacy/static paths remain in the repository for compatibility/testing. Do not infer production ownership from a legacy helper merely because it still exists.

Examples:

- legacy direct runtime control in `fight_runner.py`,
- old standalone Speed config/command builder helpers in `speed_runner.py`,
- static `run_multiprocess` path,
- legacy Speed MJPEG stream source-open path (this one is still user-reachable and therefore must be treated as active debt, not harmless dead code).

Normal architecture work should improve the Supervisor-managed dynamic path without unnecessarily rewriting legacy code unless it violates current production ownership or causes regression.

---

# 21. Phase-13 guarantees now present on master

Phase 13 added and validated these contracts:

```text
Fight-only / Speed-only / Fight+Speed camera modes
single CameraIngest fan-out for runtime consumers
shared Vehicle inference service
camera-local Speed tracking/calibration state
Speed generation + consumer-epoch stale-work rejection
bounded Speed admission
live shedding vs ordered file behavior
Speed consumer failure isolation from Fight
Speed durable outbox -> Incident(type=SPEED)
common Supervisor start/stop/status for Speed
speed_paused desired-state intent
```

Focused tests live in:

```text
tests/test_speed_integration.py
Fight_backend_project/backend_frontend_project/incidents/phase13_tests.py
```

The Phase-13 validation run reported:

```text
135 passed, 1 skipped
```

with compileall, Django check, migration check and `git diff --check` clean apart from normal Windows line-ending warnings.

---

# 22. Next architecture phase (Phase 14)

Primary goal:

```text
Capability-aware shared-worker lifecycle + Vehicle service recovery
```

Required outcomes:

1. Fight-only desired camera set must not start Vehicle service/model infrastructure unnecessarily.
2. Speed-only desired camera set must not eagerly start unnecessary Fight inference services.
3. First Speed camera added to a running Fight runtime should start Vehicle service without global runtime restart.
4. Removing the last Speed camera should not disturb Fight; optional grace/hysteresis may avoid model/service thrash.
5. Dead/hung Vehicle service should be restartable with bounded retry/backoff while Fight remains healthy.
6. Vehicle service restart must invalidate stale Speed work/results before they can create incidents.
7. Health must distinguish required/running/restarting/disabled optional services so absence of an unneeded worker is healthy.
8. Do not generalize Fight shared-worker hot replacement unless needed; focus on the Vehicle service introduced by Phase 13.
9. Remove/repoint the legacy Django Speed MJPEG direct source-open path so common runtime source ownership is not violated when the Speed dashboard is viewed.

Do not mix into Phase 14:

```text
PostgreSQL
UI redesign
Fight threshold/model tuning
Speed calibration/accuracy redesign
shared-memory frame transport
large GPU benchmark
Nginx/media offload
production deployment packaging
```

---

# 23. Task router

## Dynamic camera lifecycle

Read together:

```text
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/generation.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/camera_state.py
services/pipeline_bridge/camera_registry.py
tests/test_dynamic_camera_lifecycle.py
```

## Fight inference

```text
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/scheduling.py
```

## Speed integration

```text
fight/pipeline_mp/speed_worker.py
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/health.py
fight/runtime_supervisor/camera_state.py
services/pipeline_bridge/camera_registry.py
services/speed_bridge/speed_runner.py
speed_detection/views.py
HizTespiti/speed/src/*
HizTespiti/yolo/src/vehicle_detector.py
tests/test_speed_integration.py
incidents/phase13_tests.py
```

## Health/watchdog

```text
fight/pipeline_mp/health.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/camera_lifecycle.py
fight/runtime_supervisor/core.py
tests/test_runtime_health.py
```

## Backpressure/fairness

```text
fight/pipeline_mp/scheduling.py
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/speed_worker.py
tests/test_capacity_scheduling.py
```

## Incidents/durability

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/speed_worker.py
incidents/services/ingest.py
incidents/services/retention.py
incidents/models.py
```

## Authorization/location

```text
adminx/models.py
streams/models.py
services/access_scope.py
incidents/models.py
```

---

# 24. Cross-phase invariants checklist

Before accepting architecture-affecting changes, verify all relevant items:

```text
[ ] one runtime source/decode owner per physical camera
[ ] no runtime Django ORM imports
[ ] Supervisor still owns global AI runtime lifecycle
[ ] model sharing preserved; no model-per-camera regression
[ ] camera slot/generation stale protection preserved
[ ] Speed consumer epoch protection preserved
[ ] Fight failure semantics unchanged unless explicitly in scope
[ ] Speed failure does not unnecessarily kill Fight
[ ] live freshness / file ordering semantics preserved
[ ] no qsize()-based correctness
[ ] bounded queues / bounded telemetry
[ ] Windows spawn compatibility
[ ] durable outbox before Incident DB import
[ ] Speed uses Incident(type=SPEED), not a parallel incident store
[ ] retention/disk-pressure semantics preserved
[ ] no UI/template/static redesign during backend-only phases
[ ] PostgreSQL untouched unless explicitly in scope
```

---

# 25. Deferred work after Phase 14

Still separate unless explicitly promoted:

- production-scale CPU/RAM/VRAM/latency/throughput measurement,
- shared-memory transport decision for large NumPy frames,
- multi-GPU partitioning,
- PostgreSQL migration,
- production service/deployment packaging,
- Nginx/media offload,
- dashboard and incident UX redesign,
- preview/offline UX redesign,
- durable incident-history archival/compaction,
- legacy/aborted-run operator tooling.
