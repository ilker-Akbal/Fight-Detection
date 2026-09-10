# Fight-Detection Architecture Contract

## Document ownership and maintenance discipline

This file is the architecture contract for the repository. It describes the **current Supervisor-managed production architecture**, the guarantees inherited from earlier phases, the failure and durability boundaries, the measurement model, and the work that is intentionally deferred.

**Maintenance rule:** coding agents (including Codex) must **read this file before architecture-affecting work but must not edit it unless the project owner explicitly asks them to**. The project owner and ChatGPT maintain this document from committed repository state.

Architecture refreshes must not be performed as a narrow “append the latest commit” exercise. Before changing this file, the maintainer should:

1. read the whole current `ARCHITECTURE.md`,
2. inspect the current `master` implementation of the affected ownership paths,
3. compare the new code commit to the previous architecture baseline,
4. review earlier phase guarantees that the change depends on,
5. inspect focused tests as executable architecture contracts,
6. remove or correct stale statements instead of leaving contradictory history,
7. verify task-router, failure-domain, observability, durability and deferred-work sections remain mutually consistent,
8. distinguish measured facts from estimates or future production assumptions.

The purpose is that a coding agent should be able to read this document from a fresh context and make architecture-compatible decisions without reconstructing the project from chat history.

Current code reference commit:

```text
022d2fd5a3a6cef4ece0ac1b7434b9b9493512a0
feat: add capacity benchmark harness
```

**Phase 16 is committed on `master` and is the current architecture/measurement baseline.** Phase 16 adds offline measurement infrastructure only; it does not change the production runtime topology.

---

# 1. System purpose and current architectural direction

The repository is evolving from a single-purpose fight detector into a centralized multi-camera security platform in which Fight Detection and Speed Detection share infrastructure while preserving their own camera-local temporal state.

The production direction is:

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

The design is deliberately **not** “one complete AI pipeline per camera”. Expensive/stateless model inference is shared; state that has camera-specific temporal meaning remains camera-local.

The target scale is large multi-camera deployment, but **no production camera count is asserted by this contract**. Capacity is now measured through the Phase-16 benchmark system and must be validated on the actual production hardware and live workload.

---

# 2. Non-negotiable architecture invariants

These rules are binding unless a future explicitly-scoped phase changes them and this document is subsequently refreshed from committed code.

1. Runtime workers must not import or depend on Django ORM.
2. Django/Gunicorn is the control/application plane, not the owner of AI child processes.
3. Runtime Supervisor owns the global AI runtime lifecycle in the production path.
4. `run_multiprocess` owns the multiprocessing topology below the Supervisor.
5. One physical camera has one intended `CameraIngest` source/decode owner in the Supervisor-managed production runtime.
6. Fight and Speed on the same physical camera share that ingest; they are capabilities of one camera entry, not duplicate source owners.
7. Expensive/stateless inference models are shared services, not model-per-camera instances.
8. Fight temporal tracking/pair/ROI/event state and Speed tracking/calibration/speed state remain camera-local where correctness depends on history.
9. Shared inference services are capability-aware and should run only while the desired camera set requires them.
10. Dynamic desired-state/capability changes should not require a global runtime restart solely because capabilities changed.
11. Camera work is protected by stable slot + generation identity. Speed adds a consumer epoch. Reincarnated shared services use service epochs where required.
12. Old/stale work must fail closed; it must not become a current incident after camera/service reconfiguration.
13. Live and file workloads intentionally have different backpressure/recovery semantics.
14. Correctness must not depend on OS `Queue.qsize()` or `Queue.empty()` observations. Parent-owned scheduler counters/acknowledgements may be correctness inputs when their semantics are controlled.
15. Queueing, telemetry, recovery retry and operational scans must remain bounded.
16. Windows `spawn` compatibility is a first-class runtime constraint.
17. Runtime incident truth crosses into Django through the durable incident outbox; runtime workers do not write Incident ORM rows.
18. Fight and Speed share the same Django Incident/routing domain.
19. Optional service absence is healthy when that service is not currently required.
20. Benchmarks must never be confused with production capacity claims: synthetic camera-equivalents are not real inference, and one GPU’s measurements are not extrapolated into another GPU’s camera count.
21. Benchmark code must not mutate production model thresholds, source ownership, queue semantics or recovery behavior merely to improve benchmark numbers.
22. PostgreSQL, Docker, Nginx, deployment/service packaging and UI redesign are frozen/deferred unless explicitly promoted into a future phase.
23. Shared-memory frame transport is deferred until measurement shows ordinary multiprocessing frame transport is a meaningful bottleneck.

---

# 3. Current top-level production flow

```text
Django / Web / DB
  |
  | Camera desired state + control requests
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
  |      +--> Fight bundle, only when required
  |      |      +--> shared Person worker
  |      |      +--> Person result router
  |      |      +--> shared Pose worker + router when enabled
  |      |      +--> shared Stage3/X3D worker when enabled
  |      |
  |      +--> Vehicle bundle, only when required
  |             +--> shared Vehicle worker/model
  |
  +--> CameraRuntimeManager
         |
         +--> CameraIngest(camera N)
         |      |
         |      +--> Fight frame channel     optional
         |      +--> Speed frame channel     optional
         |      +--> Preview frame channel
         |
         +--> camera_worker(camera N)        Fight capability only
         +--> speed_worker(camera N)         Speed capability only
         +--> camera_preview(camera N)
```

Fight incident path:

```text
CameraIngest
  -> camera_worker
  -> shared Person
  -> camera-local person/pair/ROI/temporal logic
  -> shared Pose when required by runtime/config/state
  -> shared Stage3/X3D candidate inference
  -> Incident worker
  -> IncidentAggregator
  -> evidence + durable outbox
  -> Django Incident Dispatcher
  -> Incident(type=FIGHT)
  -> routing / ACK / escalation / resolve
```

Speed incident path:

```text
CameraIngest
  -> speed_worker
  -> camera-local motion/tracking/calibration state
  -> shared Vehicle inference
  -> camera-local speed/violation decision
  -> evidence + durable outbox
  -> Django Incident Dispatcher
  -> Incident(type=SPEED)
  -> routing / ACK / escalation / resolve
```

There is no parallel Speed incident database or separate Speed dispatcher in the production design.

---

# 4. Ownership planes

## 4.1 Django / application plane owns

- `streams.Camera` and camera configuration persisted in the application domain,
- `SpeedCameraConfig`,
- physical `Location` hierarchy,
- security units, coverage and user assignments,
- Incident ORM rows and audit/routing state,
- desired-camera publication,
- start/stop requests through the Runtime Supervisor client,
- operator-facing status/preview/action endpoints,
- ORM-aware retention decisions and evidence-reference protection,
- the independent Incident Dispatcher service loop.

Django may **observe** runtime state and consume runtime-produced preview/evidence. It must not become a hidden second AI runtime owner.

