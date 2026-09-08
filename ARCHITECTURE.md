# Fight-Detection Architecture Contract

## Document ownership

This file is the architecture contract for the repository.

**Maintenance rule:** coding agents (including Codex) must **read** this file before architecture-affecting work, but must **not edit it unless the project owner explicitly asks them to**. The project owner and ChatGPT maintain this document from the committed repository state.

Current reference commit:

```text
c3c019b2871dcd3891b24ef242a2c5e93fe9212f
feat: add resilient fight service recovery
```

Phase 15 is committed on `master` and is the current architecture baseline.

---

# 1. Core architecture rules

The system is split into four ownership planes:

```text
Django / Web / DB
    = control + application plane

Runtime Supervisor
    = owner of the global AI runtime process

run_multiprocess
    = runtime parent/orchestrator

CameraIngest
    = single physical source/decode owner per camera in production runtime
```

Primary invariants:

1. Runtime workers must not import Django ORM.
2. Django/Gunicorn must not own AI child processes.
3. Runtime Supervisor owns the global AI runtime lifecycle.
4. One physical camera must have one `CameraIngest` source/decode owner in the Supervisor-managed production runtime.
5. Expensive/stateless inference models are shared services, not one model per camera.
6. Shared inference services are capability-aware: they run only while current desired cameras require them.
7. Temporal/tracking/calibration state remains camera-local where correctness requires it.
8. Camera work is identified by stable slot + generation; Speed adds a consumer epoch; shared-service reincarnation adds a service epoch.
9. Fight recovery must fence pre-failure Stage3 publication before replacement transport can become current.
10. Live and file sources intentionally use different backpressure/recovery semantics.
11. Runtime incident truth crosses into Django through the durable incident outbox.
12. `Incident(type=FIGHT)` and `Incident(type=SPEED)` use the same Django incident/routing domain.
13. Queue `qsize()`/`empty()` observations must not be correctness dependencies.
14. Windows `spawn` compatibility is a first-class constraint.
15. Optional service absence is not a health failure when that service is not required.
16. Recovery is bounded. A failed service must not create unbounded restart storms.

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
  +--> Reporter                         (runtime-global)
  +--> Incident worker / aggregator    (runtime-global)
  |
  +--> SharedServices                  (capability + recovery owner)
  |      |
  |      +--> Fight bundle, only when required
  |      |      +--> Person worker
  |      |      +--> Person result router
  |      |      +--> Pose worker/router when runtime.use_pose
  |      |      +--> Stage3/X3D worker when runtime.use_stage3
  |      |
  |      +--> Vehicle bundle, only when required
  |             +--> shared Vehicle worker
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
  -> shared Person / Pose / Stage3
  -> Fight service-epoch tagged Stage3 result
  -> Incident worker
  -> IncidentAggregator publication fence
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
- Browser-facing read-only consumption of runtime-produced previews.

## Django must not own

- Person/Pose/Stage3/Vehicle inference model processes.
- Runtime camera worker processes.
- Runtime camera source reconnect loops.
- A second physical camera connection while the common runtime owns the camera.

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
- capability-aware shared inference services,
- shared-service recovery,
- shared-service result routers,
- health queue/registry/watchdog,
- fair scheduling queues,
- `CameraRuntimeManager`,
- desired-state reconciliation,
- file EOF/drain/finalization,
- Reporter and Incident worker.

Capability/recovery lifecycle is delegated to:

```text
fight/pipeline_mp/shared_services.py::SharedServices
```

Reporter and Incident remain runtime-global services outside the recoverable Fight/Vehicle bundles.

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

The calibration revision is based on calibration-file metadata so a calibration update can reconfigure the affected camera.

Enabled cameras must have distinct source strings. If Fight and Speed operate on the same physical source, they must be represented by one camera entry with both flags enabled rather than duplicate camera records using the same source.

Desired revisions are applied inside the existing runtime parent. Capability changes must not require a global runtime restart merely because Fight or Speed demand changed.

---

# 5. Dynamic camera lifecycle

Primary implementation:

```text
fight/pipeline_mp/camera_lifecycle.py::CameraRuntimeManager
```

Per-camera runtime state includes:

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
speed_service_waiting
speed_restarts
fight_service_waiting
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

