# Fight-Detection Architecture Contract

## Document ownership and maintenance discipline

This file is the architecture contract for the repository. It describes the **current Supervisor-managed production architecture**, ownership and failure boundaries, identity/fencing rules, live-vs-file semantics, durability boundaries, health/recovery behavior, performance/benchmark interpretation, measured characterization evidence, and intentionally deferred work.

**Maintenance rule:** coding agents (including Codex) must read this file before architecture-affecting work and **must not modify it**. The project owner and ChatGPT maintain it from committed repository state.

Architecture refreshes are not a narrow “append the latest commit” exercise. Before changing this file, the maintainer must read the whole current contract, inspect the current `master` implementation of affected ownership paths, compare the new code with the prior architecture baseline, inspect focused tests as executable contracts, remove stale or contradictory claims, verify health/recovery/durability/task-router/deferred-work sections remain consistent, and keep measured facts separate from estimates or future production assumptions.

Current production-code reference commit:

```text
cbeb45066e8d25b6b6d2eeeda757e61b5e23e562
Finalize Phase 20/21 live recovery and Fight-Speed isolation
```

**Phase 20/21 is committed on `master` and is the current production architecture baseline.** It preserves the shared Fight + Speed runtime while separating camera/source lifetime from Fight-consumer lifetime. A recoverable Fight failure can now replace only the affected Fight consumer(s) and/or shared Fight bundle while keeping a healthy CameraIngest, Preview, Speed consumer, Vehicle service, camera generation, and Speed epoch alive. A new parent-owned Fight consumer incarnation fences stale work when a Fight consumer is replaced without changing the source generation.

Phase 20/21 also hardens live reconnect shutdown responsiveness, capability churn during recovery, camera-local Fight recovery, evidence identity across consumer replacement, stale health/result/incident fencing, and a Speed-consumer spawn-failure retry edge case. Ordered-file fail-closed/no-replay semantics, Phase-19 graceful finalization, Phase-19.1 Windows snapshot behavior, production Person batching defaults, Django models, UI, deployment architecture, and detection/calibration thresholds remain unchanged.

Validation reported for this baseline:

```text
focused lifecycle/shared-service/health/Speed/ingest/batching tests:
  152 passed, 26 subtests passed

full pytest:
  236 passed, 26 subtests passed

compileall fight benchmarks tests:
  passed

git diff --check:
  passed

Django models / migrations:
  unchanged

Person batching default:
  OFF
```

Automated spawn tests establish process/transport ownership and stale-work fencing with generated frames/stub detectors. They do **not** establish real external RTSP/network behavior or target-GPU capacity. Real live-source qualification remains an environment-specific acceptance step.

---

# 1. System purpose and direction

The repository is a centralized multi-camera security platform in which Fight Detection and Speed Detection share runtime infrastructure while preserving camera-local temporal state.

```text
Django / application control plane
    -> Camera Registry Reconciler
    -> Runtime Supervisor
        -> one global run_multiprocess parent
            -> one CameraIngest source/decode owner per physical camera
            -> independently gated Fight / Speed / Preview consumers
            -> capability-managed shared inference services
            -> camera-local Fight/Speed temporal state
            -> runtime-global Reporter + Incident processing
            -> bounded best-effort health/performance/attribution reporting
    -> durable incident outbox
    -> Django Incident Dispatcher
    -> common Incident / routing / authorization domain
```

The production design is deliberately **not** “one complete AI pipeline per camera”. Expensive/stateless inference is shared across cameras. Temporal interpretation that depends on one camera’s history remains camera-local.

The runtime now distinguishes three separate camera-related lifetimes:

```text
SOURCE / CAMERA INCARNATION
  CameraIngest + physical source + Preview + camera generation

FIGHT CONSUMER INCARNATION
  camera_worker + Fight-local temporal state + Fight consumer epoch

SPEED CONSUMER INCARNATION
  speed_worker + tracking/calibration/speed state + Speed consumer epoch
```

Shared model services have their own service incarnations:

```text
FIGHT SERVICE INCARNATION
  Person / router / optional Pose/router / optional Stage3
  -> Fight service epoch

VEHICLE SERVICE INCARNATION
  Vehicle worker/model
  -> Vehicle service epoch
```

These identities are intentionally separate. A Fight-consumer replacement is not a source replacement. A shared Fight-service replacement is not a camera-generation replacement. A Speed replacement is not a Fight replacement.

Target scale is large multi-camera deployment, but this contract does **not** assert a production camera-count guarantee. RTX 3050 measurements are characterization evidence for specific local-file workloads, not an RTX 5090 estimate, not a sustained RTSP SLA, and not evidence that 200/300 real cameras fit one node.

---

# 2. Non-negotiable invariants

1. Runtime workers must not import or depend on Django ORM.
2. Django/Gunicorn is the application/control plane, not the AI child-process owner.
3. Runtime Supervisor owns the global production AI runtime lifecycle.
4. `run_multiprocess` owns the multiprocessing topology below the Supervisor.
5. One physical camera has exactly one intended `CameraIngest` source/decode owner in the Supervisor-managed production runtime.
6. Fight and Speed on the same physical camera share one desired camera entry and one CameraIngest decode path.
7. Fight recovery must not create a second CameraIngest or temporary duplicate physical-source owner.
8. `camera_worker`, `speed_worker`, Preview and Django views must not independently reopen the centralized production source during normal Supervisor operation.
9. Expensive/stateless inference models are shared services, not model-per-camera instances.
10. Fight temporal tracking/pair/ROI/event state and Speed tracking/calibration/speed state remain camera-local where history matters.
11. Shared inference services are capability-aware and run only while the desired camera set requires them.
12. Desired capability changes do not require a global runtime restart solely because capability requirements changed.
13. Camera/source incarnation is fenced by stable slot + camera generation.
14. Fight consumer incarnation adds a separate per-slot Fight epoch when a Fight consumer can be replaced while camera generation remains stable.
15. Speed adds its own consumer epoch.
16. Recoverable shared services add service epoch where required.
17. Fight durable publication uses both shared-service publication fencing and per-Fight-consumer publication fencing.
18. Old/stale work may physically finish but must fail closed logically; it cannot become current health, result, or durable incident truth after reconfiguration/recovery.
19. Live and file workloads intentionally use different backpressure/recovery semantics.
20. Correctness must not depend on OS `Queue.qsize()` or `Queue.empty()` observations. Parent-owned/custom queue accounting may be correctness input only where its semantics are controlled.
21. Queueing, telemetry, retries, health scans and operational cleanup remain bounded.
22. Windows `spawn` compatibility is a first-class constraint.
23. Runtime incident truth crosses into Django through the durable incident outbox; runtime workers do not create Incident ORM rows.
24. Fight and Speed share the same Django Incident/routing/authorization domain.
25. Optional service absence is healthy when that service is not required.
26. Runtime-global Incident/Reporter failure remains explicit; those processes are not silently reconstructed as optional capability services.
27. Synthetic camera-equivalents are not production inference capacity.
28. Measurements from one GPU/workload must not be linearly extrapolated to another GPU/workload.
29. Benchmark code must not mutate production thresholds, source ownership, queue semantics or recovery behavior merely to improve results.
30. PostgreSQL, Docker, Nginx, deployment/service packaging and UI redesign remain frozen/deferred unless explicitly promoted.
31. Shared-memory frame transport remains deferred until controlled measurement demonstrates transport is a material bottleneck.
32. **Ordered non-looping file EOF is correctness state, not telemetry.** The authoritative EOF fact is a generation-local multiprocessing Event owned by the current source runtime and published by CameraIngest before consumer EOF signals.
33. A dead required file consumer is a clean drain only when authoritative EOF has been reached and that process exited with code `0`.
34. Clean EOF must not increment camera generation, reopen the source, replay the file, or synthesize watchdog recovery.
35. A Fight failure on an ordered file must not be transparently resumed under live-style consumer recovery after ordered work may have been lost.
36. Attribution/performance telemetry is observation only. It must never drive health, admission, generation, consumer epoch, service recovery, EOF, durable incident publication, or benchmark classification.
37. Missing/disabled/no-sample metrics remain unavailable/null; they must not be fabricated as zero.
38. Normal graceful finalization is distinct from failure recovery. A failed or poisoned service incarnation must not be reused merely to obtain final metrics.
39. Final telemetry is best-effort observability, not incident durability and not an ordered-file correctness signal.
40. Health snapshot publication failure is best-effort/non-fatal, but errors remain observable; snapshot retry policy stays bounded and must not hide persistent filesystem errors.
41. Failure reporting preserves the cause observed **before teardown**. Exit codes caused by later forced termination must not be misrepresented as the original cause.
42. Person microbatching remains configurable but **OFF by default** unless a future explicit architecture decision changes that after target-workload validation.
43. Fight recovery must preserve healthy Speed state on mixed LIVE cameras whenever source/Speed ownership itself is healthy.
44. A camera-local Fight consumer failure must not automatically recycle the whole shared Fight bundle.
45. A shared Fight-bundle failure must not automatically recycle CameraIngest, Preview, Speed, or Vehicle.
46. Global stop is authoritative: reconnect/recovery/backoff paths must not recreate consumers after shutdown begins.

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
  +--> SharedServices                        parent-owned service lifecycle
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
         +--> CameraIngest(camera N)         source-generation owner
         |      +--> Fight branch gate/queue
         |      +--> Speed branch gate/queue
         |      +--> Preview queue
         |
         +--> camera_worker(camera N)        Fight consumer incarnation
         +--> speed_worker(camera N)         Speed consumer incarnation
         +--> camera_preview(camera N)       source-generation component