## 4.2 Django must not own

- shared Person/Pose/Stage3/Vehicle model processes,
- per-camera runtime worker processes,
- production RTSP reconnect loops,
- a second source connection while the common runtime owns the camera,
- runtime-local temporal truth,
- direct runtime Incident creation through ORM.

## 4.3 Runtime Supervisor owns

Primary implementation:

```text
fight/runtime_supervisor/core.py::RuntimeSupervisor
```

It owns exactly one common `fight.pipeline_mp.run_multiprocess` parent in normal Supervisor mode.

Supervisor states:

```text
STOPPED
STARTING
RUNNING
STOPPING
FAILED
BACKOFF
```

Supervisor state includes run/config identity, runtime PID, restart information, desired-camera metadata, disk state and health projection. Local Supervisor state under `.runtime_supervisor/` is operational data, not repository source.

## 4.4 Runtime parent owns

Primary implementation:

```text
fight/pipeline_mp/run_multiprocess.py
```

The dynamic path owns:

- `multiprocessing.get_context("spawn")`,
- slot-generation and Speed-epoch shared arrays,
- Fight publication floor,
- Incident and Reporter workers,
- `SharedServices`,
- `CameraRuntimeManager`,
- capability/service reconciliation,
- desired-state polling,
- health registry/watchdog/snapshot,
- fair admission queues,
- file EOF/drain/finalization,
- global exit semantics.

The parent is orchestration/ownership authority; it does not move camera-specific temporal state into a shared global tracker.

---

# 5. Desired camera state contract

Primary files:

```text
fight/runtime_supervisor/camera_state.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
```

Desired state schema is currently version `1`.

Canonical camera fields include:

```text
camera_id
source
name
enabled
use_fight_detection
use_speed_detection
speed_config
```

A camera is active for the common runtime when:

```text
enabled == true
AND
(use_fight_detection == true OR use_speed_detection == true)
```

Supported capability combinations:

```text
Fight-only
Speed-only
Fight + Speed
neither -> not started
```

Desired state has a monotonic revision. Runtime applies newer revisions in the existing parent; stale/equal revisions must not create duplicate lifecycle work.

`speed_paused` is durable desired intent. Stopping Speed is not equivalent to killing the common Fight runtime.

The registry publishes a Speed branch only when the application-side Speed conditions are satisfied, including active camera and enabled Speed configuration.

Speed runtime configuration sent through desired state is bounded to required fields such as:

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

Calibration revision is used so calibration changes can trigger the necessary camera-local reconfiguration.

Enabled camera entries must not claim the same physical source independently. If Fight and Speed use one physical camera, one desired camera entry carries both flags.

Current desired-state validation exposes `MAX_CAMERAS = 512`. **This is a schema/registry bound, not evidence that 512 real cameras can be processed by one machine.** Runtime slot provisioning and actual inference/decode capacity remain separate concerns and must be measured.

---

# 6. CameraIngest and physical source ownership

Primary implementation:

```text
fight/pipeline_mp/camera_ingest.py
```

Production source topology:

```text
                     +--> Fight consumer
Physical source ---> CameraIngest
                     +--> Speed consumer
                     +--> Preview
```

`camera_worker` and `speed_worker` must not independently reopen the centralized production source. RTSP/live reconnect ownership belongs to `CameraIngest`.

File and live publishing intentionally differ:

- file Fight delivery is ordered/bounded and waits/defer rather than silently discarding required work,
- live freshness can supersede old work,
- preview is a bounded latest-view concern,
- live reconnect uses bounded/exponential behavior owned by ingest.

## Django browser preview ownership

Primary common-runtime preview files include:

```text
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/common_preview.py
Fight_backend_project/backend_frontend_project/speed_detection/views.py
```

The Speed dashboard common-runtime stream consumes atomically replaced JPEG preview output from the active runtime. It does not open `camera.source` with a second `cv2.VideoCapture` while Supervisor/common-runtime ownership is active.

The preview helper validates active-run ownership/path boundaries, pins the stream to the active run, bounds JPEG reads, and fails closed when runtime ownership disappears or changes.

A direct-source calibration/legacy fallback is only acceptable in an explicitly non-Supervisor, cleanly stopped legacy path. It is not part of the production ownership model.

---

# 7. Dynamic camera lifecycle

Primary implementation:

```text
fight/pipeline_mp/camera_lifecycle.py::CameraRuntimeManager
```

Per-camera runtime state includes the desired camera definition plus stable slot/generation, process stop controls, Fight/Preview/Speed channels, Speed epoch/recovery state, Fight-service waiting state, lifecycle/file completion and restart counters.

Normal process composition:

```text
Fight-only:
  CameraIngest + camera_worker + preview

Speed-only:
  CameraIngest + speed_worker + preview

Fight + Speed:
  CameraIngest + camera_worker + speed_worker + preview
```

A camera-local restart may be required by source changes, capability flags or serialized Speed configuration changes. Cosmetic metadata should not restart inference ownership unnecessarily.

Stable slot reservation exists so result-channel identity does not depend on a process PID. Camera removal invalidates its generation before teardown; re-add/restart advances generation.

During shared-service recovery the manager can temporarily withdraw affected consumers instead of globally restarting all cameras.

- Vehicle recovery withdraws affected Speed consumers while Fight/ingest/preview can remain intact where safe.
- Fight-bundle recovery withdraws affected Fight camera runtimes before Fight transport replacement. For a mixed Fight+Speed camera this currently means a camera-local restart and therefore a brief local Speed interruption; Speed-only cameras remain untouched.

This mixed-camera interruption is a known optimization opportunity, not a correctness failure.

---

# 8. Identity and stale-work fencing

## 8.1 Camera identity

The base identity is:

```text
(camera_id, slot_id, generation)
```

Rules:

- slots are stable reservations owned by the parent,
- restart/removal invalidates old generation,
- delayed Person/Pose/etc. results from old generation are rejected,
- camera health events are generation-aware,
- scheduler state is scoped to the slot/generation semantics.

## 8.2 Speed consumer epoch

Speed adds:

```text
consumer_epoch
```

Stopping/replacing a Speed consumer increments the slot’s Speed epoch. Vehicle work carries camera generation + Speed consumer epoch. Durable Speed publication is guarded so a delayed old Speed result cannot create a current violation after restart/reconfiguration.

## 8.3 Shared service epoch

Reincarnated shared services add:

```text
service_epoch
```

`SharedServices` tags bounded health events with the service incarnation. Old service-health events are ignored after replacement.

Vehicle recovery replaces its request/result transport instead of reusing queues/locks owned by a failed incarnation.

Fight recovery likewise replaces the Fight bundle transport. Person/Pose result channels and admissions are recreated; camera generations are invalidated before withdrawal.

## 8.4 Fight durable-publication fence

Fight recovery has an additional publication fence:

```text
fight_publication_floor
```