Camera restart identity includes:

```text
source
use_fight_detection
use_speed_detection
serialized speed_config
```

Therefore changing source, detection-mode flags, Speed calibration/config, etc. restarts only the affected camera runtime. Cosmetic changes should not require a restart.

The manager does not create a Speed consumer while the shared Vehicle service is unavailable.

During Fight bundle recovery, affected Fight-capable cameras are explicitly suspended. Their generations are invalidated before old readers/transports are torn down. Eligible live cameras are restarted locally after the replacement Fight bundle exists. Speed-only cameras are not involved. A mixed Fight+Speed camera is restarted locally as one camera runtime, so its Speed consumer may be briefly interrupted even though the shared Vehicle service itself is preserved.

Unsafe camera/source withdrawal fails closed; recovery must not create a duplicate physical source owner.

---

# 6. Identity: slot, generation, consumer epoch and service epoch

Camera identity is protected by:

```text
(camera_id, slot_id, generation)
```

Rules:

- stable result slots are reserved in the parent before child spawn,
- camera restart increments generation,
- Fight recovery invalidates affected camera generations before transport replacement,
- camera removal invalidates the slot before teardown,
- delayed results from old generations are rejected,
- health events are generation-aware,
- per-slot scheduling counters reset for new generations.

Speed adds:

```text
consumer_epoch
```

A Speed consumer stop/restart increments the slot's Speed epoch. Vehicle requests/results carry both camera generation and consumer epoch. Durable Speed publication is guarded by generation + consumer epoch before outbox append.

Shared-service reincarnation adds:

```text
service_epoch
```

`SharedServices` wraps the single bounded health channel with the current service epoch. Old Fight- or Vehicle-service health events are rejected after replacement.

Fight Stage3 results additionally carry their Fight service epoch into the Incident worker. This identity is used by the Fight incident publication fence so buffered results from a failed service incarnation cannot later publish as current incidents.

Vehicle transport and Fight transport are replaced on recovery rather than reusing queues/locks from the failed incarnation.

Do not replace this identity model with `multiprocessing.Manager`, mutable fork-only routing, queues sent through other queues, or PID-only correctness.

---

# 7. CameraIngest and source ownership

Primary implementation:

```text
fight/pipeline_mp/camera_ingest.py
```

Production topology:

```text
                     +--> Fight consumer
Physical camera ---> CameraIngest
                     +--> Speed consumer
                     +--> Preview
```

`camera_worker` and `speed_worker` do not open the centralized source themselves. RTSP/live reconnect ownership belongs to `CameraIngest`.

## Django Speed stream ownership after Phase 14

Primary files:

```text
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/common_preview.py
Fight_backend_project/backend_frontend_project/speed_detection/views.py
```

`speed_camera_stream` no longer opens `camera.source` with `cv2.VideoCapture` in the common-runtime path. It serves the atomically replaced JPEG produced under the active runtime's `previews/` directory through `common_preview`.

The preview helper:

- validates the active run against `PIPELINE_OUTPUT_BASE`,
- validates the per-camera preview path stays under the run preview root,
- pins the stream to the active run token,
- closes when runtime ownership changes/disappears,
- bounds JPEG reads to 8 MiB,
- never opens a physical camera source.

Supervisor/common-runtime ownership therefore fails closed if preview ownership is unavailable.

Speed calibration follows the same ownership rule while Supervisor/common runtime is active or ownership is uncertain. A direct source-open fallback may exist only in the explicit legacy/non-Supervisor path when runtime state is cleanly `STOPPED`; it is not part of the production Supervisor-managed ownership model.

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
  -> FightIncidentChannel(service_epoch)
  -> Incident worker
