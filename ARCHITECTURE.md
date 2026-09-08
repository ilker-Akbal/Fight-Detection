Scope
Repository architecture contract including completed Phase-13 Speed integration in the current worktree.
Phase-12 base commit (Phase-13 changes are not committed):
0045300624c50959be32303739b3c1e780146aae
feat: harden operational durability retention and recovery
Primary rule:
Django / Web / DB = control + application plane
Runtime Supervisor = AI runtime process owner
run_multiprocess = runtime parent/orchestrator
CameraIngest = single physical source/decode owner per camera
AI workers = shared inference
camera_worker = camera-local temporal Fight state
speed_worker = camera-local temporal Speed state
Incident Dispatcher = durable runtime -> Django/DB boundary
Runtime code must not import Django ORM.
1. Top-Level Runtime Flow
Django
  |
  | Camera desired state
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
  +--> shared Pose worker
  +--> Pose result router
  +--> shared Stage3 worker
  +--> shared vehicle worker (lazy Speed YOLO)
  +--> Incident worker / IncidentAggregator
  +--> Reporter
  |
  +--> CameraRuntimeManager
         |
         +--> CameraIngest(camera N)
         |      |
         |      +--> fight_queue (when Fight enabled)
         |      +--> speed_queue (when Speed enabled)
         |      +--> preview_queue
         |
         +--> camera_worker(camera N)
         |      |
         |      +--> shared Person admission
         |      +--> shared Pose admission
         |      +--> shared Stage3 admission
         |
         +--> camera_preview(camera N)
         +--> speed_worker(camera N, when Speed enabled)
                |
                +--> shared vehicle admission / per-slot results
                +--> local tracking, calibration, speed and evidence
Incident path:
camera_worker
  -> Stage3
  -> Incident worker
  -> IncidentAggregator
  -> evidence
  -> incidents outbox JSONL
  -> Incident Dispatcher
  -> Incident DB
  -> routing
  -> security-unit inbox / ACK / escalation / resolve