Stage3 results carry `service_epoch` into the Incident worker/Aggregator path. On confirmed Fight-service failure, the parent raises the publication floor **before** touching the failed transport. Buffered segments/results from an older Fight service incarnation cannot later finalize into a durable current incident.

The aggregator checks the publication floor both when accepting state and immediately around durable publication. This protects against invalidation during expensive evidence finalization.

Do not replace this identity/fencing model with PID-only checks, fork-only mutable state, unbounded Manager objects, or queues passed through queues.

---

# 9. Shared inference services and capability lifecycle

Primary implementation:

```text
fight/pipeline_mp/shared_services.py::SharedServices
```

Required capabilities are derived from the current enabled desired camera set:

```text
fight   = any active camera with use_fight_detection
vehicle = any active camera with use_speed_detection
```

Service groups:

```text
Fight bundle:
  person
  person_router
  pose + pose_router       if runtime.use_pose
  stage3                   if runtime.use_stage3

Vehicle bundle:
  vehicle
```

Runtime-global `incident` and `reporter` are outside these capability bundles.

Expected states:

```text
Fight-only desired set
  -> Fight bundle running
  -> Vehicle disabled/absent

Speed-only desired set
  -> Vehicle running
  -> Fight inference bundle disabled/absent

Fight + Speed desired set
  -> both required bundles running
```

Transitions happen inside the same runtime parent. First capability demand can hot-start its bundle; last demand removal can stop that bundle without restarting unrelated capabilities.

Unused bundles have a bounded idle grace. Current default:

```text
SHARED_SERVICE_IDLE_GRACE_SEC=5
```

Fight shutdown additionally drains acknowledged/owned admission work before stopping so accepted ordered work is not abandoned merely because the last Fight camera disappeared.

Configured-off Pose/Stage3 components are expected absence, not health failure.

---

# 10. Fight inference ownership

## Person

```text
camera_worker[N]
  -> fair per-slot Person admission
  -> one shared Person inference worker/model
  -> Person result router
  -> per-slot result channel
  -> camera_worker[N]
```

Person detection is shared across cameras; downstream identity/temporal interpretation remains camera-local.

## Pose

```text
camera_worker[N]
  -> selected ROI/candidate
  -> fair per-slot Pose admission
  -> one shared Pose worker/model
  -> Pose router
  -> per-slot result channel
  -> camera-local PoseGate/history
```

Pose temporal history is not globalized across cameras.

## Stage3 / X3D

```text
camera_worker[N]
  -> bounded Stage3 candidate admission
  -> one shared Stage3/X3D worker
  -> Incident worker
  -> IncidentAggregator
```

IncidentAggregator must not instantiate a second local Person/Pose model. Shared Stage3 output is incarnation-tagged for Fight recovery publication fencing.

The production Fight decision thresholds/model configuration are not changed by Phase 16 measurement code.

---

# 11. Speed inference ownership

Primary runtime worker:

```text
fight/pipeline_mp/speed_worker.py
```

`speed_worker` owns camera-local state such as:

- ROI and calibration,
- motion gating,
- tracker state,
- speed estimator state,
- violation decision/cooldown,
- evidence buffer/writer.

It does **not** own its own YOLO Vehicle model.

Vehicle topology:

```text
speed_worker[N]
  -> bounded/fair per-slot Vehicle admission
  -> shared vehicle_service_main
  -> one lazily created VehicleDetector/model
  -> per-slot VehicleResult channel
  -> speed_worker[N]
```

Vehicle result payloads do not send full frame pixels back through the result queue.

Vehicle inference/model errors are service failures, not “no detections”.

Normal Django Speed control uses the common Runtime Supervisor. Older standalone Speed multiprocess helpers may remain for compatibility/history but are not the production ownership reference.

---

# 12. Shared-service recovery and failure isolation

## 12.1 Vehicle recovery

Vehicle is optional to Fight and has bounded in-runtime recovery.

Confirmed Vehicle failure sequence:

```text
failure detected
  -> mark Vehicle unavailable
  -> withdraw affected Speed consumers
  -> invalidate Speed consumer epochs
  -> stop/close failed Vehicle bundle
  -> replace Vehicle request/result transport
  -> increment Vehicle service epoch
  -> bounded exponential-backoff replacement
  -> resume eligible LIVE Speed consumers
```

Fight processing and runtime-global incident infrastructure remain available when possible.

Defaults:

```text
VEHICLE_SERVICE_RESTART_LIMIT=3
VEHICLE_SERVICE_RESTART_BACKOFF_SEC=2
VEHICLE_SERVICE_RESTART_MAX_BACKOFF_SEC=30
```

Replacement-start failures consume the bounded service retry budget. Recovery exhaustion leaves Vehicle failed/degraded and requires operator intervention; it must not create an unbounded restart storm or force an otherwise healthy Fight bundle down solely because Speed’s optional Vehicle service is exhausted.

File Speed consumers are not replayed after partial shared-service failure. Clean completed file work stays completed; partial/failed work remains incomplete/fail-closed.

## 12.2 Fight bundle recovery

Recoverable shared Fight components are:

```text
person
person_router
pose
pose_router
stage3
```

Confirmed failure of one required/running component recovers the **whole Fight inference bundle**, not just one process, because the transport graph and result ownership form one incarnation boundary.

Sequence:

```text
Fight shared failure
  -> raise fight_publication_floor
  -> invalidate affected camera generations
  -> mark affected Fight cameras waiting/reconnecting
  -> withdraw affected camera runtimes/readers
  -> close old Fight queues/results/admissions
  -> stop old Fight bundle
  -> bounded backoff
  -> create fresh Fight transport/bundle with new service_epoch
  -> restart/resume eligible LIVE affected cameras
```

Speed-only camera runtimes and the shared Vehicle process preserve identity through Fight recovery. A mixed Fight+Speed camera currently restarts locally as the safe ownership unit, so its local Speed consumer can be interrupted briefly.

Defaults:

```text
FIGHT_SERVICE_RESTART_LIMIT=3
FIGHT_SERVICE_RESTART_BACKOFF_SEC=2
FIGHT_SERVICE_RESTART_MAX_BACKOFF_SEC=30
```

The retry budget is bounded over the runtime lifetime and includes replacement-start failures.

Fight recovery is deliberately stricter for unsafe states:

- a Fight file source affected by a shared Fight failure is `fight_file_incomplete` and is not replayed,
- unsafe camera withdrawal/teardown is fatal rather than risking duplicate source owners,
- recovery retry exhaustion is global fatal,
- initial Fight bundle startup failure is global fatal,
- Incident and Reporter are not part of Fight bundle hot recovery and retain runtime-global failure semantics.

## 12.3 Runtime-global failures

`incident` and `reporter` are runtime-global. `run_multiprocess` explicitly checks their process liveness. Their confirmed death remains a global runtime stop/failure boundary rather than being silently reconstructed as a Fight-only optional service.