```

IncidentAggregator must not instantiate a second local Person/Pose model.

The Fight shared bundle is demand-started by `SharedServices` and, as of Phase 15, supports bounded in-runtime replacement after confirmed recoverable Fight worker death/stall. Recovery is bundle-wide rather than per-model because the bundle owns coupled request/result transport and generation-sensitive consumers.

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

The Vehicle detector is lazily instantiated on first useful inference request inside the shared Vehicle service. Vehicle result payloads do not return frame pixels through the result queue.

Vehicle inference/model errors are service failures, not negative detections.

Speed's old standalone multiprocess runner code still exists for compatibility/history, but normal Django Speed control uses the common Runtime Supervisor and does not own a second Speed runtime process.

---

# 10. Capability-aware shared-service lifecycle

Primary implementation:

```text
fight/pipeline_mp/shared_services.py
```

Required capabilities are derived from the current enabled desired camera set:

```text
fight   = any active camera with use_fight_detection
vehicle = any active camera with use_speed_detection
```

Current service groups:

```text
Fight bundle:
  person
  person_router
  pose + pose_router       when runtime.use_pose
  stage3                   when runtime.use_stage3

Vehicle bundle:
  vehicle
```

Runtime-global Incident and Reporter services are outside these capability bundles.

Behavior:

```text
Fight-only desired set
  -> Fight bundle runs
  -> Vehicle service is disabled / absent

Speed-only desired set
  -> Vehicle service runs
  -> Fight inference bundle is disabled / absent

Fight + Speed desired set
  -> both required bundles run
```

Capability transitions occur inside the same runtime parent:

- first Speed camera can hot-start Vehicle without global runtime restart,
- last Speed removal can stop Vehicle without disturbing Fight,
- first Fight camera can hot-start the Fight bundle while Speed remains active,
- last Fight removal drains acknowledged Fight work before stopping the Fight bundle,
- unrelated bundle processes are preserved across capability transitions.

Unused service bundles stop after a bounded idle grace. Default:

```text
SHARED_SERVICE_IDLE_GRACE_SEC=5
```

Fight shutdown additionally waits for owned fair-admission state and JoinableQueue acknowledgements so accepted work is not abandoned merely because the final Fight camera disappeared.

Capability lifecycle is a property of the Supervisor-managed dynamic path. Legacy/static runtime paths are not the production reference.

---

# 11. Shared-service recovery and failure isolation

## Vehicle recovery

Vehicle is optional to Fight.

On confirmed Vehicle death/stall/start failure:

```text
Vehicle failure
  -> mark shared Vehicle unavailable
  -> withdraw affected Speed consumers
  -> increment Speed consumer epochs
  -> stop/close failed Vehicle transport
  -> replace Vehicle request/result transport
  -> increment Vehicle service epoch
  -> bounded exponential-backoff restart
  -> resume eligible live Speed consumers after recovery
```

Fight camera ingest, Fight workers, previews and incident infrastructure continue.

Default controls:

```text
VEHICLE_SERVICE_RESTART_LIMIT=3
VEHICLE_SERVICE_RESTART_BACKOFF_SEC=2
VEHICLE_SERVICE_RESTART_MAX_BACKOFF_SEC=30
```

Exhaustion leaves Vehicle in a failed optional-service state and requires operator intervention; it must not force a Fight-wide runtime failure solely because Vehicle is unavailable.

## Fight bundle recovery

Recoverable Fight components:

```text
person
person_router
pose
pose_router
stage3
```

Incident and Reporter are deliberately **not** part of this recoverable bundle.

On confirmed recoverable Fight worker death/stall:

```text
Fight component failure
  -> raise Fight publication floor before transport teardown
  -> invalidate generations for affected Fight-capable cameras
  -> mark affected cameras fight_service_waiting
  -> stop affected camera runtimes/source owners
  -> stop the old Fight bundle
  -> close/replace Fight request/result transport
  -> increment Fight service epoch
  -> bounded exponential-backoff restart
  -> recreate required Fight bundle
  -> restart eligible LIVE Fight-capable cameras locally