```

Fight path:

```text
CameraIngest
 -> Fight branch tagged with current Fight consumer epoch
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

It owns exactly one common `fight.pipeline_mp.run_multiprocess` parent in normal Supervisor mode.

Supervisor states remain:

```text
STOPPED
STARTING
RUNNING
STOPPING
FAILED
BACKOFF
```

Local Supervisor state under `.runtime_supervisor/` is operational data, not repository source.

Phase 20/21 exposes compact Fight status through the existing Supervisor status projection; it does not transfer lifecycle policy to the web process.

## 4.3 Runtime parent owns

Primary implementation:

```text
fight/pipeline_mp/run_multiprocess.py
```

The dynamic parent owns:

- multiprocessing spawn context,
- stable camera slots,
- camera-generation array,
- Speed-consumer epoch array,
- Fight-consumer epoch array,
- Fight publication-floor vector,
- current Fight service epoch,
- current Vehicle service epoch through `SharedServices`,
- Reporter and Incident workers,
- `SharedServices`,
- `CameraRuntimeManager`,
- desired-state polling/reconcile,
- health registry/watchdog/snapshot publication,
- fair admissions,
- ordered-file EOF/drain/finalization,
- performance-summary construction,
- global exit semantics.

Lifecycle policy remains parent-owned. Child workers receive only spawn-safe queues, Events/Arrays, simple identity values and configuration required for their role.

---

# 5. Desired camera state and capability reconciliation

Primary files:

```text
fight/runtime_supervisor/camera_state.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
fight/pipeline_mp/camera_lifecycle.py
```

Schema version remains `1`. Canonical camera fields include:

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

Supported modes:

```text
Fight-only
Speed-only
Fight + Speed
neither / not started
```

Desired state has a monotonic revision; stale/equal revisions must not create duplicate lifecycle work. `speed_paused` remains durable desired intent and stopping Speed is not equivalent to stopping Fight/common runtime.

## 5.1 Source-preserving LIVE capability change

Phase 20/21 changes the meaning of same-source LIVE capability updates.

When source identity is unchanged and the source is LIVE/non-file, a Fight/Speed capability or Speed-config transition can reconfigure consumers without replacing the source runtime merely because branch composition changed.

Conceptually:

```text
same live source
 + capability/config change
 -> keep CameraRuntime object/source generation
 -> keep CameraIngest
 -> keep Preview
 -> stop/start only affected Fight/Speed consumer(s)
```

A genuine source change remains a source-runtime replacement and advances camera generation according to existing restart semantics.

Ordered files are deliberately more conservative: capability/source changes that would compromise ordered-work semantics continue to use the full camera/file lifecycle rather than transparent live-style branch replacement.

`MAX_CAMERAS = 512` remains a schema/registry validation bound, **not** a capacity claim.

---

# 6. CameraIngest and physical source ownership

Primary implementation:

```text
fight/pipeline_mp/camera_ingest.py
```

```text
                     +--> independently gated Fight branch
Physical source ---> CameraIngest
                     +--> independently gated Speed branch
                     +--> Preview
```

CameraIngest is the sole source/decode/reconnect owner in the Supervisor-managed production path.

Neither Fight-consumer replacement nor Vehicle/Fight shared-service replacement is permission to reopen the physical source.

## 6.1 Independent branch gating

Fight and Speed branch queues are allocated for the source runtime and are gated independently.

Fight publication uses the parent-owned Fight pause/epoch state:

- if Fight is paused/disabled, ingest does not offer Fight frames as active work;
- if Fight is active, frame/signal publication is tagged with the current per-slot Fight consumer epoch;
- Speed publication remains governed by its own Speed stop/epoch semantics;
- Preview remains independent.

A disabled Fight branch is not counted as a dropped Fight frame merely because the branch is intentionally not offered.

This separation is what allows CameraIngest to stay alive while `camera_worker` is replaced.

## 6.2 Ordered-file EOF — Phase 17 remains authoritative

For a non-looping local file, each `CameraRuntime` owns a fresh `multiprocessing.Event` representing authoritative EOF.

CameraIngest sets the Event **before** delivering consumer EOF signals.

```text
legitimate non-looping file EOF
 -> set generation-local EOF Event
 -> publish branch EOF signal(s)
 -> consumers may drain/exit
 -> manager/watchdog may classify clean completion
```

Telemetry does not own EOF correctness.

The EOF Event belongs to one camera/source generation. Fight consumer epoch does **not** replace source generation for EOF ownership.

## 6.3 Ordered Fight publication observes consumer withdrawal

Ordered Fight frame/EOF/error publication now observes the Fight stop/pause guard in addition to global source stop. This prevents a blocked ordered publication from remaining stuck behind a full Fight queue after that Fight consumer has been intentionally withdrawn.

This is a shutdown/recovery liveness hardening only. It does not authorize ordered-file replay or lost-work continuation.

## 6.4 Live reconnect ownership and stop-aware backoff

Live reconnect remains CameraIngest-owned.

Reconnect backoff is now stop-aware in normal production execution: when the standard sleep path is used, CameraIngest waits on the global stop Event rather than sleeping uninterruptibly through the whole retry delay.

The reconnect delay resets only after real frame flow begins, preventing a repeated “open succeeds, no frame arrives” sequence from continually resetting the backoff.

Required ownership rules:

```text
Fight failure      -> no source reopen
Vehicle failure    -> no source reopen
Fight consumer swap-> no source reopen
Speed consumer swap-> no source reopen
source failure     -> CameraIngest reconnect/recovery path
```

A genuinely failed source may still require source-level watchdog recovery according to existing policy; that is distinct from Fight/Vehicle recovery.

## 6.5 Browser preview ownership

Common-runtime preview consumes runtime-produced output and does not independently open `camera.source` in Supervisor mode.

Preview may be restarted locally if its own process fails, without turning Fight/Speed recovery into source recovery.

---

# 7. CameraRuntimeManager ownership model

Primary implementation:

```text
fight/pipeline_mp/camera_lifecycle.py::CameraRuntimeManager
```

Each `CameraRuntime` now tracks source-level and consumer-level state separately.

Important state includes conceptually:

```text
camera / source identity
slot_id
generation
stop_event
file_eof_event
fight_queue
preview_queue
speed_queue

Fight:
  fight_pause
  fight_stop
  fight_epoch
  fight_failed
  fight_failure_reason
  fight_service_waiting
  fight_restarts
  fight_last_restart

Speed:
  speed_stop
  speed_epoch
  speed_failed
  speed_service_waiting
  speed_restarts
  speed_last_restart
```

Normal process composition:

```text
Fight-only:
  CameraIngest + camera_worker + Preview

Speed-only:
  CameraIngest + speed_worker + Preview

Fight + Speed:
  CameraIngest + camera_worker + speed_worker + Preview
```

The key Phase-20/21 rule is that these processes no longer share one indivisible recovery lifetime.

---

# 8. Identity model and stale-work fencing

The runtime now has several orthogonal incarnation dimensions.

## 8.1 Camera/source identity

```text
(camera_id, slot_id, generation)
```

Generation identifies the camera/source runtime incarnation.