---

# 13. Speed Django bridge

Primary compatibility/control facade:

```text
Fight_backend_project/backend_frontend_project/services/speed_bridge/speed_runner.py
```

Normal common-runtime behavior:

```text
start_speed_pipeline
  -> require/use Supervisor-managed common runtime
  -> clear speed_paused
  -> reconcile desired cameras
  -> start or reuse common runtime

stop_speed_pipeline
  -> persist speed_paused=true
  -> remove/disable Speed demand
  -> Fight remains independently available
```

`CommonRuntimeProcess` is a read-only compatibility facade over common runtime status; it must not become a second `Popen` runtime owner.

---

# 14. Fair scheduling and bounded capacity

Primary implementation:

```text
fight/pipeline_mp/scheduling.py::FairRequestQueue
```

Conceptual shape:

```text
slot 0 -> bounded pending work
slot 1 -> bounded pending work
slot 2 -> bounded pending work
...
        -> round-robin shared consumer
```

Fair/shared stages currently include:

```text
Person
Pose
Stage3
Vehicle
```

Capacity/accounting fields include:

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

The scheduler prevents one hot camera from monopolizing another slot’s reserved pending capacity. Fight and Speed use separate stage admissions rather than one undifferentiated FIFO.

For live workloads, explicit shedding can be correct; for ordered files, capacity pressure should defer/wait without silently converting missing work into a negative detection.

`qsize()`/`empty()` on OS queues are telemetry/convenience observations only unless the queue is a custom parent-owned structure with explicit owned semantics. Correctness should use owned counters/acks/generation state.

---

# 15. Live vs file semantics

## LIVE / RTSP

Priority:

```text
freshness > completeness
```

Expected behavior:

- bounded queues,
- stale live frame/inference work may be explicitly shed/replaced,
- dropped/stale work is not interpreted as a clean negative detection,
- CameraIngest owns reconnect,
- camera-local retries are bounded,
- eligible live Speed consumers can resume after Vehicle recovery,
- eligible live Fight cameras can resume after Fight bundle recovery,
- unrelated cameras/services should survive local/recoverable failures.

## FILE

Priority:

```text
correctness + ordering > freshness
```

Expected behavior:

- ordered work waits/defer rather than silently dropping required inference,
- admitted Stage3 work drains before final completion,
- Speed file delivery/EOF drains before clean completion,
- normal non-looping EOF is not a failure,
- partially failed Speed files are not replayed and called clean,
- a Fight file touched by shared Fight service failure is explicitly incomplete/fatal for that run,
- timeout/deadline truncation in benchmark mode is `INCOMPLETE`, not successful throughput.

---

# 16. Health architecture

Primary files:

```text
fight/pipeline_mp/health.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/core.py
```

Flow:

```text
child workers
  -> one bounded HealthEvent queue
  -> runtime parent
  -> HealthRegistry
  -> RuntimeWatchdog
  -> atomic runtime_health.json
  -> Supervisor /runtime/health projection
```

Health/liveness decisions use monotonic time where appropriate.

Camera components include:

```text
camera_ingest
camera_worker
camera_preview
speed_worker
```

Shared health records include:

```text
person
person_router
pose
pose_router
stage3
incident
vehicle
```

Reporter is runtime-global process-liveness critical but is not required to appear as a normal inference worker record in the health snapshot.

Capability-managed worker metadata includes:

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

- `required=false + disabled` => healthy `service_disabled`,
- required/restarting => degraded rather than falsely healthy,
- a recoverable Fight service fault is surfaced as degraded while the parent-owned bundle recovery proceeds; exhaustion becomes fatal,
- Vehicle recovery/exhaustion is isolated from Fight-wide critical failure as described above,
- configured-off optional Pose/Stage3 absence is expected,
- queue pressure can degrade health without causing uncontrolled restart storms,
- disk pressure can degrade health but is not itself a reason to thrash the runtime,
- stale service-epoch health events cannot overwrite the current incarnation.

Current health snapshots are state snapshots, not a complete historical monitoring database.

---

# 17. Incident durability boundary

Primary files:

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/speed_worker.py
```

Runtime produces evidence and durable incident envelopes before Django ingestion.

Core durability semantics:

- append-only outbox JSONL,
- serialized writers,
- flush/fsync before legacy success publication,
- partial-tail preservation,
- explicit failure on persistence errors,
- evidence durability before durable incident publication,
- stale generation/epoch guards before current event publication.

Fight adds the service-incarnation publication floor described earlier. Speed durable publication uses camera generation + Speed consumer epoch protection.

Runtime does not directly create Django ORM Incident rows.

---

# 18. Django incident/application domain

Primary models include:

```text
incidents.models.Incident
incidents.models.IncidentRoutingRule
incidents.models.IncidentRoute
incidents.models.IncidentAuditEvent
incidents.models.IncidentIngestCursor
incidents.models.IncidentIngestRecord
```

Incident types include:

```text
FIGHT
SPEED
OTHER
```

Application flow:

```text
durable runtime outbox
  -> run_incident_dispatcher
  -> incidents.services.ingest
  -> Incident ORM row
  -> location/security routing
  -> ACK / resolve / escalation / audit
```

Dispatcher consumption is a separate application service responsibility from global AI runtime ownership.

Camera/evidence deletion must respect Incident references and retention protections.

---

# 19. Location and authorization model

Primary application entities:

```text
adminx.Location
adminx.SecurityUnit
adminx.SecurityUnitCoverage
adminx.UserSecurityAssignment
streams.Camera
```

Conceptual authorization path:

```text
User
  -> SecurityUnit assignment
  -> SecurityUnitCoverage
  -> Location hierarchy
  -> Camera.location
  -> incident/preview/action visibility
```

Fight and Speed share the same physical authorization/location domain.

Legacy `Camera.faculty` may exist for compatibility, but physical authorization should use `Camera.location` where assigned.

Organizational access decisions belong in the Django/application layer, not in model workers.

---

# 20. Runtime durability, retention and operational state

Phase-12 durability remains binding.

Primary areas include:

```text
fight/operations.py
fight/retention.py
fight/service_loop.py
incidents/services/retention.py
fight/runtime_supervisor/core.py
```

Rules:

- Supervisor/desired state is written durably/atomically where designed,
- outbox/evidence durability is explicit,
- cleanup scans are bounded,
- current/active/unknown/abnormal runs fail closed against unsafe cleanup,
- Incident-referenced evidence is protected,
- evidence retention is indefinite by default unless explicitly configured,
- optional evidence deletion requires the intended durable/outbox safety conditions,
- disk pressure is observable and can degrade health without causing restart storms,
- singleton service loops/locks protect long-running operational jobs.

Generated runtime/benchmark content is not source code and must not be casually committed.

Known local operational paths include:

```text
.runtime_supervisor/
Fight_backend_project/backend_frontend_project/media/camera_uploads/
Fight_backend_project/backend_frontend_project/media/pipeline_runs/.run_locks/
Fight_backend_project/backend_frontend_project/media/runtime_spool/
benchmarks/results/
benchmarks/.capacity.lock
```

`benchmarks/results/` can contain copied/hard-linked media, isolated Supervisor state, runtime logs, effective configs, evidence and outbox data. Treat it as local measurement output, not repository source.

---

# 21. Phase-16 capacity benchmark subsystem

Phase 16 introduces an **offline measurement subsystem**, not another production runtime.

Primary files:

```text
benchmarks/__main__.py
benchmarks/control_plane.py
benchmarks/real_inference.py
benchmarks/telemetry.py
benchmarks/README.md
tests/test_capacity_benchmarks.py
```

The package is deliberately separated from production imports. `benchmarks/__init__.py` identifies it as offline capacity measurement.

## 21.1 Measurement taxonomy

There are two distinct benchmark classes and their results must never be merged conceptually:

```text
REAL INFERENCE
  = real Supervisor/runtime
  + real local file decode
  + real shared models/workers
  + real queue/recovery/health behavior