```

The global `run_multiprocess` parent remains the same when recovery succeeds.

Unrelated domains are preserved:

- Vehicle service process is not replaced because Fight failed.
- Speed-only cameras remain untouched.
- Incident and Reporter process identities remain unchanged.
- A mixed Fight+Speed camera is locally restarted, so its Speed consumer may be briefly interrupted, but Vehicle itself remains alive.

Default controls:

```text
FIGHT_SERVICE_RESTART_LIMIT=3
FIGHT_SERVICE_RESTART_BACKOFF_SEC=2
FIGHT_SERVICE_RESTART_MAX_BACKOFF_SEC=30
```

With defaults, replacement attempts occur after approximately 2 / 4 / 8 seconds. Replacement-start failures consume the same bounded runtime-lifetime retry budget.

Fight recovery exhaustion escalates to the existing runtime-level failure path. Unsafe teardown/withdrawal also fails closed rather than risking duplicate source ownership.

## Fight publication fence

Primary implementation:

```text
fight/pipeline_mp/shared_services.py::FightIncidentChannel
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
```

Every Stage3 result from the Fight bundle is tagged with that Fight service epoch. A shared `fight_publication_floor` is advanced **before** failed Fight transport is replaced.

The IncidentAggregator checks that floor:

- when accepting Stage3 results,
- when deciding whether buffered incident state remains current,
- immediately before durable outbox/legacy JSONL publication under the shared floor lock.

Therefore Stage3 output buffered before or during a failed incarnation cannot create a current incident after recovery.

## File-source recovery rules

Speed:

- partially failed Speed file processing is not replayed after Vehicle recovery.

Fight:

- if a recoverable Fight service fails while an affected Fight file source is active, the run fails closed with `fight_file_incomplete`,
- the file is not automatically replayed from frame 0,
- the old partial camera runtime is not presented as clean completion.

## Live-source recovery rules

- eligible live Speed consumers can resume after Vehicle recovery,
- eligible live Fight-capable cameras are locally restarted on fresh Fight transport after Fight bundle recovery,
- generation/service-epoch fences reject stale work from the failed incarnation.

---

# 12. Speed Django bridge

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
  -> disable Speed branches
  -> Fight remains available
```

`CommonRuntimeProcess` is a read-only compatibility facade over common runtime status. It does not own a Popen child.

Status is reconstructed from Supervisor/runtime state rather than depending on the Django process that originally handled the start request.

---

# 13. Fair scheduling and bounded capacity

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

A hot camera cannot consume another slot's reserved pending capacity. Fight and Speed admission paths are separate so one consumer cannot permanently starve the other through one common FIFO.

Correctness must not depend on OS queue `qsize()`/`empty()` observations. Internal owned counters/acknowledgements may be used where the runtime owns their semantics.

---

# 14. Live vs file semantics

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
- camera-local Speed consumer recovery is bounded,
- shared Vehicle recovery may resume eligible live Speed consumers,
- shared Fight recovery may locally restart eligible live Fight-capable camera runtimes,
- mixed Fight+Speed cameras may briefly interrupt their Speed consumer during Fight camera-local restart.

## File

Priority:

```text
correctness + ordering > freshness
```

Rules:

- ordered work waits/defer rather than silently dropping required work,
- admitted Stage3 work drains before normal final completion,
- Speed frame/EOF delivery drains before clean completion,
- normal non-looping EOF is not failure,
- failed Speed file processing is not replayed after local/shared recovery,
- Fight service failure during affected file processing is fatal/incomplete rather than replayed,
- a partial failed file run must not be reported as clean success.

---

# 15. Health architecture

Primary implementation:

```text
fight/pipeline_mp/health.py
fight/pipeline_mp/shared_services.py
fight/runtime_supervisor/core.py
```

Topology:

```text
child processes
  -> one bounded HealthEvent queue
  -> runtime parent
  -> HealthRegistry
  -> RuntimeWatchdog
  -> atomic runtime_health.json
  -> Supervisor /runtime/health
```

Health uses monotonic clocks for liveness/stall decisions.

Camera components:

```text
camera_ingest
camera_worker
camera_preview
speed_worker
```

Camera status also exposes `fight_service_waiting` while Fight recovery has withdrawn a camera pending replacement transport.

Shared worker records:

```text
person
person_router
pose
pose_router
stage3
incident
vehicle
```

Capability/recovery-managed worker records expose:

```text
required
service_state
service_epoch
restart_count
```

Service states include:

```text
disabled
starting
running
restarting
failed
```

Semantics:

- not required + disabled => `HEALTHY / service_disabled`,
- required + recovering Fight/Vehicle service => runtime may be `DEGRADED`,
- confirmed recoverable Fight component failure is converted to service recovery rather than immediately forcing runtime failure,
- Fight recovery exhaustion => required Fight service `FAILED` and runtime-level failure,
- Vehicle recovery exhaustion => optional Vehicle service failed/degraded without forcing Fight-wide failure,
- configured-off Pose/Stage3 absence is expected rather than a failure,
- queue pressure may degrade health but must not trigger restart storms,
- disk pressure may degrade health but is not itself a restart reason.