Generation changes for true source/runtime replacement such as removal/re-add, source change, or full camera restart. It does **not** change merely because a Fight consumer is replaced under the same healthy source runtime.

## 8.2 Fight consumer identity

```text
(slot_id, fight_consumer_epoch)
```

`CameraRuntimeManager` owns a spawn-safe shared `fight_epochs` Array with one counter per stable slot.

Every new Fight consumer increments the slot’s Fight epoch before spawning `camera_worker`.

This identity is required because Phase 20/21 intentionally permits:

```text
same camera generation
same source owner
same or replacement shared Fight service
old camera_worker F1 -> replacement camera_worker F2
```

Generation alone can no longer distinguish F1 from F2.

## 8.3 Speed consumer identity

```text
(slot_id, speed_epoch)
```

Speed consumer epoch remains independent and changes only when the Speed consumer is invalidated/replaced according to Speed lifecycle rules.

A Fight-only failure must not increment Speed epoch.

## 8.4 Shared-service identity

```text
Fight service epoch
Vehicle service epoch
```

A shared Fight bundle replacement advances Fight service epoch.
A Vehicle replacement advances Vehicle service epoch.

Service epoch does not replace camera generation or consumer epoch.

## 8.5 Fight publication-floor vector

The Fight publication floor now protects two independent boundaries:

```text
publication_floor[0]
  = minimum acceptable shared Fight service epoch

publication_floor[slot_id + 1]
  = minimum acceptable Fight consumer epoch for that camera slot
```

Shared Fight failure advances the service floor before old transport is torn down.
Failed Fight-consumer withdrawal advances that slot’s consumer floor before replacement.

This allows buffered/late work to finish physically without allowing it to become current durable truth.

---

# 9. Fight identity adapters

Primary implementation:

```text
fight/pipeline_mp/fight_identity.py
```

## 9.1 `FightGenerations`

`FightGenerations` wraps the ordinary slot-generation array plus the Fight publication-floor vector.

Generation validation remains the first identity boundary. When `generation.is_current_generation(...)` receives a wrapper that exposes `allows(message)`, the message must satisfy both:

```text
message.generation == current slot generation
AND
message.consumer_epoch >= current per-slot Fight publication floor
```

This lets existing generation-validation points reject a stale Fight consumer without redefining camera generation.

## 9.2 `FightChannel`

`FightChannel` is a spawn-safe queue/channel adapter carrying one expected Fight consumer epoch.

On publication it tags correctness-relevant payloads with that epoch; ReportMessage rows receive `consumer_epoch` in the row, while dataclass messages receive the field directly.

On result consumption it loops past results from older/different consumer epochs and returns only the exact expected incarnation, preserving the caller’s bounded timeout behavior.

It delegates controlled queue capabilities such as `observe`/capacity metadata to the wrapped channel rather than creating a parallel scheduling model.

The existence of a `qsize()` delegate does not authorize new OS-queue correctness semantics; the architecture rule against `qsize()/empty()` lifecycle correctness remains binding.

---

# 10. Fight inference ownership and end-to-end incarnation propagation

Fight consumer epoch must follow the entire correctness-relevant path.

## 10.1 Camera worker -> Person

```text
CameraIngest frame tagged with Fight epoch
 -> camera_worker for that exact epoch
 -> Person request tagged with consumer_epoch
 -> shared Person worker preserves consumer_epoch in result
 -> router/result channel
 -> replacement camera_worker accepts only its exact epoch
```

An old F1 request may still be present physically after F1 is withdrawn, but generation/Fight-floor checks and exact result-channel filtering prevent its output from becoming F2 work.

## 10.2 Pose

Pose requests/results preserve the same Fight consumer epoch semantics.

The camera-local Pose temporal state belongs to the current camera_worker incarnation and is replaced when that Fight consumer is replaced.

## 10.3 Stage3

Stage3 work carries:

```text
generation
slot_id
Fight consumer epoch
Fight service epoch
```

The shared service epoch protects shared-bundle replacement.
The consumer epoch protects camera-worker replacement within the same source generation.

These are independent fences and both remain relevant.

## 10.4 Health and reports

Fight camera-worker health/status/report messages carry the Fight consumer epoch.

Old F1 heartbeats/progress/stopped/failure/summary events must not overwrite the current F2 consumer’s health interpretation merely because camera generation is unchanged.

Ingest/Preview continue to use source generation; Speed continues to use its Speed epoch; shared services continue to use their service epoch.

No universal epoch replaces those ownership-specific identities.

---

# 11. Fight evidence and incident identity

A camera-local Fight event counter restarts when `camera_worker` is recreated. Therefore a segment/evidence identity based only on:

```text
camera + local counter
```

can collide after Fight-only recovery even when the source generation remains stable.

Dynamic Fight evidence IDs now include source generation and Fight consumer incarnation, conceptually:

```text
<camera>_g<generation>_f<fight_consumer_epoch>_<local_counter>
```

Example shape documented by Phase 20/21 review:

```text
camera_g<generation>_f<incarnation>_000001
```

This prevents local counter reuse across Fight-consumer replacements from overwriting/reusing earlier evidence identity.

Repository review found downstream consumers treat this segment/evidence identifier as an opaque identifier rather than parsing the old exact textual structure. The durable outbox UUID/external incident identity remains a separate identity. No Django schema change was required.

Any future code that parses the textual Fight segment-ID layout would violate this contract unless deliberately introduced and documented.

---

# 12. Durable Fight publication fencing

Primary implementation:

```text
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/generation.py
fight/pipeline_mp/fight_identity.py
```

The IncidentAggregator validates both shared-service and consumer-incarnation publication floors.

Conceptually a Stage3 result is current only when:

```text
result.service_epoch >= publication_floor[0]
AND
result.consumer_epoch >= publication_floor[result.slot_id + 1]
```

for messages that carry a valid slot.

The aggregator retains a locked recheck immediately before durable publication so a floor change racing with buffered incident state cannot allow a failed incarnation to cross into the durable outbox after it has been invalidated.

This preserves the Phase-15 rule:

```text
old/failed incarnation may finish physically
!=
old/failed incarnation may publish current incident truth
```

Healthy ordinary capability withdrawal is different: already-admitted healthy incident work is not automatically invalidated merely because Fight capability was intentionally removed. Failure fencing and ordinary drain semantics remain separate.

---

# 13. Shared inference services and capability lifecycle

Primary implementation:

```text
fight/pipeline_mp/shared_services.py::SharedServices
```

Required capabilities:

```text
fight   = any enabled desired camera requiring Fight
vehicle = any enabled desired camera requiring Speed
```

Fight bundle:

```text
person
person_router
pose + pose_router   when configured
stage3               when configured
```

Vehicle bundle:

```text
vehicle
```

Runtime-global Incident and Reporter remain outside these bundles.

Behavior:

```text
first demand -> hot-start required bundle
continued demand -> reuse service incarnation
last demand removed -> idle grace / required drain
normal withdrawal -> bounded Phase-19 graceful finalization
failure withdrawal -> bounded forced teardown, no poisoned-transport drain
unrelated capability change -> no global runtime restart
```

Default shared-service idle grace remains 5 seconds.

Configured-off Pose/Stage3 is expected absence, not health failure.

---

# 14. Phase 20/21 shared Fight-service recovery

Recoverable shared Fight components remain:

```text
person
person_router
pose
pose_router
stage3
```

The old Phase-15 coupling was safe but coarse: `suspend_fight()` invalidated camera generation and stopped the entire camera runtime, so a mixed camera lost CameraIngest, Preview, Speed and Speed-local state while the Fight bundle recovered.

Phase 20/21 removes that unnecessary coupling.

## 14.1 New recovery sequence

```text
shared Fight component failure detected
 -> preserve concrete component/reason/pre-teardown exit identity
 -> raise shared Fight publication floor
 -> mark Fight service unavailable/restarting
 -> pause affected Fight branch publication
 -> advance failed Fight-consumer publication floors
 -> signal/withdraw only affected camera_worker consumers
 -> boundedly verify Fight consumer/frame transport can be reused safely
 -> keep CameraIngest alive
 -> keep Preview alive
 -> keep Speed consumer alive
 -> keep Vehicle service alive
 -> tear down failed Fight shared transport without graceful finalization
 -> bounded Fight-service retry/backoff
 -> start fresh Fight bundle
 -> advance Fight service epoch
 -> resume only still-desired eligible LIVE Fight consumers
 -> each resumed consumer receives a fresh Fight consumer epoch
```