CONTROL PLANE / SYNTHETIC
  = real parent-side state/lifecycle/scheduling structures
  + inert/fake process/transport execution
  - no model inference
  - no real decode
```

A result such as “300 camera-equivalents passed synthetic control-plane checks” **does not mean 300 cameras can be inferred in production**.

## 21.2 CLI contract

Entry point:

```text
python -m benchmarks --help
```

Examples:

```powershell
python -m benchmarks control_plane --counts 50 100 200 300 --workload mixed --warmup-sec 1 --duration-sec 5 --seed 17

python -m benchmarks real_inference --counts 1 2 --workload fight --config <effective-config.json> --source fight/sample_2.mp4 --warmup-sec 5 --duration-sec 180
```

Supported real workload labels:

```text
fight
speed
mixed
```

`mixed` enables both capabilities on each benchmark camera and therefore exercises the single CameraIngest fan-out model.

Counts must be distinct positive integers within the existing registry validation bound. The harness has no smaller hardcoded “capacity ceiling”; operators increase real counts manually while preserving machine safety.

The CLI exposes bounded warmup/measurement/sample controls, deterministic seed and optional NVIDIA device selection.

## 21.3 Real-inference isolation

Real mode uses the common `RuntimeSupervisor` and `run_multiprocess`; it does not implement a fake performance pipeline.

Important isolation rules:

- requires an existing local effective/production-like config,
- preserves detection/model thresholds and queue/recovery semantics,
- forces local benchmark output/state paths,
- requires ordered non-looping local file behavior,
- uses `auto_restart=False` for isolated benchmark Supervisor behavior,
- refuses to run if another common runtime is already active,
- uses a benchmark singleton lock to prevent duplicate benchmark harnesses,
- routes benchmark incident output into an isolated benchmark outbox rather than Django dispatcher/Incident DB,
- rejects remote/credential-bearing benchmark inputs,
- does not automatically download missing model weights.

When several logical benchmark cameras reuse one local media file, the harness creates distinct hardlinks with copy fallback so desired-state source uniqueness remains valid. This is an **ordered shared-content file benchmark technique**, not a claim that separate physical RTSP cameras behave identically.

## 21.4 Synthetic/control-plane scope

Control-plane mode exercises production parent-side structures including:

```text
DesiredCameraState validation
CameraRuntimeManager
slot/generation handling
HealthRegistry / snapshot serialization
FairRequestQueue for Person/Pose/Stage3/Vehicle
capability transition/reconcile logic
```

It uses inert process objects/in-memory bounded transport rather than spawning hundreds of model workers.

The scenario checks:

- initial reconcile,
- deterministic remove/re-add subset,
- stable unique slots,
- generation advancement,
- stale request fencing,
- unchanged-camera identity preservation,
- capability transition scope,
- idempotent reconcile,
- health evaluation/snapshot serialization,
- round-robin scheduler fairness,
- full-slot rejection/capacity accounting,
- bounded retained telemetry.

Synthetic timings intentionally exclude real process spawn, CUDA/model inference, image decode, RTSP pacing/loss and multiprocessing frame-copy cost.

## 21.5 Telemetry and output

Real benchmark telemetry can collect:

```text
host CPU utilization
runtime process-tree RSS
harness RSS
host RAM
NVIDIA GPU utilization when available
NVIDIA VRAM used/total
camera decoded/consumed progress
Fight/Speed drops/reconnect/final health
Person/Pose/Stage3/Vehicle capacity counters
existing bounded latency summaries
```

NVIDIA telemetry is optional and uses bounded `nvidia-smi` subprocess calls. Missing/failing GPU telemetry must become unavailable data, not crash the benchmark. Explicit device selection is supported; GPU utilization alone never proves which GPU/model path is the bottleneck.

Sampling tails are bounded in memory. Current default maximum retained samples is `256`; streamed CSV can contain the observation sequence without keeping it all in RAM.

Generated result directory contains, at minimum:

```text
benchmark_summary.json
runs.jsonl
system_samples.csv
```

Real runs additionally contain isolated Supervisor/runtime state, logs/config/report artifacts and source links/copies.

Machine-readable summary keeps:

```text
real_inference: [...]
control_plane: [...]
```

as separate arrays.

Exported summaries redact sensitive path/URL/credential information and identify local files/models by hashes/metadata where possible. Local effective configs inside the full result directory can still contain local paths, so the entire directory must be reviewed before external sharing.

## 21.6 Unavailable metrics are explicit

Phase 16 deliberately does not fabricate metrics it cannot derive reliably. Current known unavailable/limited areas include:

```text
Vehicle latency summary when no existing production metric exists
reliable end-to-end incident latency
steady-window camera FPS
Speed completed-frame count distinct from sequence/progress semantics
some Person/Pose raw inference_ms distributions depending on existing instrumentation
```

Zero samples means unavailable/no samples; it must not be interpreted as zero latency.

## 21.7 Classification contract

Real benchmark classifications are measurement labels, not product guarantees:

```text
HEALTHY
PRESSURED
SATURATED
INCOMPLETE
```

Current default concepts:

- `INCOMPLETE`: deadline/nonzero exit/missing required reports or samples/failed required consumer/observed recovery/failed runtime health/no frames,
- `SATURATED`: significant live shedding, or heavy rejection together with actual live shedding,
- `PRESSURED`: lower live shedding, significant rejection attempts or sampled queue occupancy near configured capacity,
- `HEALTHY`: completed without those observed pressure criteria.

Default thresholds implemented by the harness include approximately:

```text
pressure live-drop ratio      0.01
saturation live-drop ratio    0.10
pressure rejection ratio      0.10
saturation rejection ratio    0.50
queue pressure ratio          0.90
```

Ordered file retries/defer counters must not be mistaken for dropped live frames. High GPU utilization alone cannot set `SATURATED`.

Diagnostic hints such as `gpu_bound_candidate`, `cpu_bound_candidate`, `queue_pressure` and `unknown` are hypotheses based on evidence, not proven root-cause conclusions.

---

# 22. Phase-16 measured baseline and interpretation

## 22.1 Synthetic/control-plane acceptance

The accepted Phase-16 synthetic mixed scenarios were run at:

```text
50
100
200
300
```

Observed representative results from the implementation acceptance:

```text
cameras   initial reconcile   health/snapshot p95   approx RSS delta
50        6.31 ms             3.21 ms               3.37 MiB
100       20.71 ms            6.04 ms               4.06 MiB
200       15.06 ms            11.13 ms              7.90 MiB
300       20.35 ms            8.23 ms               12.05 MiB
```

All synthetic slot/generation/bounded-storage/scheduler-fairness checks passed in that run.

These values show that the **parent-side synthetic structures did not expose an immediate 300-entry control-plane wall in that test**. They do not include decode, CUDA inference, OS multiprocessing frame copy, RTSP behavior or real 300-camera process load, so they are not a production capacity claim.

## 22.2 RTX 3050 one-camera real Fight smoke

A post-implementation accepted real Fight smoke ran successfully on the development GPU:

```text
GPU: NVIDIA GeForce RTX 3050 6GB Laptop GPU
camera_count: 1
workload: fight
runtime_exit_code: 0
runtime_health: HEALTHY
classification: HEALTHY
frames decoded: 903
aggregate full-run effective FPS: ~14.76
Person: 417 accepted / 417 dispatched / 417 results
Pose: 311 accepted / 311 dispatched / 311 results
Stage3: 6 accepted / 6 dispatched / 6 results
live admission drop ratio: 0
rejection-attempt ratio: 0
observed queue ratio peak: 0.03125
```

Representative host/GPU observations for this smoke:

```text
CPU mean ~12.84%, p95 ~19.75%, max ~32.4%
GPU utilization mean ~13.02%, p95 ~42.5%, max ~67%
GPU VRAM mean ~487.9 MiB, p95/max ~640 MiB
runtime process-tree RSS mean ~3.79 GB, max ~4.98 GB
```

Representative existing latency summaries:

```text
Person queue wait p95 ~1.52 ms
Person round trip p95 ~28.80 ms
Pose queue wait p95 ~1.29 ms
Pose round trip p95 ~33.80 ms
Stage3 queue wait p95 ~160.27 ms
Stage3 inference p95 ~624.04 ms, max ~808.67 ms
```

The reported full-run FPS includes startup/drain/EOF semantics and is **not steady-state live FPS**. A per-camera internal processing-rate field can have different semantics and must not be compared as if it were the same denominator.

This run was a harness acceptance smoke, not a saturation search. Low average GPU usage does not establish unused production capacity by itself because workload gating/stage demand, file pacing and model activation affect utilization.

## 22.3 Two-camera real Fight smoke

A second post-implementation smoke was run with two logical Fight cameras and completed:

```text
classification: HEALTHY
```

No detailed two-camera summary has been promoted into this architecture contract, so no specific two-camera FPS/latency/VRAM values should be invented or inferred here.

## 22.4 Native OpenMP environment observation

The first real smoke attempt failed with Intel OpenMP Error #15 (`libiomp5md.dll already initialized`) and was correctly classified `INCOMPLETE`. The harness did **not** set the unsafe `KMP_DUPLICATE_LIB_OK=TRUE` workaround.

Environment diagnosis found two filesystem locations for `libiomp5md.dll` (environment and base Anaconda paths), with identical observed SHA-256 content. Ordinary imports and CUDA allocation succeeded. After starting from a clean conda activation (`CONDA_SHLVL=1`) and running through `conda run -n torch_gpu --no-capture-output`, one-camera and two-camera Fight smokes completed `HEALTHY`.

**Root cause is not proven.** Do not encode “nested conda definitely caused the failure” as architecture truth. The reliable operational lesson from current evidence is to benchmark from a clean inference environment and not hide native runtime conflicts with unsafe OpenMP overrides.

## 22.5 What Phase 16 does NOT prove

It does not prove:

- production capacity on an RTX 5090,
- 200–300 simultaneous real RTSP cameras,
- sustained live-stream steady-state FPS,
- full mixed Fight+Speed production saturation behavior,
- network/jitter/reconnect behavior at campus scale,
- whether NumPy frame IPC is or is not the next real bottleneck,
- multi-GPU scaling.

The benchmark harness now makes those questions measurable; it does not answer them in advance.

---

# 23. Historical performance observability context

Earlier performance work introduced bounded timing/status instrumentation and established a historical development-machine baseline before later architecture phases.

A previously recorded dense-file RTX 3050 characterization produced approximate aggregate rates:

```text
1 camera  ~21.29 FPS
2 cameras ~41.56 FPS
4 cameras ~59.58 FPS
8 cameras ~68.73 FPS
```

That older experiment suggested queue latency began becoming important around several dense cameras. **These numbers are historical context only.** Runtime architecture, test workload, instrumentation and later phases have changed, so they must not be treated as the current Phase-16 capacity baseline or compared blindly to the new ordered-file benchmark.

The correct process now is to use the Phase-16 harness with comparable configs/media/hardware and preserve raw result metadata.

Shared-memory transport remains deferred until current measurement demonstrates IPC/frame-copy pressure as a meaningful limiting factor.

---

# 24. Production runtime vs legacy paths

This architecture contract describes the **Supervisor-managed dynamic runtime**.

Legacy/static/compatibility code may remain, including old direct-control or standalone Speed helpers. Presence of legacy files does not make them the current ownership model.

Likewise, repository deployment files such as Docker/Nginx-related artifacts may exist historically. Their presence does not mean deployment architecture is currently being redesigned or accepted as production scope.

When inspecting a legacy helper, agents must ask whether that path is reachable in the Supervisor-managed production flow before using it to infer ownership.

Normal architecture work should improve the current dynamic path without gratuitously rewriting stable legacy code, unless a legacy path violates active production ownership or creates a user-reachable correctness/security problem.

---

# 25. Phase 1–12 foundation ledger

The current architecture depends on foundations built before shared Speed integration. These guarantees remain relevant even though the exact code has evolved.

## Phase 1 — Shared Person inference

Established one shared Person inference service/model rather than one detector per camera, with camera-local Motion/stabilization/tracking/pair/ROI/event/prebuffer state and explicit request identity.

## Phase 2 — Shared Pose inference

Moved Pose inference into a shared service/router while keeping Pose temporal interpretation camera-local. Later stabilization removed accidental secondary model ownership from incident aggregation.

## Phase 3 — Performance observability

Added bounded performance timing/queue/inference/delivery telemetry and machine-readable summaries. This was the first evidence-led basis for discussing scale and later informed fair scheduling and the decision to defer shared-memory optimization.

## Phase 4 — Microbatching

Added latency-bounded FIFO microbatch capability for shared inference stages while preserving conservative defaults (batching effectively off/size 1 unless configured).

## Phase 5 — Centralized CameraIngest

Established one source/decode owner feeding Fight and Preview, later extended to Speed. File ordered delivery and live latest-frame behavior became explicit architectural policies.

## Phase 6 — Runtime Supervisor

Moved global runtime ownership out of request-serving Django processes into a standalone authenticated local Supervisor with durable state, PID/config tracking and Windows-aware stop handling.

## Phase 7 — Physical organization/access scope

Added Location/SecurityUnit/Coverage/UserAssignment and `Camera.location`, providing a common physical authorization model for camera/incident visibility.

## Phase 8 — Durable incident/routing boundary

Established runtime evidence + durable outbox -> independent Django Incident Dispatcher -> common Incident/routing/ACK/resolve/escalation domain. Runtime remained ORM-free.

## Stabilization after Phase 8

Removed duplicate/local incident inference ownership, hardened browser/media behavior, bounded file/report scans, corrected dispatcher cursor/partial-tail semantics, and strengthened clean EOF behavior.

## Phase 9 — Dynamic camera lifecycle

Added desired-state revisions, stable result slots, camera generations and in-parent camera add/remove/restart reconciliation without global runtime restart for ordinary camera changes.

## Phase 10 — Health/watchdog

Added bounded child health events, parent HealthRegistry/Watchdog, camera/worker state classification and atomic runtime health snapshots.

## Phase 11 — Fair scheduling/capacity

Added per-slot bounded admission and round-robin fairness. Live shedding and file defer semantics became explicit. Scheduler/accounting was designed for hundreds of slots without giving one camera an unbounded shared FIFO advantage.

## Phase 12 — Operational durability/retention

Added bounded cleanup/retention protections, runtime/run locks, disk-pressure health, serialized/fsynced durable writes and resilient singleton service-loop behavior.

These are not optional historical curiosities: later phases assume these ownership and durability boundaries.

---

# 26. Phase 13 — Shared Speed integration guarantees

Code baseline:

```text
418f65bf137cfb31aa92629ac8fe1e03a0a1c54a
feat: integrate speed detection into shared runtime
```

Phase 13 established:

```text
Fight-only / Speed-only / Fight+Speed camera modes
single CameraIngest fan-out for Fight/Speed/Preview
shared Vehicle inference model/service
camera-local Speed motion/tracking/calibration/speed state
bounded Vehicle admission
Speed generation + consumer-epoch stale-work rejection
file fail-closed / live recovery distinction
Speed durable outbox -> Incident(type=SPEED)
common Runtime Supervisor control/status for Speed
speed_paused desired intent
```

Reported validation baseline at that phase:

```text
135 passed, 1 skipped
```

Later phases supersede its lifecycle/recovery limitations but preserve its ownership model.

---

# 27. Phase 14 — Capability-aware lifecycle and Vehicle recovery guarantees

Code baseline:

```text
7b395ef94f04b435862ab013f9226b0982de34b6
feat: add capability-aware shared service lifecycle
```

Phase 14 established:

```text
SharedServices as parent capability lifecycle owner
Fight-only without Vehicle process
Speed-only without unnecessary Fight inference services
same-runtime hot start/stop of capability bundles
bounded idle grace
Fight drain before ordinary capability shutdown
Vehicle crash/stall/start-failure recovery
Vehicle transport replacement on recovery
bounded exponential Vehicle retry/backoff
Vehicle service epoch health isolation
eligible live Speed resume after recovery
file Speed no-replay after partial failure
optional-service-aware health
Speed dashboard common-preview source ownership
Supervisor-mode fail-closed duplicate-source protection
```

Reported validation baseline:

```text
148 passed, 1 skipped
```

Live acceptance verified same-runtime capability transitions and Vehicle disabled/running behavior. Phase 15 later generalized recovery for the Fight inference bundle.

---

# 28. Phase 15 — Resilient Fight service recovery guarantees

Code baseline:

```text
c3c019b2871dcd3891b24ef242a2c5e93fe9212f
feat: add resilient fight service recovery
```

Phase 15 established:

```text
bounded in-runtime recovery for Person/Person-router/Pose/Pose-router/Stage3
whole Fight bundle transport replacement
camera generation invalidation before withdrawal
Fight service epochs
Stage3 -> Incident service_epoch propagation
fight_publication_floor durable publication fence
LIVE affected Fight camera resume after replacement
FILE Fight fail-closed / no replay
bounded retry/backoff and replacement-start accounting
Fight exhaustion/unsafe teardown/global Incident-Reporter failures remain fatal
Vehicle and Speed-only identity preserved across Fight recovery
```

Focused tests include component-parametrized failure/recovery, old transport/result rejection, service-health epoch rejection, retry/start-failure exhaustion, file fail-closed behavior, publication invalidation during evidence finalization and Windows `spawn` recreation.

Reported validation baseline:

```text
162 passed, 1 skipped
```

Live acceptance also confirmed Fight-only real inference health and Speed-only convergence with Fight bundle disabled after grace while Vehicle/Speed remained healthy.

---

# 29. Phase 16 — Capacity benchmark harness guarantees

Code baseline:

```text
022d2fd5a3a6cef4ece0ac1b7434b9b9493512a0
feat: add capacity benchmark harness
```

Phase 16 changed only benchmark/test source files; production runtime code and `ARCHITECTURE.md` were intentionally unchanged in the code commit.

Added source files:

```text
benchmarks/.gitignore
benchmarks/README.md
benchmarks/__init__.py
benchmarks/__main__.py
benchmarks/control_plane.py
benchmarks/real_inference.py
benchmarks/telemetry.py
tests/test_capacity_benchmarks.py
```

Guarantees:

```text
real inference and synthetic control-plane results remain explicitly separate
real mode reuses the actual Supervisor/runtime rather than a fake inference loop
synthetic mode reuses real parent-side lifecycle/health/scheduler structures with inert processes
bounded host/GPU telemetry
optional nvidia-smi fallback
result/source/config redaction and hashing
isolated benchmark state/outbox/results
no concurrent production runtime or duplicate benchmark harness
HEALTHY/PRESSURED/SATURATED/INCOMPLETE classification
classification does not use GPU utilization alone
no production threshold/queue/recovery/source-ownership mutation
no automatic stress sweep
registry bound is not advertised as real capacity
generated benchmark outputs are git-ignored
```

Focused tests cover:

```text
CLI/config validation
distributions and bounded sampler
missing/optional GPU telemetry
real/synthetic output separation and redaction
classification semantics
real workload isolation/identity without threshold mutation
300-camera-equivalent lifecycle/generation/fairness/capacity checks
isolated Speed failure not being mislabeled as success
```

Reported automated validation after implementation:

```text
Phase-16 focused tests: 8 passed
full pytest: 170 passed, 1 skipped
compileall: passed
Django check: passed
makemigrations --check: passed
git diff --check: passed
UI/template/static: unchanged
production tracked code outside benchmarks/tests: unchanged
```

Post-implementation manual acceptance then produced successful one-camera and two-camera real Fight smokes from a clean inference environment. These smokes validate the harness path; they do not establish production saturation limits.

---

# 30. Task router for future coding agents

Use this section to avoid broad repository scans when a task has a clear ownership domain. Read this document first, then inspect the relevant current files before editing.

## Runtime Supervisor / desired state

```text
fight/runtime_supervisor/core.py
fight/runtime_supervisor/camera_state.py
fight/runtime_supervisor/locking.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/fight_runner.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
```

## Dynamic camera lifecycle / capability ownership

```text
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/generation.py
fight/runtime_supervisor/camera_state.py
tests/test_dynamic_camera_lifecycle.py
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