Health stores current state only; transition history remains bounded.

---

# 16. Failure domains

## Camera / ingest failure

A confirmed camera-local failure can restart the affected camera generation through `CameraRuntimeManager`.

## Preview failure

Preview is non-critical and may restart independently.

## Speed consumer failure

A local Speed consumer failure:

```text
-> marks Speed failed for that camera
-> invalidates its consumer epoch
-> leaves Fight/ingest generation intact
```

Live local recovery remains bounded. File failures are not silently replayed.

## Recoverable Fight shared-worker failure

Recoverable set:

```text
person
person_router
pose
pose_router
stage3
```

Confirmed death/stall now enters bounded in-runtime Fight bundle recovery.

Global failure still occurs for:

```text
Fight recovery exhaustion
unsafe Fight camera/source withdrawal
Fight file-source service failure (fight_file_incomplete)
initial Fight service startup failure
Incident worker failure
Reporter failure
```

Incident and Reporter deliberately retain their runtime-global critical semantics.

## Vehicle shared-worker failure

Vehicle is non-critical to Fight and has its own bounded in-runtime recovery lifecycle.

---

# 17. Incident durability boundary

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

Speed event publication creates a normal outbox envelope containing the common incident identity plus Speed metadata such as measured speed, limit/tolerance/threshold data, generation and consumer epoch.

Durable Speed publication is generation/epoch guarded so stale work cannot create a current incident after reconfiguration/restart.

Fight incident publication additionally uses the Phase-15 Fight service-epoch publication fence. Buffered Stage3 state whose epoch is below the current publication floor is discarded and cannot append a durable incident after a failed Fight service incarnation.

---

# 18. Django incident domain

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

Runtime does not write ORM rows directly.

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

# 19. Location / authorization

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

Fight and Speed use the same authorization/location model.

Legacy `Camera.faculty` exists only as compatibility; physical authorization should use `Camera.location` when assigned.

---

# 20. Runtime durability / retention

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

Operational/runtime directories are not source files and must not be committed:

```text
.runtime_supervisor/
Fight_backend_project/backend_frontend_project/media/camera_uploads/
Fight_backend_project/backend_frontend_project/media/pipeline_runs/.run_locks/
Fight_backend_project/backend_frontend_project/media/runtime_spool/
```

`media/camera_uploads/` contains local uploaded/demo camera media and is operational content, not Phase source code.

---

# 21. Production runtime vs legacy paths

This contract applies to the **Supervisor-managed dynamic runtime**.

Legacy/static paths remain for compatibility/testing. Do not infer production ownership from a legacy helper merely because it still exists.

Examples:

- legacy direct runtime control in `fight_runner.py`,
- old standalone Speed config/command builder helpers in `speed_runner.py`,
- static `run_multiprocess` path,
- direct calibration source-open fallback only in explicit non-Supervisor/cleanly stopped legacy mode.

The user-reachable Speed dashboard stream is no longer an active duplicate-source exception: in the production/common-runtime path it consumes common runtime preview ownership.

Normal architecture work should improve the Supervisor-managed dynamic path without unnecessarily rewriting legacy code unless legacy behavior violates active production ownership or causes regression.

---

# 22. Phase-13 guarantees retained

Phase 13 established:

```text
Fight-only / Speed-only / Fight+Speed camera modes
single CameraIngest fan-out for runtime consumers
shared Vehicle inference model/service
camera-local Speed tracking/calibration state
Speed generation + consumer-epoch stale-work rejection
bounded Speed admission
live shedding vs ordered file behavior
Speed consumer failure isolation from Fight
Speed durable outbox -> Incident(type=SPEED)
common Supervisor start/stop/status for Speed
speed_paused desired-state intent
```

Focused tests:

```text
tests/test_speed_integration.py
Fight_backend_project/backend_frontend_project/incidents/phase13_tests.py
```

Phase-13 validation baseline:

```text
135 passed, 1 skipped
```

---

# 23. Phase-14 guarantees retained