The camera/source generation does not change solely because the Fight bundle changed.
The Speed epoch does not change solely because Fight changed.

## 14.2 Mixed LIVE survival matrix

For an otherwise healthy mixed LIVE camera:

```text
Component / identity         Shared Fight recovery
--------------------------------------------------------------
CameraIngest                 survives; same source owner
Preview                      survives
Speed worker                 survives; local state preserved
Vehicle service              survives
camera generation            unchanged
Speed consumer epoch         unchanged
Fight camera_worker          replaced
Fight consumer epoch         advances
Fight service epoch          advances when bundle replaced
```

Preserving the Speed process preserves the state it owns, including tracker/calibration/speed estimator/cooldown/history/evidence-buffer state.

## 14.3 Unsafe withdrawal fails closed

A killed Fight reader can leave multiprocessing frame transport in an unsafe/poisoned state. The parent pauses Fight publication and performs a bounded probe/drain of the Fight frame queue.

If safe withdrawal cannot be established, `CameraLifecycleError` is raised. The runtime prefers fail-closed behavior over attaching a second Fight consumer to suspect transport or starting a duplicate source reader.

No recovery path is allowed to briefly create a second CameraIngest as a swap technique.

---

# 15. Camera-local Fight consumer recovery

A `camera_worker` failure is distinct from shared Person/Pose/Stage3 failure.

For a LIVE camera with healthy shared Fight services:

```text
camera_worker failure
 -> pause/fence that camera's Fight branch
 -> stop/remove only that Fight consumer
 -> keep CameraIngest
 -> keep Preview
 -> keep Speed/Vehicle on mixed camera
 -> bounded local retry/cooldown
 -> start replacement camera_worker
 -> advance Fight consumer epoch
 -> preserve camera generation
```

The local retry policy reuses existing bounded watchdog camera restart settings:

```text
watchdog_camera_restart_limit        default 3
watchdog_camera_restart_cooldown_sec default 120
```

Spawn failures consume the same bounded local Fight recovery accounting; exhaustion leaves the Fight branch degraded/failed rather than restarting unrelated Speed/source state forever.

Ordered FILE Fight consumers do **not** use this transparent LIVE reattach path after failure. File failure remains incomplete/no-replay.

---

# 16. Speed inference ownership and recovery

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

## 16.1 Vehicle recovery

Confirmed Vehicle failure remains:

```text
observe original cause
 -> capture pre-teardown Vehicle exit code when already available
 -> mark Vehicle unavailable
 -> withdraw affected Speed consumers
 -> invalidate Speed consumer epochs
 -> tear down old Vehicle transport
 -> bounded backoff
 -> fresh Vehicle transport + service epoch
 -> resume eligible LIVE Speed consumers
```

Defaults remain:

```text
VEHICLE_SERVICE_RESTART_LIMIT=3
VEHICLE_SERVICE_RESTART_BACKOFF_SEC=2
VEHICLE_SERVICE_RESTART_MAX_BACKOFF_SEC=30
```

Fight-consumer incarnation changes do not alter Vehicle ownership or epoch semantics.

## 16.2 Speed-consumer spawn failure edge case

Phase-20/21 final review found a case where enabling Speed could fail during spawn and leave the capability enabled without a process or effective retry.

A missing Speed process caused by spawn failure now participates in the existing bounded LIVE Speed retry budget/cooldown. Ordered FILE semantics remain conservative.

This fix changes neither Vehicle recovery budgets nor shared Vehicle model ownership.

---

# 17. Capability churn during recovery

Desired-state reconciliation remains authoritative while recovery is pending.

## 17.1 Fight removed during Fight recovery

If a mixed camera loses Fight capability while the shared Fight service is backing off:

- do not recreate its Fight consumer after service recovery;
- keep Speed/source/Preview alive where still desired;
- if global Fight demand disappears, the shared Fight bundle follows normal idle/graceful retirement rules.

## 17.2 Speed added during Fight recovery

If Speed is added while Fight is unavailable:

- Vehicle starts if globally required;
- Speed consumer can start independently on the existing valid LIVE source runtime;
- Speed does not wait for Fight recovery merely because the same camera also wants Fight.

## 17.3 Camera removed during recovery

Removal invalidates the source generation and tears down the runtime according to normal removal semantics.

Pending Fight recovery state must not resurrect the removed camera.

## 17.4 Source changed during recovery

Source change is a real source-runtime transition.

It advances camera generation/replaces the camera runtime according to existing restart semantics. Old Fight-consumer/service recovery identity cannot attach to the replacement source generation.

## 17.5 Fight re-enabled

Re-enabling Fight creates exactly one eligible current Fight consumer when dependencies are available. It receives the current source generation and a fresh Fight consumer epoch.

## 17.6 Global stop during recovery

`CameraRuntimeManager.stopping` combines parent stop state with the global stop Event. Pending local/shared consumer recreation checks this condition.

Global stop prevents new consumer/service work from being started after shutdown begins.

---

# 18. Live source failure overlapping Fight recovery

Source and Fight recovery have separate owners.

```text
source unavailable
  -> CameraIngest reconnect/source-health path

Fight service unavailable
  -> SharedServices Fight-bundle recovery

Fight consumer unavailable
  -> CameraRuntimeManager Fight-consumer lifecycle
```

An overlapping source outage does not give Fight recovery permission to reopen the source.

When source and Fight service recover, current desired state and current source generation determine which consumers are eligible to exist.

No distributed transaction is required between source reconnect and shared-service recovery; correctness comes from explicit ownership plus generation/consumer/service fencing.

---

# 19. Ordered-file semantics after Phase 20/21

FILE priority remains:

```text
correctness + ordering > freshness
```

Required rules:

- ordered Fight/Speed work waits/defer instead of silently dropping required inference;
- authoritative EOF is the generation-local Event;
- CameraIngest sets EOF before branch EOF delivery;
- clean required consumer completion requires authoritative EOF + exit code `0`;
- mixed Fight+Speed file completion waits for every required branch;
- pre-EOF Fight failure remains failure/incomplete;
- pre-EOF Speed failure remains failure/incomplete;
- later EOF cannot rewrite a previously failed branch as successful;
- Fight shared-service failure affecting FILE work remains incomplete/no-replay;
- local Fight-consumer failure does not transparently attach a replacement consumer to continue possibly-lost ordered work;
- source is not reopened merely to recover Fight;
- already processed Speed frames are not replayed merely because Fight failed;
- benchmark deadline truncation remains `INCOMPLETE`, never successful throughput.

Phase-20/21 ordered error-publication hardening only makes withdrawal responsive to the Fight stop guard; it does not change these correctness semantics.

---

# 20. Health architecture and partial capability outage

Primary files:

```text
fight/pipeline_mp/health.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/camera_lifecycle.py
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

Health identity follows component ownership:

```text
CameraIngest / Preview:
  camera generation

Fight camera_worker:
  camera generation + Fight consumer epoch

Speed worker:
  camera generation + Speed consumer epoch

Fight shared workers:
  Fight service epoch

Vehicle:
  Vehicle service epoch
```

## 20.1 Partial capability outage

A mixed camera may legitimately be in a state like:

```text
CameraIngest: healthy
Preview:      healthy
Speed:        healthy
Fight:        waiting / service restarting
aggregate:    degraded
```

The runtime must not claim the whole camera is healthy while Fight is unavailable, but it also must not mark Speed failed solely because Fight is unavailable.

Intentional Fight suspension is distinguishable from unexpected `camera_worker` death, preventing the watchdog from launching a competing full-camera restart during shared Fight recovery.

## 20.2 Stale Fight health

Old Fight-consumer health events cannot overwrite the replacement consumer when camera generation remains unchanged. Fight consumer epoch participates in health identity/fencing.

This closes a gap that camera generation alone could no longer solve once source-preserving Fight replacement became legal.

## 20.3 Source health remains independent

Frame starvation/source failure remains an ingest/source concern. A Fight-service outage does not fabricate source failure, and a source outage does not by itself justify replacing Person/Pose/Stage3 or Vehicle.

---

# 21. Phase 19.1 Windows health snapshot reliability

The Phase-19.1 snapshot contract remains unchanged.

Primary implementation:

```text
fight/pipeline_mp/health.py::HealthSnapshotStore
fight/pipeline_mp/run_multiprocess.py
```

Only Windows atomic-replacement errors with:

```text
winerror 5
winerror 32
winerror 33
```

receive retry.

Policy:

```text
attempt 1 immediate
sleep 20 ms
attempt 2
sleep 40 ms
attempt 3
sleep 80 ms
attempt 4 or raise
maximum added sleep: 140 ms
```

Guarantees:

- complete temporary JSON exists before replacement;
- previous complete snapshot remains until replacement succeeds;
- persistent/unrelated filesystem errors still surface;
- snapshot publication failure remains best-effort/non-fatal;
- status includes exception class plus `errno`/`winerror`;
- health snapshot output never becomes lifecycle correctness state.

Phase 20/21 tests keep these regressions green.

---

# 22. Failure cause preservation

Shared-service status preserves the failure observed before teardown.

Vehicle retains conceptually:

```text
detail
component
reason
component_failure
retries
service_epoch
exit_code
```

Fight shared-service failure retains equivalent failing-component identity.

Examples:

```text
process already dead with exit 7
 -> preserve exit_code=7