Speed joins the same incident path at the durable outbox:
speed_worker -> finalized evidence -> outbox -> Dispatcher -> Incident(type=SPEED).
2. Process Ownership
Django
Does not:
- own AI child processes
- open centrally owned physical camera sources (Fight or Speed)
- load runtime inference models
- import/runtime-manage multiprocessing workers
Owns:
- Camera configuration
- Location/SecurityUnit authorization
- Incident DB
- routing rules
- user actions
- desired camera publication
- operational cleanup requiring ORM knowledge
Key boundary:
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/*
    -> fight.runtime_supervisor.client
    -> Supervisor HTTP API
Runtime Supervisor
fight/runtime_supervisor/core.py::RuntimeSupervisor
    -> exactly one run_multiprocess parent
States:
STOPPED
STARTING
RUNNING
STOPPING
FAILED
BACKOFF
Important files:
.runtime_supervisor/runtime_state.json
.runtime_supervisor/desired_cameras.json
.runtime_supervisor/supervisor_events.jsonl
.runtime_supervisor/logs/runtime-<run_id>.stdout.log
.runtime_supervisor/logs/runtime-<run_id>.stderr.log
.runtime_supervisor/ is local operational state, not repository data.
Runtime parent
fight/pipeline_mp/run_multiprocess.py
Owns:
- multiprocessing context
- shared inference workers
- result routers
- Incident worker
- Reporter
- health queue / registry / watchdog
- fair admission queues
- CameraRuntimeManager
- dynamic desired-state reconciliation
- file-run drain/finalization
3. Runtime Supervisor Dependency Map
fight/runtime_supervisor/server.py
    -> fight/runtime_supervisor/http_api.py
    -> fight/runtime_supervisor/core.py::RuntimeSupervisor

fight/runtime_supervisor/core.py::RuntimeSupervisor
    -> fight/runtime_supervisor/camera_state.py::DesiredCameraStateStore
    -> fight/runtime_supervisor/locking.py::SingletonLock
    -> fight/operations.py::atomic_json
    -> fight/operations.py::DiskMonitor
    -> fight/pipeline_mp/run_multiprocess.py
Launch:
RuntimeSupervisor::_launch_under_maintenance_lock
    -> validate config
    -> DesiredCameraStateStore.bootstrap(...)
    -> _make_launch_config(...)
    -> python -m fight.pipeline_mp.run_multiprocess --config <launch_config>
Launch config forces:
runtime.dynamic_camera_lifecycle_enabled = true
runtime.desired_camera_state_path = <desired_cameras.json>
runtime.health_snapshot_path = <output_dir>/runtime_health.json
runtime.run_id = <Supervisor run_id>
Global start/stop belongs to Supervisor.
Desired camera publication must not start a globally stopped runtime.
4. Dynamic Camera Lifecycle
Primary implementation:
fight/pipeline_mp/camera_lifecycle.py::CameraRuntimeManager
Canonical runtime camera:
camera_id
source
name
enabled
use_fight_detection
use_speed_detection
speed_config
Runtime reconciliation selects:
enabled == true
AND
(use_fight_detection == true OR use_speed_detection == true)
Fight-only, Speed-only and combined cameras share this lifecycle.
Cameras with neither consumer enabled are not started.
Core relationships:
CameraRuntimeManager::reconcile
    -> start_camera
    -> stop_camera
    -> restart_camera

CameraRuntimeManager::start_camera
    -> allocate stable slot
    -> increment generation
    -> reset per-slot capacity metrics
    -> _spawn_trio

CameraRuntimeManager::_spawn_trio
    -> camera_ingest_process_main
    -> camera_preview_process_main
    -> camera_process_main (Fight enabled only)
    -> speed_process_main (Speed enabled only)
Per-camera runtime object:
CameraRuntime
    camera
    slot_id
    generation
    stop_event
    fight_queue
    preview_queue
    speed_queue / speed_stop / speed_epoch
    speed_failed / speed_restarts / speed_last_restart
    processes
    state
    file_done
    restart_count
5. Stable Slot + Generation Invariant
Files:
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/generation.py
fight/pipeline_mp/messages.py
Identity:
(camera_id, slot_id, generation)
Rules:
- result slots are created before child spawn
- camera restart increments generation
- camera removal invalidates slot generation before teardown
- reused slot belongs to new generation
- delayed results/events from previous generation are rejected
- health events are generation-aware
- scheduling metrics reset when a new camera generation starts
Do not replace this with:
- Manager-based mutable routing
- fork-only shared mutations
- Queue objects sent through other queues
- per-camera inference model processes
Source, consumer enable flags and Speed configuration (including calibration revision)
determine camera process restart identity:
fight/pipeline_mp/camera_lifecycle.py::restart_identity
Cosmetic camera changes do not require camera restart.
Speed-only failure recovery increments a separate consumer epoch, not the camera generation.
Vehicle requests/results, Speed health and durable publication validate that epoch too.
6. Single Decode Ownership
fight/pipeline_mp/camera_ingest.py::camera_ingest_process_main
Invariant:
one physical camera/source
    -> one CameraIngest open/decode path
Production fan-out:
CameraIngest
  +--> fight_queue (optional)
  +--> speed_queue (optional)
  +--> preview_queue
Neither camera_worker nor speed_worker may reopen the centralized source.
Django preview/calibration code must not independently open a centrally owned source.
Production Speed calibration uses the common runtime preview; start that runtime first.
Live source reconnect ownership remains in CameraIngest.
Watchdog must not create a competing RTSP reconnect loop.
7. File vs Live Source Semantics
Source helper:
fight/pipeline_mp/common.py::is_file_source
File
Priority:
correctness + order > freshness
Rules:
- no silent inference work loss
- producer may defer/wait under pressure
- accepted Stage3 work drains before final completion
- Speed frames and its ordered EOF signal drain before final completion
- non-looping file EOF is expected completion
- EOF is not failure
- all-file clean completion exits runtime with code 0
- failed Speed processing is incomplete, not clean EOF; after healthy consumers finish,
  the file run exits 13 rather than replaying a partially processed file consumer
Relevant:
run_multiprocess.py::_should_not_restart_finished_camera
run_multiprocess.py::_wait_for_pipeline_settle
CameraRuntimeManager::poll
Live / RTSP
Priority:
freshness > completeness
Rules:
- bounded latest-frame behavior
- stale live inference may be shed
- stale work is explicit skip/shed, never interpreted as negative detection
- reconnect is owned by CameraIngest
8. Shared Person Inference
Files:
fight/pipeline_mp/person_worker.py
fight/yolo/*
fight/pipeline_mp/messages.py
fight/pipeline_mp/scheduling.py
Topology:
camera_worker[N]
    -> FairRequestQueue / Person admission
    -> one Person inference process
    -> Person result router
    -> per-slot result channel
    -> camera_worker[N]
Invariant:
one shared Person model per worker, not one model per camera
Camera retains its own temporal/tracking state.
Person batching:
- latency bounded
- batch-size bounded
- frame-shape grouping retained
Generation checks apply before stale work can be consumed/delivered.
9. Shared Pose Inference
Files:
fight/pipeline_mp/pose_worker.py
fight/pose/*
fight/pipeline_mp/messages.py
fight/pipeline_mp/scheduling.py
Topology:
camera_worker[N]
    -> ROI
    -> Pose admission
    -> one shared Pose process
    -> Pose result router
    -> per-slot result channel
    -> camera-local pose gate/history
Invariant:
Pose model is shared.
Pose temporal interpretation remains camera-local.
Idle Pose traffic is healthy if heartbeat remains valid.
First CUDA inference may use startup/warm-up grace before inference timeout becomes fatal.
10. Stage3 / X3D
Files:
fight/pipeline_mp/stage3_worker.py
fight/3D_CNN/*
fight/pipeline_mp/scheduling.py
Topology:
camera_worker
    -> Stage3 candidate
    -> fair bounded Stage3 admission
    -> shared Stage3/X3D worker
    -> Incident queue
Stage3 candidates are ordered under pressure.
Do not silently discard admitted Stage3 work required for incident completion.
File EOF waits for admitted Stage3 work to drain before final runtime completion.
11. Fair Scheduling / Capacity
Primary abstraction:
fight/pipeline_mp/scheduling.py::FairRequestQueue
Structure:
slot 0 -> bounded FIFO
slot 1 -> bounded FIFO
slot 2 -> bounded FIFO
...
          |
          v
round-robin single consumer
          |
          v
existing microbatch collector
Producer view:
FairRequestQueue::for_slot
    -> AdmissionPort
Spawn invariant:
each camera receives only its own producer queue handles
Primary helpers:
scheduling.py::admit
scheduling.py::deliver_result
scheduling.py::live_request_stale
Outcomes/counters:
accepted
rejected_capacity
deferred_file
dropped_live
stale_generation
dispatches
high_water
Correctness must not depend on multiprocessing.Queue.qsize().
FairRequestQueue.qsize() is internal accounting from owned counters, including in-flight work.
12. Capacity Policy
Person/Pose:
pending capacity reserved per stable camera slot
Stage3:
pending capacity =
DYNAMIC_CAMERA_SLOT_COUNT * STAGE3_PENDING_PER_CAMERA
Vehicle:
one bounded pending reservation per stable slot, with pre-created per-slot results
and at most one outstanding request per Speed consumer.
Fight and Speed admissions are separate. Live Speed frame pressure sheds explicitly.
Ordered file fan-out may wait for the slower consumer; it does not accumulate unbounded frames.
Rules:
- noisy camera cannot consume another camera's reserved pending capacity
- scheduling is round-robin
- fair admission precedes existing batching
- batching efficiency should remain intact
- overload is not process failure
Live:
full admission
    -> dropped_live / explicit shed
File:
full admission
    -> deferred_file
    -> cooperative wait
Capacity pressure may produce:
DEGRADED / queue_pressure
but is not a watchdog restart reason.
13. Health Plane
Files:
fight/pipeline_mp/health.py
fight/pipeline_mp/messages.py::HealthEvent
fight/pipeline_mp/run_multiprocess.py
Topology:
child processes
    -> one bounded HealthEvent queue
    -> runtime parent
    -> HealthRegistry
    -> RuntimeWatchdog
    -> HealthSnapshotStore
    -> runtime_health.json
Primary classes:
health.py::HealthPolicy
health.py::HealthEmitter
health.py::HealthRegistry
health.py::RuntimeWatchdog
health.py::HealthSnapshotStore
Health registry stores current state, not heartbeat history.
Transition history is bounded.
14. Camera Health
Camera components:
camera_ingest
camera_worker
camera_preview
speed_worker (when enabled)
Health states include:
STARTING
ONLINE
DEGRADED
RECONNECTING
OFFLINE
STOPPING
STOPPED
FAILED
EOF
Lifecycle and health are separate.
Example:
lifecycle = RUNNING
health = DEGRADED
is valid.
Camera metrics include:
ingest heartbeat/progress
frame progress
fight publication
worker frame consumption
Person/Pose request progress
preview publication
event generation
reconnect count
drop counters
capacity counters
Speed enabled/failed, consumer epoch, restarts, progress, drops and heartbeat age
15. Shared Worker Health
Critical components:
person
person_router
pose
pose_router
stage3
incident
Idle shared worker:
heartbeat fresh
-> HEALTHY
Pending synchronous inference:
request_received
    -> pending inference
    -> inference warning deadline
    -> DEGRADED / inference_stall
    -> inference failure deadline
    -> FAILED
Completion/result closes pending work:
inference_completed
result_delivered
work_completed
result_produced
Priority:
process_dead
    -> immediate FAILED

pending inference
    -> inference deadlines take precedence over loop heartbeat timeout
First Person/Pose/Stage3/vehicle inference:
failure grace >= HEALTH_STARTUP_GRACE_SEC
to allow lazy model/CUDA warm-up.
Vehicle is an optional shared worker: its FAILED state degrades runtime health and
withdraws Speed consumers, without failing Fight or triggering whole-runtime recovery.
The existing critical shared-worker list and Phase-10 deadlines are unchanged.
16. Watchdog Actions
fight/pipeline_mp/health.py::RuntimeWatchdog
    -> CameraRuntimeManager
Camera-specific failure:
confirmed camera stall
    -> camera-only restart
    -> generation increment
    -> restart cooldown
    -> bounded restart count
Preview failure:
preview heartbeat/process failure
    -> preview-only restart
    -> generation unchanged
Speed consumer failure/stall:
    -> withdraw Speed branch only
    -> live-only bounded retry using existing camera restart limit/cooldown
    -> consumer epoch increment; Fight/Ingest generation unchanged
    -> no partial-file consumer restart
Shared critical failure:
critical worker dead/hung
    -> controlled runtime failure
    -> Supervisor recovery/backoff policy
One unhealthy camera:
runtime DEGRADED
!= runtime FAILED
Normal RTSP reconnect:
RECONNECTING
-> no competing watchdog restart during reconnect grace
Normal file EOF:
EOF
-> no restart
17. Runtime Health Snapshot
Runtime path:
<run_output>/runtime_health.json
Properties:
- atomic
- bounded camera count
- bounded worker count
- compact
- current-state only
- no raw frame
- no source URL
- no credentials
Supervisor exposes summary via:
GET /status
Detailed authenticated health:
GET /runtime/health
Authorization: Bearer <RUNTIME_SUPERVISOR_TOKEN>
Stopped runtime:
runtime_health = STOPPED
health snapshot unavailable
Running + missing snapshot:
UNKNOWN
Stale snapshot:
stale = true
aggregate normally DEGRADED unless snapshot already FAILED
18. Dynamic Desired Camera State
Files:
fight/runtime_supervisor/camera_state.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
Fight_backend_project/backend_frontend_project/streams/management/commands/run_camera_registry_reconciler.py
Django:
CameraRegistryReconciler
    -> active Fight Camera rows OR enabled Speed Camera/SpeedCameraConfig rows
    -> Supervisor /runtime/cameras
Supervisor:
DesiredCameraStateStore
    -> schema_version
    -> monotonically increasing revision
    -> atomic desired_cameras.json
Runtime:
run_multiprocess
    -> watches desired camera state
    -> CameraRuntimeManager::reconcile
Unchanged desired state should not create unnecessary revisions/restarts.
Desired-state update does not globally start a stopped runtime.
The durable speed_paused service intent survives reconciler ticks; Speed stop disables
only Speed consumers. Speed start unpauses/reconciles and uses the common Supervisor start.
The Speed bridge owns no Popen process; status is reconstructed from common runtime state.
Duplicate enabled source strings are rejected; source aliases still require operator care.
19. Camera DB Model
Fight_backend_project/backend_frontend_project/streams/models.py::Camera
Relevant fields:
camera_id
source
uploaded_video
location
faculty          # legacy compatibility
is_active
use_fight_detection
use_speed_detection
Runtime source:
Camera::get_runtime_source
    uploaded_video.path if present
    else source
Centralized publication includes Fight-only, Speed-only and combined cameras.
Speed also requires SpeedCameraConfig.enabled. Existing SpeedCameraConfig supplies
limit/tolerance, calibration, ROI and evidence flags. Calibration file mtime/size is
published as a revision so updates trigger only the affected camera reconfiguration.
No Phase-13 DB model or migration is required.
20. Physical Authorization Model
Models:
adminx/models.py::Location
adminx/models.py::SecurityUnit
adminx/models.py::SecurityUnitCoverage
adminx/models.py::UserSecurityAssignment
Relationships:
Location
  -> parent Location

SecurityUnit
  -> optional root Location

SecurityUnitCoverage
  -> SecurityUnit
  -> Location
  -> include_descendants

UserSecurityAssignment
  -> User
  -> SecurityUnit

Camera
  -> Location
Location invariants:
- no self-parent
- no indirect cycle
- PROTECT parent deletion
- effective active state includes ancestor state
Legacy:
Camera.faculty
adminx.models.FacultyLocation
is transition compatibility; authorization uses Camera.location when assigned.
21. Access Scope
Primary service boundary:
Fight_backend_project/backend_frontend_project/services/access_scope.py
Conceptual dependency:
User
  -> UserSecurityAssignment
  -> SecurityUnit
  -> SecurityUnitCoverage
  -> Location tree
  -> Camera.location
Admin/privileged bypass and legacy-null-location behavior belong in access-scope service code, not runtime.
Incident visibility must derive from the same camera/location/security-unit scope.
22. Incident Domain
Models:
incidents/models.py::Incident
incidents/models.py::IncidentRoutingRule
incidents/models.py::IncidentRoute
incidents/models.py::IncidentAuditEvent
incidents/models.py::IncidentIngestCursor
incidents/models.py::IncidentIngestRecord
Incident types:
FIGHT
SPEED
OTHER
Incident state:
OPEN
ACKNOWLEDGED
RESOLVED
Routing state:
PENDING
ROUTED
UNROUTED
Identity invariant:
unique(source_system, run_id, external_incident_id)
event_id unique
Camera deletion is protected:
Incident.camera -> Camera on_delete=PROTECT
23. Fight Incident Production
Files:
fight/pipeline_mp/incident_worker.py
fight/pipeline/incident_aggregator.py
fight/pipeline/incident_outbox.py
Runtime dependency:
Stage3 result
    -> incident_worker
    -> IncidentAggregator
    -> finalized evidence
    -> durable outbox
    -> legacy incidents.jsonl
Durability invariant:
durable outbox succeeds before legacy incident output is considered published
Persistence failure must not be represented as incident success.
IncidentAggregator does not load a second local Pose/Ultralytics inference model for final overlay processing.
24. Durable Incident Outbox
Primary:
fight/pipeline/incident_outbox.py
Configured path:
settings.INCIDENT_OUTBOX_PATH
    -> MEDIA_ROOT/runtime_spool/incidents_outbox.jsonl
Write properties:
- append-only JSONL
- serialized writers
- partial-tail preservation
- newline boundary
- short-write handling
- flush/fsync
- writer lock
Do not compact/delete durable outbox from runtime.
Runtime remains ORM-free.
25. Incident Dispatcher
Command:
incidents/management/commands/run_incident_dispatcher.py
Flow:
run_incident_dispatcher
    -> fight.service_loop::run_service
    -> incidents.services.ingest::dispatcher_tick
    -> IncidentIngestCursor
    -> IncidentIngestRecord
    -> Incident
    -> routing
Service singleton:
OPERATIONAL_SERVICE_DIR/dispatcher.lock
Dispatcher cursor invariant:
cursor advances only for safely handled records according to ingest semantics
Partial trailing JSONL data is not treated as consumed.
Transient/retryable infrastructure conditions must remain retryable.
Unknown camera records must not be silently lost.
26. Incident Routing
Routing dependencies:
Incident.camera.location
    -> SecurityUnitCoverage
    -> IncidentRoutingRule
    -> IncidentRoute
Routing rule key:
security_unit
incident_type
routing_stage
delay_sec
priority
active
Active rule uniqueness:
(SecurityUnit, incident_type, routing_stage)
Escalation:
stage 0
  -> delay
  -> later routing stages
ACK:
Incident
IncidentRoute
IncidentAuditEvent
Resolve requires application-domain rules in incidents services/views; routing semantics must not be duplicated in runtime.
27. Reporter / Performance
Files:
fight/pipeline_mp/reporter.py
fight/pipeline_mp/performance.py
Reporter receives bounded status/report messages.
Performance summary can include:
- request timing
- queue timing
- inference timing
- delivery/RTT
- capacity counters
Capacity metrics remain available in final performance_summary.json even when periodic health snapshot writing is disabled.
Do not add per-frame unbounded telemetry.
28. Operational Durability
Files added/changed by the Phase-12 base commit:
fight/operations.py
fight/retention.py
fight/service_loop.py
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/health.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/camera_state.py
fight/runtime_supervisor/core.py

incidents/services/retention.py
incidents/management/commands/run_operational_cleanup.py
incidents/management/commands/run_incident_dispatcher.py
streams/management/commands/run_camera_registry_reconciler.py
backend_frontend_project/settings.py
Phase-12 architecture impact (preserved by Phase 13):
runtime durability
    + disk health
    + retained run state
    + explicit persistence failure

Supervisor
    + durable atomic state
    + event-log rotation
    + disk status
    + maintenance locking

Django services
    + singleton lifecycle
    + interruptible service loops
    + retention boundary
29. Atomic Operational Writes
Primary helper:
fight/operations.py::atomic_json
Used by:
RuntimeSupervisor runtime state
Desired camera state
cleanup status
run lifecycle state where applicable
Required semantics:
write temp
flush
fsync file
replace target
directory fsync on POSIX where supported
Failed replacement should leave prior target intact.
Stale owned temp files may be replaced/cleaned according to retention ownership rules.
30. Disk Pressure
Primary:
fight/operations.py::DiskMonitor
Config:
DISK_WARNING_BYTES
DISK_CRITICAL_BYTES
DISK_CHECK_INTERVAL_SEC
Default:
warning  = 5 GiB free
critical = 1 GiB free
interval = 30 sec
Runtime health:
HealthRegistry.disk
Rules:
disk pressure
    -> operational/health degradation
    -> no camera restart
    -> no shared-worker restart
    -> no watchdog restart storm
Actual persistence failure:
must be explicit/fatal to affected persistence path
ENOSPC must never be reported as successful incident durability.
31. Service Loop
Primary:
fight/service_loop.py::run_service
Used by:
run_incident_dispatcher
run_camera_registry_reconciler
run_operational_cleanup
Responsibilities:
- local singleton lock
- once/loop operation
- interruptible sleep
- bounded retry/backoff
- SIGINT/SIGTERM handling
- no busy loop
Django services close/reopen old DB connections around ticks as needed.
Locks are local-host coordination, not distributed locks.
32. Retention Boundary
Runtime-generic retention:
fight/retention.py::RetentionPass
ORM-aware retention:
incidents/services/retention.py
Key functions:
incidents/services/retention.py::cleanup_tick
incidents/services/retention.py::outbox_consumed
incidents/services/retention.py::evidence_referenced
Command:
python manage.py run_operational_cleanup
python manage.py run_operational_cleanup --once
python manage.py run_operational_cleanup --once --dry-run
33. Retention Invariants
Transient defaults:
health/metrics/previews      7 days
owned temp files             1 day
completed runs              30 days
closed Supervisor logs      14 days
Bounds:
RETENTION_MAX_SCAN
RETENTION_MAX_FILES
Must protect:
- current run
- active run
- unknown/aborted/pre-Phase12 run
- failed run requiring recovery
- runtime recovery state
- lock files
- durable JSONL history
- unconsumed outbox
- referenced Incident evidence
- files currently owned by active writers
Only Phase-12 runs marked:
COMPLETED
are eligible for normal run cleanup.
No recursive unknown-directory deletion.
34. Evidence Retention
Default:
RETENTION_EVIDENCE_DAYS=0
-> retain indefinitely
If enabled:
minimum age = 180 days
Deletion additionally requires:
runtime STOPPED
runtime PID absent
dispatcher exclusive lock
outbox writer exclusive lock
outbox file identity matches cursor
cursor offset == full outbox size
outbox ends with newline
no retryable IncidentIngestRecord
evidence not referenced by any Incident
evidence_valid=False does not make a referenced evidence file deletable.
35. Outbox Consumption Check
incidents/services/retention.py::outbox_consumed
Fail closed if:
- outbox missing
- ingest cursor missing
- file identity mismatch
- byte offset != file size
- partial trailing record
- retryable ingest records exist
Missing durable history is not proof of consumption.
36. Operational Cleanup Locking
cleanup_tick
    -> Supervisor maintenance.lock
Evidence deletion additionally:
dispatcher.lock
outbox writer lock
Purpose:
prevent cleanup racing runtime launch, dispatcher ingest or outbox append
Unknown runtime ownership:
cleanup blocked
rather than guessing safety.
37. Supervisor Telemetry Rotation
RuntimeSupervisor::_record
Path:
.runtime_supervisor/supervisor_events.jsonl
Config:
SUPERVISOR_EVENT_LOG_MAX_BYTES
SUPERVISOR_EVENT_LOG_BACKUPS
Default:
8 MiB
3 backups
This log is operational telemetry, not durable incident history.
38. Run Lifecycle / Recovery
Run artifacts:
PIPELINE_OUTPUT_BASE/<run>/
Typical:
run_config.json
run_config.supervisor-<run_id>.json
runtime_health.json
performance_summary.json
incidents/
preview/
run state/marker files
Abnormal termination policy:
- fail closed
- retain unknown/aborted artifacts
- do not infer safe deletion
- tolerate stale health/config/temp artifacts
File clean completion:
drain Fight and Speed consumers, including admitted Stage3
-> wait incident finalization
-> mark completion
-> runtime exit 0
39. Browser / Evidence Encoding Boundary
Operational evidence is generated before Django presentation.
Do not let Django incident listing rewrite evidence.
Final browser evidence should remain compatible with existing H.264/AVC + faststart stabilization path.
HTTP Range/evidence serving belongs to Django/web application layer, not runtime inference.
UI/media redesign is separate from runtime architecture.
40. Configuration Sources
Main runtime config:
JSON run config
    -> runtime{}
    -> cameras[]
    -> speed{} (existing Speed builder/settings and base Speed YAML)
Environment:
.env / process environment
Key groups:
RUNTIME_SUPERVISOR_*
SUPERVISOR_*
HEALTH_*
CAMERA_*_TIMEOUT*
INFERENCE_STALL_*
WATCHDOG_*
DYNAMIC_CAMERA_*
FAIR_SCHEDULING_ENABLED
PERSON_PENDING_PER_CAMERA
POSE_PENDING_PER_CAMERA
STAGE3_PENDING_PER_CAMERA
CAMERA_INGEST_SPEED_QUEUE_SIZE (default 2)
SPEED_* (existing Speed settings; no new detection thresholds)
LIVE_FRAME_MAX_AGE_SEC
LIVE_INFERENCE_MAX_AGE_SEC
CAPACITY_OVERLOAD_RATIO
DISK_*
RETENTION_*
Django config:
Fight_backend_project/backend_frontend_project/backend_frontend_project/settings.py
Example/default reference:
.env.example
41. Important Queue Contracts
Ingest queues
Per camera:
CameraIngest -> fight_queue -> camera_worker
CameraIngest -> speed_queue -> speed_worker
CameraIngest -> preview_queue -> camera_preview
File consumer queues:
ordered / bounded
Live queue:
freshness / replacement policy
Shared inference admission
camera_worker -> Person FairRequestQueue
camera_worker -> Pose FairRequestQueue
camera_worker -> Stage3 FairRequestQueue
speed_worker -> vehicle FairRequestQueue
Result channels
Pre-created by slot:
slot_id -> Person result channel
slot_id -> Pose result channel
slot_id -> vehicle result channel (service delivers directly; no camera-specific model)
Health
all instrumented children
    -> one bounded HealthEvent queue
    -> runtime parent only
Reporting
workers/processes
    -> bounded report queue
    -> Reporter
Incident
Stage3
    -> incident queue
    -> Incident worker
42. Message Identity Contracts
See:
fight/pipeline_mp/messages.py
Inference/work messages must carry sufficient identity to reject stale work:
camera_id
slot_id
generation
request identity
frame/work identity
Health:
HealthEvent
    component
    component_type
    event_type
    monotonic_ts
    camera_id
    slot_id
    generation
    progress
    secondary_progress
    queue_depth
    dropped
    reconnect_count
    detail
    consumer_epoch (Speed; default zero for existing emitters)
Health detail is bounded.
Do not put frame/image payloads into health events.
43. Time Semantics
Use monotonic clocks for:
- watchdog deadlines
- heartbeat age
- inference stall age
- restart cooldowns
- scheduler freshness/admission timing
Use wall-clock/UTC for:
- persisted timestamps
- incident timestamps
- logs
- API/display metadata
Never use wall-clock jumps for liveness decisions.
44. Fight Camera Local State
fight/pipeline_mp/camera_worker.py
Owns camera-local Fight state such as:
Motion
tracker/pairs/ROI
PoseGate/history
event/prebuffer
temporal request state
outstanding Person/Pose request state
generation-aware result handling
Shared models must not absorb camera-local temporal state.
Restarting one camera resets its local generation/state without restarting shared models.
45. Model Ownership
Expected production ownership:
Person model -> shared Person worker
Pose model   -> shared Pose worker
X3D-M       -> shared Stage3 worker
Vehicle YOLO -> shared vehicle worker, lazily loaded on first valid request
Not allowed:
N cameras -> N copies of Person/Pose/Stage3/vehicle models
Incident finalization must not instantiate additional inference models.
46. Failure Domains
Camera-local
CameraIngest process death
camera_worker process death
camera stall
Action:
restart one camera generation
Preview-local
camera_preview process death/stale heartbeat
Action:
restart preview only
Speed-local
speed_worker process death/stall or evidence/outbox failure
Action:
withdraw failed Speed consumer, preserve healthy Fight/ingest/preview, bounded live retry
Shared vehicle death/hung inference
Action:
degrade/withdraw Speed, preserve Fight; hard shared vehicle failure needs operator recovery
Shared critical
Person/Pose/Stage3/Incident/router death
confirmed shared-worker hung inference
Action:
controlled runtime failure
-> Supervisor handles whole-runtime recovery
Application services
Incident Dispatcher
Camera Registry Reconciler
Operational Cleanup
Not part of runtime HealthRegistry.
Service/deployment management owns their availability.
47. Local Service Startup
From repository root:
python -m fight.runtime_supervisor.server
Backend directory:
python manage.py run_camera_registry_reconciler
python manage.py run_incident_dispatcher
python manage.py runserver
Optional cleanup:
python manage.py run_operational_cleanup
Supervisor must be the normal AI runtime owner.
Do not run competing direct runtime instances alongside Supervisor-managed runtime.
48. Tests by Architecture Area
Runtime:
tests/test_dynamic_camera_lifecycle.py
tests/test_runtime_health.py
tests/test_capacity_scheduling.py
tests/test_runtime_supervisor.py
tests/test_supervisor_django_bridge.py
tests/test_operational_durability.py
tests/test_speed_integration.py
Django focused phase tests:
adminx/phase9_tests.py
incidents/phase12_tests.py
incidents/phase13_tests.py (isolated SQLite test subprocess from general pytest)
Task rule:
modify architecture invariant
-> read corresponding focused test before implementation
-> run focused tests as needed
-> final general pytest
49. Task Router
Camera add/remove/restart/reconfigure
Read together:
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/generation.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/camera_state.py
services/pipeline_bridge/camera_registry.py
tests/test_dynamic_camera_lifecycle.py
Source/decode/reconnect/file EOF
Read:
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/common.py
fight/pipeline_mp/run_multiprocess.py
Person inference
Read:
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/scheduling.py
fight/yolo/*
Pose inference
Read:
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/scheduling.py
fight/pose/*
Stage3/X3D
Read:
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/scheduling.py
fight/3D_CNN/*
Fairness/backpressure/load
Read:
fight/pipeline_mp/scheduling.py
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/health.py
tests/test_capacity_scheduling.py
Health/watchdog
Read:
fight/pipeline_mp/health.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/camera_lifecycle.py
fight/runtime_supervisor/core.py
tests/test_runtime_health.py
Supervisor/API
Read:
fight/runtime_supervisor/core.py
fight/runtime_supervisor/http_api.py
fight/runtime_supervisor/client.py
fight/runtime_supervisor/server.py
fight/runtime_supervisor/camera_state.py
fight/runtime_supervisor/locking.py
tests/test_runtime_supervisor.py
Incident generation/evidence
Read:
fight/pipeline_mp/incident_worker.py
fight/pipeline/incident_aggregator.py
fight/pipeline/incident_outbox.py
fight/operations.py
Incident ingest
Read:
incidents/services/ingest.py
incidents/models.py
incidents/management/commands/run_incident_dispatcher.py
fight/service_loop.py
Routing/ACK/resolve/escalation
Read:
incidents/models.py
incidents/services/*
services/access_scope.py
adminx/models.py
streams/models.py
Authorization/location
Read:
adminx/models.py
services/access_scope.py
streams/models.py
Retention/cleanup/disk durability
Read:
fight/operations.py
fight/retention.py
fight/service_loop.py
incidents/services/retention.py
incidents/management/commands/run_operational_cleanup.py
fight/runtime_supervisor/core.py
fight/pipeline/incident_outbox.py
tests/test_operational_durability.py
incidents/phase12_tests.py
Django runtime start/status bridge
Read:
services/pipeline_bridge/fight_runner.py
services/pipeline_bridge/camera_registry.py
fight/runtime_supervisor/client.py
UI-only task
Read only relevant:
guvenlik/templates/**
guvenlik/static/**
adminx/templates/**
adminx/static/**
Do not modify runtime architecture for UI-only requirements.
Speed task
Completed integration references:
fight/pipeline_mp/speed_worker.py
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_lifecycle.py
services/pipeline_bridge/camera_registry.py
services/speed_bridge/speed_runner.py
HizTespiti/speed/src/evidence_writer.py
tests/test_speed_integration.py
DB contract:
streams.models.Camera.use_speed_detection
incidents.models.Incident.TYPE_SPEED
Integration reuses:
CameraIngest
CameraRuntimeManager
generation identity
health
fair scheduling
durable incident outbox
Incident(type=SPEED)
routing/access
Do not build a second independent runtime/incident system.
50. Cross-Phase Invariants
Never break:
1. one source/decode owner per physical camera
2. runtime owns no Django ORM
3. Django/Gunicorn owns no AI child process
4. Supervisor owns global AI runtime lifecycle
5. shared inference model per worker, not per camera
6. temporal Fight and Speed state remains camera-local
7. slot + generation rejects stale work
8. one bad camera does not fail entire runtime
9. critical shared-worker failure may fail runtime
10. preview is non-critical
11. RTSP reconnect belongs to CameraIngest
12. file EOF is clean completion
13. live freshness and file ordering are distinct policies
14. overload != process failure
15. fair scheduling prevents noisy-camera starvation
16. outbox durability precedes Django import
17. runtime never writes Django Incident rows directly
18. dispatcher cursor cannot silently skip retryable data
19. referenced evidence is retention-protected
20. disk pressure alone does not trigger restart storms
21. active/current/unknown run artifacts fail closed against cleanup
22. no credential/source leakage in health/status snapshots
23. no unbounded heartbeat/per-frame history
24. no UI change during backend-only architecture phases unless explicitly requested
51. Current Deferred Architecture Work
After Phase 13:
automatic optional vehicle-service respawn (hard failures currently require operator recovery)
avoiding eager Fight shared-worker startup for Speed-only deployments
PostgreSQL migration
production-scale capacity measurement
shared-memory/frame transport decision
multi-GPU partitioning
production process/service deployment
production media offload/Nginx
dashboard/UI redesign
incident media lazy-loading UX
preview/offline UX
durable incident-history archival/compaction design
legacy/aborted-run operator tooling
These are separate tasks.
Do not fold them into unrelated fixes.
52. Fast Change-Impact Rules
camera_lifecycle.py change
    -> generation.py
    -> run_multiprocess.py
    -> health.py
    -> scheduling.py
    -> dynamic lifecycle tests

messages.py identity change
    -> camera_worker
    -> Person/Pose/Stage3 workers
    -> result routers
    -> health
    -> tests

scheduling.py change
    -> Person
    -> Pose
    -> Stage3
    -> camera_worker
    -> health capacity metrics
    -> file EOF drain behavior

health.py change
    -> all emitters
    -> RuntimeWatchdog
    -> run_multiprocess
    -> Supervisor health API
    -> runtime health tests

camera_ingest.py change
    -> file/live semantics
    -> preview
    -> camera_worker
    -> CameraRuntimeManager
    -> EOF/reconnect behavior

incident_outbox.py change
    -> IncidentAggregator
    -> Incident Dispatcher ingest
    -> retention outbox-consumed checks
    -> durability tests

Incident model change
    -> ingest
    -> routing
    -> access
    -> retention evidence reference checks
    -> API/views/UI consumers

Supervisor state schema change
    -> core.py
    -> http_api.py
    -> client.py
    -> retention cleanup
    -> tests

Camera model/runtime publication change
    -> streams/models.py
    -> camera_registry.py
    -> DesiredCameraStateStore
    -> CameraRuntimeManager
53. Repository Boundaries
fight/
    runtime/inference/process layer

Fight_backend_project/backend_frontend_project/
    Django/application/DB/web layer
Allowed dependency:
Django services
    -> runtime Supervisor client / generic runtime helpers
Forbidden dependency:
fight runtime workers
    -> Django models/ORM
ORM-aware cleanup is intentionally:
incidents/services/retention.py
while generic filesystem retention remains:
fight/retention.py
Keep this boundary.
54. Completed Speed Consumer / Incident Contract
fight/pipeline_mp/speed_worker.py owns the CameraIngest-fed integration.
SpeedProcessor reuses existing motion gating, ROI, SimpleIoUTracker, calibration,
SpeedEstimator, ViolationDecider, visualization and buffered evidence components.
Temporal state is per camera/consumer epoch; YOLO inference is shared and lazy.
Original frame sequence/source FPS are retained when live frames are shed, so skips
do not compress the estimator's frame-based time. Quiet traffic remains healthy.
No Speed or Fight detection model, accuracy threshold or calibration algorithm is redesigned.

Evidence path:
<common_run>/incidents/speed/<camera_id>/<generation>-<consumer_epoch>/
    clips/   snapshots/   events/
Successful persistence order:
write selected evidence -> verify write -> fsync evidence -> durable shared outbox append
    -> legacy Speed events JSONL (compatibility only)
Outbox failure/ENOSPC raises; it is never reported as successful legacy publication.
SpeedGenerationGuard locks camera generation and Speed epoch across durable append,
rejecting publication from withdrawn generations/consumers.
SpeedEnvelope extends the existing outbox envelope with measured-speed, limit,
tolerance, track/frame/time and evidence metadata; the Fight envelope is unchanged.
The existing dispatcher creates Incident(type=SPEED) with the same identity, cursor,
routing, authorization and Phase-12 retention rules. Runtime never imports ORM.

Normal Django Speed start/stop/status uses the common Supervisor runtime.
Existing Speed report/media endpoints resolve common-run evidence without changing
templates/static assets, HTTP Range behavior or evidence encoding policy.
Legacy standalone Speed CLI remains offline tooling, not a competing production owner.
Existing Speed evidence buffering/post-roll behavior is preserved, not redesigned.
GPU/production-scale validation and transport optimization remain separate work.