Phase 14 established:

```text
capability-aware shared service lifecycle
Fight-only without Vehicle process
Speed-only without unnecessary Fight inference workers
same-runtime hot start/stop of capability bundles
bounded idle grace / Fight drain before shutdown
Vehicle crash/stall/start-failure recovery
Vehicle transport replacement on recovery
bounded exponential Vehicle retry/backoff
Vehicle service epoch health isolation
live Speed resume after Vehicle recovery
file Speed no-replay rule after partial failure
optional-service-aware health
Speed dashboard common-preview ownership
Supervisor-mode fail-closed duplicate-source protection
```

Primary implementation:

```text
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/health.py
fight/pipeline_mp/speed_worker.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/common_preview.py
Fight_backend_project/backend_frontend_project/speed_detection/views.py
fight/runtime_supervisor/core.py
```

Focused tests:

```text
tests/test_shared_services.py
Fight_backend_project/backend_frontend_project/speed_detection/phase14_tests.py
```

Final automated validation:

```text
148 passed, 1 skipped
compileall passed
Django check passed
migration check passed
git diff --check passed
UI/template/static directories unchanged
```

Live acceptance verified Fight-only Vehicle-disabled startup, same-runtime first-Speed hot-add, and same-runtime last-Speed hot-remove.

---

# 24. Phase-15 guarantees now present on master

Phase 15 added bounded in-runtime recovery for the recoverable Fight inference bundle.

Primary guarantees:

```text
Person / Person router / Pose / Pose router / Stage3 failures recover as one Fight bundle
Fight transport is replaced, not reused
camera generations are invalidated before failed transport replacement
Fight service epochs reject stale shared-worker health
old generation Person results are rejected
pre-failure Stage3 publication is fenced before durable incident output
LIVE Fight cameras resume through camera-local restart
Speed-only cameras are preserved
Vehicle process identity is preserved across Fight recovery
Incident and Reporter process identities remain unchanged
mixed Fight+Speed camera may briefly restart locally but Vehicle remains alive
Fight file-source service failure is fail-closed / no replay
replacement-start failures consume the bounded recovery budget
recovery exhaustion escalates to runtime failure
Windows spawn recreation is covered by deterministic tests
```

Default Fight recovery controls:

```text
FIGHT_SERVICE_RESTART_LIMIT=3
FIGHT_SERVICE_RESTART_BACKOFF_SEC=2
FIGHT_SERVICE_RESTART_MAX_BACKOFF_SEC=30
```

Primary implementation:

```text
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/health.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/incident_worker.py
fight/pipeline/incident_aggregator.py
fight/runtime_supervisor/core.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/fight_runner.py
```

Focused tests:

```text
tests/test_fight_service_recovery.py
tests/test_shared_services.py
```

Final automated validation reported:

```text
162 passed, 1 skipped
compileall passed
Django check passed
migration check passed
git diff --check passed
ARCHITECTURE.md unchanged by Codex
UI/template/static directories unchanged
```

Live acceptance additionally verified:

```text
Fight-only startup
  -> Fight bundle runs and processes real work
  -> Vehicle remains HEALTHY / service_disabled
  -> stable runtime health becomes HEALTHY

Speed-only convergence
  -> desired camera set converges to only the Speed camera
  -> Fight bundle becomes HEALTHY / service_disabled after grace
  -> Vehicle remains required/running and processes real work
  -> runtime remains HEALTHY before normal file EOF shutdown
```

Manual random child-process killing was not required for acceptance because deterministic tests cover each recoverable Fight component, health-confirmed hangs, retry exhaustion, stale health/results, publication fencing, file fail-closed behavior, same-parent recovery and Windows-spawn recreation.

Remaining Phase-15-specific debt:

- production GPU/scale validation of recovery behavior,
- reducing the brief Speed interruption on a mixed Fight+Speed camera during Fight bundle recovery if later justified.

---

# 25. Task router

## Dynamic camera + capability lifecycle

```text
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/generation.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/camera_state.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
tests/test_dynamic_camera_lifecycle.py
tests/test_shared_services.py
tests/test_fight_service_recovery.py
```

## Fight inference / recovery

```text
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/incident_worker.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/scheduling.py
tests/test_fight_service_recovery.py
tests/test_shared_services.py
```