inference stall while process still alive
 -> original exit_code=None
 -> later forced-kill code is NOT rewritten as the original cause
```

Camera-local Fight-consumer failure is a different failure class from Person/Pose/Stage3 shared-service failure and must not be conflated with it.

---

# 23. Phase 19 graceful shared-worker finalization

Normal healthy capability retirement remains distinct from failure recovery.

`SharedServices.FINALIZE_TIMEOUT_SEC` remains:

```text
8.0 seconds
```

This is one common budget per service bundle, not eight seconds per worker.

Normal withdrawal:

```text
bundle no longer required / clean runtime close
 -> bounded sentinel publication to alive admission workers
 -> workers exit normal loops and publish summaries
 -> join within remaining common deadline
 -> set bundle stop
 -> bounded terminate/kill fallback if still needed
 -> close transport
```

Failure recovery:

```text
failed/poisoned service
 -> NO graceful drain of failed transport
 -> bounded forced teardown/replacement
```

Clean dynamic runtime exit preserves ordering:

```text
camera producers finish
 -> shared services finalize normally
 -> shared workers publish final summaries
 -> Reporter sentinel
 -> Reporter final flush/exit
 -> performance_summary.json construction
```

Phase-20/21 Fight-consumer isolation does not merge these two paths.

---

# 24. Performance and attribution identity

Primary files:

```text
fight/pipeline_mp/attribution.py
fight/pipeline_mp/performance.py
benchmarks/real_inference.py
benchmarks/telemetry.py
```

Performance populations remain distinct:

- camera-local call timings,
- shared-worker queue/inference/result-enqueue timings,
- worker all-request distributions,
- worker steady-state distributions,
- batch distributions,
- per-camera attribution,
- host/process/GPU samples.

Phase 20/21 additionally requires consumer-incarnation awareness where source generation can stay stable while Fight consumer changes.

Current summary behavior retains the latest compatible consumer incarnation per camera and latest relevant service epoch rather than pooling pre/post-recovery timing populations as though they were one incarnation.

Important metric distinctions remain:

- `queue_wait_inclusive_ms` is not pure post-enqueue queue delay;
- frame-delivery age is not pure IPC-copy latency;
- detector-call wall time is not CUDA-kernel duration;
- mean of run-level p95 values is not a pooled p95;
- per-request inference and per-batch inference are different populations;
- missing metrics are unavailable, not zero.

Attribution remains bounded, best-effort and non-correctness.

---

# 25. Person microbatching contract

Production defaults remain:

```text
person_batch_enabled = false
person_batch_size = 1
person_batch_max_wait_ms = 0
```

Batching is an optional execution profile, not a correctness requirement and not a universal optimization.

Phase 20/21 does not change these defaults.

---

# 26. Fair scheduling and bounded capacity

Primary implementation:

```text
fight/pipeline_mp/scheduling.py::FairRequestQueue
```

Shared fair stages remain Person, Pose, Stage3 and Vehicle.

Per-slot bounded pending work plus round-robin dispatch prevents one hot camera from monopolizing a shared FIFO.

Capacity/accounting includes conceptually:

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

Fight consumer epoch fencing augments stale-work identity; it does not replace fair-scheduler capacity accounting.

Live may shed stale work explicitly. Ordered files wait/defer. Dropped/stale work must not be converted into a synthetic negative detection.

---

# 27. Incident durability boundary

Primary files:

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/speed_worker.py
```

Runtime produces evidence and durable incident envelopes before Django ingestion.

Core semantics:

- append-only JSONL incident outbox,
- serialized writers,
- flush/fsync before success where designed,
- partial-tail preservation,
- persistence failure surfacing,
- evidence durability before incident publication,
- camera-generation fencing,
- Fight consumer-epoch fencing,
- Fight shared-service epoch/publication-floor fencing,
- Speed generation + consumer-epoch fencing.

Runtime never directly creates Incident ORM rows.

Reporter, performance attribution and health snapshots are not durable incident truth.

---

# 28. Django incident/application domain

Primary application entities include common Incident, routing rules/routes, audit events, ingest cursor and ingest records.

Incident types include:

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

Phase 20/21 changes internal Fight segment/evidence identity, not the external Django Incident schema.

Evidence deletion must continue to respect Incident references and retention protections.

---

# 29. Location and authorization

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

Fight and Speed share one physical authorization/location domain.

Organizational access decisions remain application-layer concerns, not inference-worker concerns.

---

# 30. Runtime durability, retention and generated artifacts

Phase-12 durability remains binding.

Supervisor/desired state is durable/atomic where designed, outbox/evidence durability is explicit, cleanup scans are bounded, active/unknown/abnormal runs fail closed against unsafe cleanup, Incident-referenced evidence is protected, evidence retention is indefinite by default unless explicitly configured, disk pressure is observable without restart storms, and long-running operational jobs use singleton service loops/locks.

Generated runtime/benchmark content is not source code.

Known local operational paths include:

```text
.runtime_supervisor/
Fight_backend_project/backend_frontend_project/media/camera_uploads/
Fight_backend_project/backend_frontend_project/media/pipeline_runs/.run_locks/
Fight_backend_project/backend_frontend_project/media/runtime_spool/
benchmarks/results/
benchmarks/.capacity.lock
phase*_review.diff
```

These must remain out of source commits.

---

# 31. Capacity benchmark subsystem

Primary files:

```text
benchmarks/__main__.py
benchmarks/control_plane.py
benchmarks/real_inference.py
benchmarks/telemetry.py
benchmarks/README.md
```

## 31.1 Measurement taxonomy

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
 - no real inference-capacity claim
```

Synthetic camera-equivalents must never be called real inference capacity.

## 31.2 Phase 20/21 control-plane lifecycle semantics

The synthetic control-plane transition model now reflects source-preserving LIVE capability changes.

A same-live-source capability transition is considered safe when:

- the same `CameraRuntime` remains current,
- camera generation stays unchanged,
- ingest and Preview identity stay stable,
- unaffected Fight identity stays stable where appropriate,
- affected Speed branch presence matches desired state,
- peer camera-runtime objects remain unchanged.

A source change remains a true generation/source-runtime transition.

Synthetic checks remain structural correctness tests, not RTSP/network or inference-capacity evidence.

## 31.3 Real-mode isolation and classification

Real benchmark mode continues to reuse the common Supervisor/runtime and preserve production model/queue/recovery behavior.

Classification remains:

```text
HEALTHY
PRESSURED
SATURATED
INCOMPLETE
```

Observed recovery, required-consumer failure, non-zero runtime exit, deadline truncation, missing required reports/samples or failed runtime health can make a run `INCOMPLETE`.

Phase 20/21 identity changes do not alter benchmark classification rules.

---

# 32. Measured RTX 3050 characterization evidence

Development characterization hardware:

```text
NVIDIA GeForce RTX 3050 Laptop GPU, 6 GB
Intel Core i7-13700H
64 GB RAM
Windows
```

All numbers below are workload-specific characterization, not production capacity guarantees.

## 32.1 Fight-only selected points

```text
cameras   aggregate FPS
2         28.67
4         49.89
8         65.55
12        76.80
```

Healthy post-Phase-17 Fight-12:

```text
903 frames/camera
10,836 total
Person/Pose/Stage3: 5004 / 3732 / 72
Person queue p95: ~206.9 ms
Pose queue p95: ~132.1 ms
GPU mean: ~57.5%
GPU p95: ~86%
VRAM: ~640 MiB
recovery/replay/restart: none
```

This is “12-camera workload characterized”, not “12 cameras supported”.

## 32.2 Speed-only Phase-18 attribution points

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

8 -> 12 camera count rises 50%; aggregate throughput rises only:

```text
(89.25 / 82.70 - 1) * 100 = 7.9201935%
```

This indicates a throughput knee with rising queue pressure and no simple raw-GPU-saturation explanation. It does not isolate one exclusive bottleneck or prove IPC-copy causality.

## 32.3 Mixed Fight + Speed context

A healthy Phase-18 Mixed-8 run observed approximately:

```text
aggregate FPS: 47.76
Person requests/results: 2600 / 2600
Vehicle requests/results: 1344 / 1344
Person queue p95: ~205 ms
Vehicle queue mean/p95: ~14.8 / 45.3 ms
Vehicle inference mean/p95: ~24.5 / 34.3 ms
CPU mean: ~45.3%
GPU mean/max: ~33.2% / 73%
recovery/drop/rejection: none at relevant shared-admission boundaries
```

The traffic content produced no meaningful Pose/Stage3 work, so this is not a full Fight-event contention workload.

---

# 33. Phase 19 microbatch characterization

Two healthy OFF vs Batch-2 Mixed-8 comparison pairs exist.

Pair A:

```text
OFF
  aggregate FPS: 40.99
  wall: 132.32 s
  Person queue mean/p95: 171.57 / 197.40 ms