## Speed integration / Vehicle recovery

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

# Read with the production structures it exercises:
fight/runtime_supervisor/camera_state.py
fight/runtime_supervisor/core.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/scheduling.py
fight/pipeline_mp/health.py
```

Do not modify production code merely because a benchmark metric is inconvenient. First determine whether the measurement is missing, the workload is unsuitable, or a true production bottleneck is demonstrated.

---

# 31. Cross-phase acceptance checklist

Before accepting any architecture-affecting change, verify every relevant item rather than only the new feature’s happy path.

```text
[ ] ARCHITECTURE.md was read first and coding agent did not edit it
[ ] current production ownership path was inspected, not inferred from legacy helpers
[ ] one Runtime Supervisor still owns the global AI runtime
[ ] one CameraIngest remains the intended source/decode owner per physical camera
[ ] Fight+Speed on one source still use one camera entry/fan-out
[ ] no runtime Django ORM dependency was introduced
[ ] no model-per-camera regression was introduced
[ ] camera-local temporal/calibration state stayed local where required
[ ] shared services start only when desired capabilities require them
[ ] ordinary capability transitions do not globally restart the runtime
[ ] optional disabled services remain healthy/service_disabled
[ ] stable slot/generation stale-result protection is preserved
[ ] Speed consumer epoch protection is preserved
[ ] shared-service epoch protection is preserved
[ ] Fight publication floor still blocks old-incarnation durable incidents
[ ] Fight recovery replaces the whole Fight transport/bundle safely
[ ] Fight retry/backoff is bounded; no restart storm
[ ] Vehicle recovery replaces Vehicle transport safely
[ ] Vehicle retry/backoff is bounded and isolated from Fight
[ ] unsafe teardown prefers fail-closed over duplicate source ownership
[ ] file Fight/Speed partial failures are not replayed as clean success
[ ] live freshness semantics remain distinct from ordered-file semantics
[ ] fair per-slot scheduling/capacity remains bounded
[ ] no correctness dependency on OS qsize()/empty()
[ ] health events/snapshots remain bounded and epoch-aware
[ ] Incident/Reporter runtime-global failure semantics remain explicit
[ ] durable outbox remains the runtime->Django truth boundary
[ ] Speed still creates Incident(type=SPEED) in the common incident domain
[ ] location/security authorization remains application-layer
[ ] retention/disk-pressure protections remain fail-safe/bounded
[ ] Windows spawn compatibility is tested for multiprocessing changes
[ ] benchmark and production result semantics are not mixed
[ ] synthetic camera-equivalent counts are not called real capacity
[ ] measurements from one GPU are not extrapolated into another GPU camera count
[ ] benchmark code does not change production thresholds/queue/recovery behavior
[ ] unavailable metrics remain unavailable rather than fabricated
[ ] generated runtime/benchmark artifacts are not staged
[ ] UI/template/static remains unchanged during backend-only phases
[ ] PostgreSQL remains untouched unless explicitly promoted
[ ] Docker/Nginx/deployment packaging remains untouched unless explicitly promoted
[ ] shared-memory transport is introduced only after measurement justifies it
```

---

# 32. Current deferred work and frozen scope

Phase 16 supplies measurement infrastructure; it does **not** finish production-scale qualification.

## Backend/runtime work still worth promoting deliberately

- real inference scaling sweeps using the Phase-16 harness on comparable Fight-only, Speed-only and mixed workloads,
- production-target GPU validation on the actual RTX 5090-class system rather than extrapolation from the RTX 3050,
- sustained live/RTSP scale tests with realistic FPS/resolution/network behavior,
- long soak tests covering camera churn, EOF/reconnect, capability changes and recovery,
- deliberate chaos/failure tests on real spawned workers when safe worker identity/control is available,
- measured analysis of CameraIngest decode cost and multiprocessing NumPy frame-copy/IPC cost,
- shared-memory frame transport **only if** those measurements show it is justified,
- mixed Fight+Speed recovery refinement to reduce the current camera-local Speed interruption during Fight bundle recovery if operationally valuable,
- Fight decision-quality validation on a representative field dataset,
- Speed accuracy/calibration validation on representative camera geometry/traffic,
- incident duplicate/temporal semantics validation in realistic field sequences,
- trend/long-horizon observability beyond current bounded health snapshots,
- evidence/storage sizing and cleanup-scan observability,
- operator tooling for abnormal/legacy/aborted runs,
- multi-GPU partitioning only after single-node bottlenecks are measured.

## Explicitly frozen/deferred for now

Do not absorb these into a backend phase by default:

```text
PostgreSQL migration
Docker redesign
Nginx/media offload
production deployment/service packaging
frontend/dashboard redesign
incident UX redesign
preview/offline UX redesign
```

Existing files related to these areas may remain in the repository. Their existence is not authorization to expand scope.

---

# 33. Capacity qualification strategy from this baseline

The next capacity work should be evidence-driven.

On the current development machine, increase real workload counts manually only while the machine remains safe and measurements remain interpretable. Use identical configs/media where comparative analysis requires it. Record:

```text
aggregate/full-run rate
per-camera completion/progress
Person/Pose/Stage3/Vehicle admission + latency
CPU/RAM
GPU utilization/VRAM
queue occupancy/high-water/drop/rejection/defer
runtime health/recovery
```

When production hardware becomes available, run the **same harness and comparable workload definitions** on that machine. The production question is not “how many times faster is a 5090 than a 3050?”; it is:

```text
At what camera/workload point does measured service quality become PRESSURED,
SATURATED or INCOMPLETE, and which resource/queue/decode/IPC signal moves first?
```

If GPU/model inference saturates first, optimize/model-partition based on that evidence. If CPU/decode/IPC/serialization pressure appears first, target that subsystem. Shared-memory or multi-GPU work should follow measured causality rather than assumption.

No camera-count claim belongs in this architecture contract until it is backed by a clearly described real workload, hardware, configuration and acceptance criterion.