## Speed integration / recovery

```text
fight/pipeline_mp/speed_worker.py
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/health.py
fight/runtime_supervisor/camera_state.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/common_preview.py
Fight_backend_project/backend_frontend_project/services/speed_bridge/speed_runner.py
Fight_backend_project/backend_frontend_project/speed_detection/views.py
HizTespiti/speed/src/*
HizTespiti/yolo/src/vehicle_detector.py
tests/test_speed_integration.py
tests/test_shared_services.py
Fight_backend_project/backend_frontend_project/speed_detection/phase14_tests.py
```

## Health/watchdog

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

## Backpressure/fairness

```text
fight/pipeline_mp/scheduling.py
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/speed_worker.py
tests/test_capacity_scheduling.py
```

## Source ownership / preview

```text
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_preview.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/common_preview.py
Fight_backend_project/backend_frontend_project/speed_detection/views.py
Fight_backend_project/backend_frontend_project/speed_detection/phase14_tests.py
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
tests/test_fight_service_recovery.py
```

## Authorization/location

```text
adminx/models.py
streams/models.py
services/access_scope.py
incidents/models.py
```

---

# 26. Cross-phase invariants checklist

Before accepting architecture-affecting changes, verify all relevant items:

```text
[ ] one runtime source/decode owner per physical camera
[ ] Django stream/calibration paths do not steal source ownership in Supervisor mode
[ ] no runtime Django ORM imports
[ ] Supervisor still owns global AI runtime lifecycle
[ ] model sharing preserved; no model-per-camera regression
[ ] shared services start only when current capabilities require them
[ ] optional disabled services remain healthy
[ ] camera slot/generation stale protection preserved
[ ] Speed consumer epoch protection preserved
[ ] Fight/Vehicle service-epoch isolation preserved
[ ] Fight publication floor fences failed Stage3 incarnations
[ ] Fight recovery invalidates generations before replacement transport
[ ] Fight retry/backoff remains bounded; no restart storms
[ ] Vehicle recovery does not unnecessarily kill Fight
[ ] Vehicle retry/backoff remains bounded; no restart storms
[ ] Incident/Reporter remain runtime-global unless explicitly redesigned
[ ] failed/partial file Speed work is not replayed as clean success
[ ] failed/partial file Fight work is not replayed as clean success
[ ] live freshness / file ordering semantics preserved
[ ] no qsize()-based correctness
[ ] bounded queues / bounded telemetry
[ ] Windows spawn compatibility
[ ] durable outbox before Incident DB import
[ ] Speed uses Incident(type=SPEED), not a parallel incident store
[ ] retention/disk-pressure semantics preserved
[ ] no UI/template/static redesign during backend-only phases
[ ] PostgreSQL untouched unless explicitly in scope
[ ] Docker/deployment/Nginx untouched unless explicitly in scope
```

---

# 27. Remaining debt / future backend work

No next phase is fixed by this document. Promote one explicitly before implementation.

Current backend-focused remaining work:

- production-scale CPU/RAM/VRAM/latency/throughput characterization,
- GPU stress/scale validation of mixed Fight + Speed workloads,
- reusable capacity benchmark harness for 1/2/4/8/... real inference workloads,
- control-plane/runtime scaling tests for large camera-slot counts independent of GPU model throughput,
- shared-memory transport only if measurement proves NumPy IPC/copy is a bottleneck,
- soak/chaos validation for long-running runtime behavior,
- production GPU validation of Vehicle and Fight recovery,
- possible reduction of mixed-camera Speed interruption during Fight recovery,
- Fight model quality hardening on broader real-camera/hard-negative data,
- Speed measurement accuracy validation and calibration robustness,
- production storage sizing and cleanup-scan observability,
- durable incident-history archival/compaction,
- legacy/aborted-run operator tooling.

Explicitly deferred for later discussion; do **not** pull these into backend-hardening phases by default:

```text
PostgreSQL migration
Docker
Nginx/media offload
production deployment/service packaging
UI/UX redesign
multi-GPU deployment topology
```

The current priority is to make the backend/runtime excellent first. Deployment/database packaging decisions remain separate until explicitly promoted with their own acceptance criteria.