Batch-2 / 5 ms
  aggregate FPS: 45.15
  wall: 120.12 s
  Person queue mean/p95: 105.22 / 132.13 ms
  actual batch mean: 1.83
```

Pair B after Phase 19.1:

```text
OFF
  aggregate FPS: 42.370409
  wall: 128.013870 s
  Person queue mean/p95: 166.882547 / 206.519370 ms

Batch-2 / 5 ms
  aggregate FPS: 43.578609
  wall: 124.464735 s
  Person queue mean/p95: 102.568315 / 132.865525 ms
  actual batch mean/p95: 1.988281 / 2
```

Arithmetic means across the two pairs:

```text
aggregate FPS:
  OFF 41.6802045
  B2  44.3643045
  +6.4397477%

wall time:
  OFF 130.166935 s
  B2  122.2923675 s
  -6.0495912%

Person queue mean:
  OFF 169.2262735 ms
  B2  103.8941575 ms
  -38.6063669%

mean of run-level Person queue p95:
  OFF 201.959685 ms
  B2  132.4977625 ms
  -34.3939547%
```

The final statistic is **not a pooled p95**.

Batch-4 lowered Person queue pressure further but worsened Vehicle contention and gave slightly lower total mixed throughput than Batch-2 in its experiment.

The global default remains OFF.

---

# 34. Post-19.1 real Windows validation

Final Mixed-8 OFF and Batch-2 runs after Phase 19.1 both completed HEALTHY with no observed recovery.

Each recorded:

```text
Person accepted/dispatched: 2600 / 2600
Vehicle accepted/dispatched: 1344 / 1344
rejected_capacity: 0 at Person/Vehicle
shared-stage dropped_live: 0
shared-stage stale_generation: 0
Person restart_count: 0
Vehicle restart_count: 0
health_snapshot_write_failed: 0
```

This validates the targeted Windows snapshot hardening under those runs. It does not prove transient sharing errors can never recur.

---

# 35. Phase ledger — Phase 1 through Phase 19.1

## Phase 1 — Shared Person

One shared Person model/worker; camera-local Motion/stabilizer/tracking/pair/ROI/event/prebuffer; explicit request identity.

## Phase 2 — Shared Pose

One shared Pose service/router; camera-local Pose temporal interpretation; duplicate incident-side model ownership removed later.

## Phase 3 — Performance observability

Bounded timing/queue/inference/delivery metrics and machine-readable summaries.

## Phase 4 — Microbatch capability

Latency-bounded batching support while conservative defaults keep batching disabled unless configured.

## Phase 5 — Centralized CameraIngest

One source/decode owner feeding Fight/Preview and later Speed; ordered-file vs live freshness policy.

## Phase 6 — Runtime Supervisor

Standalone authenticated local owner with durable state/PID/config and Windows-aware stop behavior.

## Phase 7 — Organization/access

Location/SecurityUnit/Coverage/UserAssignment and `Camera.location`.

## Phase 8 — Durable incidents/routing

Evidence + outbox -> independent Django dispatcher -> common Incident/routing domain; runtime ORM-free.

## Phase 9 — Dynamic lifecycle

Desired revisions, stable slots, camera generations, in-parent add/remove/restart.

## Phase 10 — Health/watchdog

Bounded health events, HealthRegistry/Watchdog, atomic runtime-health snapshots.

## Phase 11 — Fair scheduling/capacity

Per-slot bounded admission/round-robin fairness; live shedding vs file defer semantics.

## Phase 12 — Operational durability/retention

Bounded cleanup/retention, locks, disk-pressure health, serialized/fsynced durable writes and resilient service loops.

## Phase 13 — Shared Speed integration

Baseline:

```text
418f65bf137cfb31aa92629ac8fe1e03a0a1c54a
feat: integrate speed detection into shared runtime
```

Established Fight-only/Speed-only/Fight+Speed modes, one CameraIngest fan-out, shared Vehicle inference, camera-local Speed state, bounded Vehicle admission, Speed generation+epoch fencing, file fail-closed/live recovery distinction and common durable Speed incidents.

## Phase 14 — Capability lifecycle + Vehicle recovery

Baseline:

```text
7b395ef94f04b435862ab013f9226b0982de34b6
feat: add capability-aware shared service lifecycle
```

Established `SharedServices`, capability-aware Fight/Vehicle bundle lifetime, Vehicle crash/stall/start-failure recovery, transport replacement/service epoch, bounded retry/backoff, live Speed resume and file no-replay.

## Phase 15 — Resilient shared Fight recovery

Baseline:

```text
c3c019b2871dcd3891b24ef242a2c5e93fe9212f
feat: add resilient fight service recovery
```

Established whole-Fight service-incarnation replacement, Fight service epoch, Stage3 epoch propagation, publication floor, LIVE resume, FILE fail-closed/no-replay and bounded shared-service recovery.

## Phase 16 — Capacity benchmark harness

Baseline:

```text
022d2fd5a3a6cef4ece0ac1b7434b9b9493512a0
feat: add capacity benchmark harness
```

Added real-vs-synthetic separation, real Supervisor/runtime reuse, synthetic parent-structure tests, bounded telemetry, isolated results and explicit classification semantics.

## Phase 17 — Ordered-file EOF/watchdog hardening

Baseline:

```text
3b9733f20a3ca4d3773c63fed0caba7939f2f27c
fix: harden ordered file EOF lifecycle
```

Established authoritative generation-local EOF Event, EOF-before-signal ordering, clean consumer exit requirement, no false replay/restart and mixed-branch completion correctness.

## Phase 18 — Bottleneck attribution

Baseline:

```text
823f87da3b4663e915085da8fd2145043e84175a
feat: add bottleneck attribution telemetry
```

Added bounded best-effort ingest/Fight/Vehicle/Speed/Preview attribution without changing correctness or classification.

## Phase 19 — Shared-worker telemetry finalization

Production code included in:

```text
fefedcc81095a5b808f048d1bc7daa13e04c0b1f
Finalize Phase 19 shared worker telemetry and Windows health reliability
```

Established bounded sentinel-based healthy service finalization, Reporter ordering and retention of worker/batch timing populations.

## Phase 19.1 — Windows snapshot + failure-reason hardening

Same production baseline as Phase 19.

Established bounded Windows atomic-replace retries, last-good snapshot preservation and pre-teardown shared-service failure identity.

---

# 36. Phase 20/21 — Live recovery hardening and Fight-Speed isolation

Production baseline:

```text
cbeb45066e8d25b6b6d2eeeda757e61b5e23e562
Finalize Phase 20/21 live recovery and Fight-Speed isolation
```

## 36.1 Trigger

The prior shared Fight recovery was safe but over-coupled:

```text
SharedServices detects Fight failure
 -> CameraRuntimeManager.suspend_fight()
 -> camera generation advanced / camera-wide stop
 -> ingest + Fight + Speed + Preview terminated
 -> camera restart
```

On a mixed LIVE camera this discarded healthy Speed state and reopened/recreated source-side processes even though only Fight inference had failed.

Generation and shared Fight service epoch alone were also insufficient for the new desired model because a local Fight consumer can now be replaced while both source generation and shared service incarnation remain unchanged.

## 36.2 New architectural boundary

Phase 20/21 establishes a dedicated Fight consumer incarnation.

```text
camera generation
  = source runtime identity

Fight consumer epoch
  = camera_worker identity within that source generation

Speed consumer epoch
  = speed_worker identity

Fight service epoch
  = shared Person/Pose/Stage3 bundle identity

Vehicle service epoch
  = shared Vehicle identity
```

These identities are not aliases.

## 36.3 Established guarantees

```text
Fight branch can be paused independently of source/Speed/Preview
Fight camera_worker can be withdrawn independently
mixed LIVE Fight recovery preserves CameraIngest
mixed LIVE Fight recovery preserves Preview
mixed LIVE Fight recovery preserves Speed process/state
mixed LIVE Fight recovery preserves Vehicle service
camera generation stays stable for Fight-only recovery
Speed epoch stays stable for Fight-only recovery
Fight consumer epoch advances on Fight consumer replacement
shared Fight epoch advances on shared Fight bundle replacement
Fight request/result/health/report paths carry consumer identity
Stage3/Incident path carries consumer + service identity
publication floor protects both shared-service and per-consumer failure boundaries
local Fight-consumer death uses bounded Fight-only recovery
FILE Fight failure remains incomplete/no-replay
same-source LIVE capability transitions preserve source runtime
source change remains true camera-generation replacement
live reconnect backoff becomes stop-aware
reconnect delay resets after actual frame flow
Fight evidence IDs include generation + Fight consumer incarnation
Speed spawn failure joins existing bounded LIVE retry policy
ordered error publication observes Fight withdrawal guard
Windows spawn compatibility retained
Person batching default unchanged/OFF
```

## 36.4 Test evidence

High-value automated coverage includes:

- mixed LIVE shared Fight failure preserving ingest/Preview/Speed/Vehicle identity;
- Speed state/progress continuing while Fight recovers;
- Fight-only LIVE recovery preserving source/Preview;
- Speed-only camera unaffected by Fight recovery;
- local camera_worker failure recovering only the Fight side;
- stale Person/Pose results rejected across Fight consumer replacement;
- stale Fight health rejected;
- stale Stage3 and buffered incident publication fenced;
- evidence-ID uniqueness across consumer reincarnation;
- capability removal/re-enable during recovery;
- camera removal during recovery;
- source change during recovery;
- global stop during recovery/backoff;
- ordered FILE no-replay/fail-closed behavior;
- interrupted ordered error publication;
- bounded consumer-spawn failure handling;
- Speed spawn retry cooldown/exhaustion;
- Phase-19 graceful finalization regression;
- Phase-19.1 snapshot/failure-identity regression;
- real `multiprocessing.get_context("spawn")` recovery path using generated frames/stub detectors.

Final validation:

```text
152 passed + 26 subtests focused
236 passed + 26 subtests full suite
compileall passed
git diff --check passed
```

---

# 37. Manual LIVE / RTSP qualification contract

Automated tests are not a substitute for actual RTSP/network acceptance.

`benchmarks/README.md` defines a manual development-runtime procedure. At minimum a real LIVE qualification should establish:

1. Start one mixed Fight+Speed camera through the normal Supervisor path.
2. Record camera generation, Fight consumer epoch, Fight service epoch, Speed epoch, Vehicle epoch and process identities.
3. Verify exactly one CameraIngest/source owner.
4. Terminate only the verified current Person child in an isolated development runtime.
5. Observe Fight service restarting with preserved root cause.
6. Confirm CameraIngest PID remains stable.
7. Confirm Preview PID remains stable.
8. Confirm Speed PID/state/progress remain stable.
9. Confirm Vehicle PID/service epoch remain stable.
10. Confirm Fight camera worker is replaced with a new Fight consumer epoch.
11. Confirm camera generation and Speed epoch do not advance solely because Fight failed.
12. Temporarily interrupt only the test source/network.
13. Verify CameraIngest owns reconnect/backoff and no competing Fight/Vehicle replacement is triggered merely by frame absence.
14. Restore source and verify progress resumes.
15. Remove/re-enable Fight capability and verify no unnecessary source/Speed/Preview restart.
16. Repeat capability churn during Fight recovery backoff.
17. Stop the Supervisor during recovery and verify no replacement starts after global stop.
18. Preserve logs/output in a new result directory; never overwrite historical evidence.

Production credentials/URLs must not be committed as qualification fixtures.

---

# 38. Task router for coding agents

Read this document first. Inspect the current Supervisor-managed ownership path before editing. Do not infer production architecture from legacy helpers.

## Supervisor / desired state

```text
fight/runtime_supervisor/core.py
fight/runtime_supervisor/camera_state.py
fight/runtime_supervisor/locking.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/fight_runner.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
```

## Source ownership / CameraIngest / reconnect

```text
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/camera_preview.py
fight/pipeline_mp/health.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/common_preview.py
tests/test_camera_ingest.py
tests/test_live_fight_isolation.py
```

## Dynamic lifecycle / consumer identities / capability churn

```text
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/fight_identity.py
fight/pipeline_mp/generation.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/health.py
fight/pipeline_mp/shared_services.py
tests/test_dynamic_camera_lifecycle.py
tests/test_live_fight_isolation.py
tests/test_runtime_health.py
tests/test_speed_integration.py
```

## Fight inference / consumer fencing

```text
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/fight_identity.py
fight/pipeline_mp/generation.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/scheduling.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
tests/test_fight_service_recovery.py
tests/test_live_fight_isolation.py
```

## Shared Fight recovery

```text
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/health.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/fight_identity.py
fight/pipeline/incident_aggregator.py
tests/test_shared_services.py
tests/test_fight_service_recovery.py
tests/test_live_fight_isolation.py
```

## Speed / Vehicle recovery

```text
fight/pipeline_mp/speed_worker.py
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/health.py
HizTespiti/speed/src/*
HizTespiti/yolo/src/vehicle_detector.py
tests/test_speed_integration.py
tests/test_shared_services.py
tests/test_live_fight_isolation.py
```

## Ordered-file EOF

```text
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/health.py
fight/pipeline_mp/run_multiprocess.py
tests/test_camera_ingest.py
tests/test_dynamic_camera_lifecycle.py
tests/test_runtime_health.py
tests/test_live_fight_isolation.py
```

## Incident durability / publication floors

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/fight_identity.py
fight/pipeline_mp/generation.py
incidents/services/ingest.py
incidents/services/retention.py
incidents/models.py
tests/test_fight_service_recovery.py
tests/test_live_fight_isolation.py
```

## Health / snapshot / restart ownership

```text
fight/pipeline_mp/health.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/core.py
tests/test_runtime_health.py
tests/test_health_snapshot_reliability.py
tests/test_live_fight_isolation.py
```

## Graceful finalization / Reporter / performance

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

## Attribution / capacity characterization

```text
fight/pipeline_mp/attribution.py
fight/pipeline_mp/performance.py
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_preview.py
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/speed_worker.py
benchmarks/README.md
benchmarks/control_plane.py
benchmarks/real_inference.py
benchmarks/telemetry.py
tests/test_attribution_telemetry.py
tests/test_capacity_benchmarks.py
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

## Location / authorization / Django domain

```text
adminx/models.py
streams/models.py
services/access_scope.py
incidents/models.py
incidents/services/ingest.py
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

# 39. Cross-phase acceptance checklist

Before accepting architecture-affecting work verify, as applicable:

```text
[ ] ARCHITECTURE.md was read first and coding agent did not edit it
[ ] current Supervisor-managed ownership path was inspected, not inferred from legacy helpers
[ ] one Runtime Supervisor still owns the global AI runtime
[ ] one CameraIngest remains the intended source/decode/reconnect owner per physical camera
[ ] no recovery path introduces a second temporary source owner
[ ] Fight+Speed on one source still use one camera entry/fan-out
[ ] no runtime Django ORM dependency or model-per-camera regression was introduced
[ ] camera-local temporal/calibration state stayed local
[ ] shared services start only when desired capabilities require them
[ ] capability transitions avoid unnecessary global runtime restart
[ ] same-source LIVE capability changes preserve source generation where safe
[ ] source changes remain true camera-generation transitions
[ ] optional disabled services remain healthy/service_disabled
[ ] camera/source generation retains source-incarnation meaning
[ ] Fight consumer epoch retains camera_worker-incarnation meaning
[ ] Speed consumer epoch retains speed_worker-incarnation meaning
[ ] shared service epochs retain service-incarnation meaning
[ ] Fight publication floor retains shared-service and per-consumer fencing
[ ] stale Fight Person/Pose results cannot become replacement-consumer work
[ ] stale Fight health cannot overwrite replacement consumer state
[ ] stale Stage3/Incident work cannot cross durable publication floors
[ ] Fight evidence identity cannot collide solely because local event counter restarted
[ ] Vehicle recovery remains isolated from Fight
[ ] shared Fight recovery preserves healthy mixed-camera Speed where safe
[ ] shared Fight recovery preserves CameraIngest/Preview where safe
[ ] camera-local Fight failure does not recycle shared Fight bundle unnecessarily
[ ] camera-local Fight recovery is bounded
[ ] unsafe Fight withdrawal fails closed rather than attaching duplicate consumer/source
[ ] capability removal during recovery cannot resurrect Fight
[ ] camera removal during recovery cannot resurrect runtime
[ ] source change during recovery cannot attach stale consumer to new generation
[ ] global stop prevents pending recovery recreation
[ ] live reconnect remains CameraIngest-owned and stop-aware
[ ] reconnect delay is not reset merely by open-without-frame failure
[ ] authoritative non-looping EOF remains generation-local correctness state
[ ] CameraIngest publishes authoritative EOF before consumer EOF
[ ] clean Fight/Speed file exit requires authoritative EOF + exitcode 0
[ ] failed ordered Fight work is not transparently resumed/replayed
[ ] later EOF cannot relabel a failed file branch as clean
[ ] mixed FILE completion waits for all required consumers
[ ] ordered Fight publication can observe consumer withdrawal without changing no-replay rules
[ ] fair per-slot scheduling/capacity remains bounded
[ ] no new correctness dependency on OS qsize()/empty()
[ ] health state remains bounded and identity-aware
[ ] intentional Fight suspension does not trigger competing whole-camera restart
[ ] source failure and consumer/service failure remain distinct ownership classes
[ ] health snapshot retry remains limited to winerror 5/32/33
[ ] persistent/unrelated snapshot errors still surface
[ ] pre-teardown service failure cause remains preserved
[ ] normal shared-service withdrawal uses Phase-19 bounded graceful finalization
[ ] failure/recovery teardown skips graceful finalization of poisoned transport
[ ] Reporter final flush precedes final performance summary
[ ] worker timing/batch populations remain distinct from client distributions
[ ] attribution remains bounded/best-effort/non-correctness
[ ] incompatible incarnation timing populations are not pooled as current
[ ] durable outbox remains runtime->Django truth boundary
[ ] Speed still uses Incident(type=SPEED) in common Incident domain
[ ] location/security authorization remains application-layer
[ ] retention/disk-pressure protections remain fail-safe/bounded
[ ] Windows spawn compatibility is tested for new multiprocessing state
[ ] Person batching remains OFF by default
[ ] synthetic tests are not called real inference capacity
[ ] automated generated-frame spawn tests are not called real RTSP qualification
[ ] one GPU's measurements are not extrapolated into another GPU's camera count
[ ] benchmark code does not tune production behavior
[ ] generated runtime/benchmark artifacts are not staged
[ ] UI/PostgreSQL/Docker/Nginx/deployment remain frozen unless explicitly promoted
[ ] shared-memory transport is introduced only after controlled evidence justifies it
```

---

# 40. Deferred / frozen work after Phase 20/21

The major structural mixed-camera recovery coupling addressed by the previous contract is no longer deferred; Phase 20/21 implements source-preserving Fight recovery.

Backend/runtime work worth promoting deliberately now includes:

- real external LIVE/RTSP qualification using the documented fault-injection procedure;
- long soak tests covering reconnect, source loss, capability churn, shared-service recovery and storage growth;
- repeated Fight recoveries while Speed actively tracks real vehicles, to validate long-lived state behavior outside stub detectors;
- target-hardware RTX 5090 characterization using the actual machine rather than extrapolation;
- mixed Fight+Speed workloads that actually exercise Pose and Stage3 rather than traffic-only content;
- representative Fight decision-quality and Speed calibration/accuracy validation;
- realistic duplicate-incident/temporal validation across repeated consumer/service recovery;
- storage/evidence sizing under long-running live workloads;
- controlled Vehicle service optimization only if the 8->12 queue/throughput knee remains important on target hardware;
- shared-memory transport only if a controlled experiment isolates transport overhead as material;
- model-worker concurrency/partitioning only after target-hardware service-time/queue evidence justifies it;
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

# 41. Capacity and production qualification strategy

The broad RTX 3050 scale sweep, Phase-18 attribution and Phase-19 microbatch experiments have already answered the first-order laptop questions:

```text
Fight and Speed throughput show diminishing returns at larger counts.
Speed 8 -> 12 is not explained by obvious raw GPU saturation alone.
Mixed load creates cross-service contention.
Batch-2 can reduce Person queue pressure on the tested Mixed-8 traffic workload.
Batch-4 can over-optimize Person while worsening Vehicle contention.
```

Phase 20/21 answers a different correctness question:

```text
Can Fight fail/recover on a mixed LIVE camera without destroying healthy source,
Preview, Speed and Vehicle ownership?
```

Automated architecture tests say yes under deterministic spawned-process conditions. The next required evidence is **real live/network qualification**, not another blind local-file scale sweep.

On production-target hardware/live sources, evaluate at minimum:

```text
single-source ownership
camera generation continuity
Fight consumer epoch behavior
Speed epoch continuity
Fight/Vehicle service epoch behavior
source reconnect count/backoff
Fight/Speed progress before/during/after fault
partial capability health state
stale/drop/rejection/defer counters
incident/evidence identity across recovery
CPU/RAM and GPU utilization/VRAM
queue/inference/result-enqueue distributions
wall-clock live latency
network jitter/loss/reconnect behavior
long-soak child-process leakage/restart storms
storage/outbox/evidence growth
```

Optimization decision rule remains evidence-driven:

```text
If shared model service time/queueing dominates:
    evaluate batching, worker concurrency or model partitioning.

If camera-local CPU work dominates:
    optimize that subsystem first.

If transport-related evidence remains dominant after controlling backlog/fanout:
    run a shared-memory experiment before adopting it.

If one shared service harms another in mixed load:
    optimize total-system behavior, not one queue in isolation.

If target GPU behavior differs from RTX 3050:
    prefer target measurements over laptop tuning conclusions.
```

No camera-count claim belongs in this contract without a clearly described real workload, hardware, configuration, duration and acceptance criterion.

---

# 42. Current architectural summary

The current production architecture at `cbeb45066e8d25b6b6d2eeeda757e61b5e23e562` can be summarized as:

```text
ONE Runtime Supervisor
ONE global multiprocessing parent
ONE CameraIngest/source owner per physical camera

PER CAMERA:
  source generation
  optional Fight consumer incarnation
  optional Speed consumer epoch
  Preview

SHARED:
  Person
  optional Pose
  optional Stage3
  Vehicle

FENCING:
  camera generation
  Fight consumer epoch
  Speed consumer epoch
  Fight service epoch
  Vehicle service epoch
  Fight shared-service publication floor
  Fight per-consumer publication floor

RECOVERY:
  source failure      -> source/CameraIngest ownership path
  local Fight failure -> Fight consumer only when safe
  shared Fight failure-> Fight bundle + Fight consumers only when safe
  Vehicle failure     -> Vehicle + affected Speed consumers
  Preview failure     -> Preview-local recovery
  Reporter/Incident   -> runtime-global failure boundary

FILE:
  authoritative EOF
  ordered/fail-closed
  no replay after affected failure

LIVE:
  freshness-oriented
  bounded recovery
  source-preserving capability/consumer replacement where safe
```

The next architecture-affecting work should preserve these boundaries unless a new phase explicitly replaces them with stronger, measured, and tested guarantees.
