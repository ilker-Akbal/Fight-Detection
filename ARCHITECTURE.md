# Fight-Detection Architecture Contract

## Document ownership and maintenance discipline

This file is the architecture contract for the repository. It describes the **current Supervisor-managed production architecture**, ownership and failure boundaries, identity/fencing rules, live-vs-file semantics, durability boundaries, health/recovery behavior, shutdown ownership, qualification methodology, performance interpretation, measured characterization evidence, and intentionally deferred work.

**Maintenance rule:** coding agents (including Codex) must read this file before architecture-affecting work and **must not modify it**. The project owner and ChatGPT maintain it from committed repository state.

Architecture refreshes are not a narrow “append the latest commit” exercise. Before changing this file, the maintainer must inspect the committed implementation and tests for the affected ownership paths, remove stale/contradictory claims, keep measurements separate from estimates, and preserve inherited invariants unless a later accepted phase explicitly changes them.

Current production-code reference commit:

```text
57dc5db9393f9791184fa4433501588ebe016634
Integrate operator UI, live preview and offline analysis
```

**Phases 23, 24, 25 and 25.1 are committed on `master` and form the current production-code architecture baseline.** They retain the Phase-20/21 identity/recovery isolation and Phase-22 Supervisor/Windows ownership model while promoting three previously deferred product concerns into accepted architecture:

- one consolidated operator workspace over the existing authorization/runtime domains,
- source-preserving LIVE preview that remains available when AI analytics are paused,
- a separate explicit one-shot historical-video analysis domain that reuses the same runtime/shared model services without treating uploaded media as live cameras.

Phase 25.1 hardens the historical Fight path: compact deterministic evidence names avoid the reproduced Windows-long-path failure, source/video-relative time is kept distinct from wall clock, evidence serialization failure is not misclassified as an inference-consumer crash, and ordered FILE completion remains authoritative-EOF/drain based.

Final reported validation for the current code baseline:

```text
full pytest:
  278 passed, 26 subtests passed

compileall:
  fight benchmarks tests Fight_backend_project/backend_frontend_project
  passed

Django:
  manage.py check
  no issues

  manage.py makemigrations --check --dry-run
  no changes detected

git diff --check:
  passed (Windows LF/CRLF conversion warnings only)
```

Manual local acceptance additionally exercised the Supervisor-managed LIVE HTTP/MJPEG path and an explicit historical Fight run. The historical source reported 903 frames and the accepted run consumed 903/903 frames, reached authoritative ingest EOF and Fight EOF, opened/closed 8/8 Fight events, submitted 6 Stage3 jobs and produced one historical Fight result without `save_failed`, `camera_process_dead`, `required_consumer_failed` or `evidence_write_failed`.

That manual result is an acceptance observation, **not** a throughput/capacity claim. Generated/local HTTP/MJPEG testing is still not proof of real RTSP/network behavior, long-duration stability, target-hardware capacity, or historical Speed correctness with a representative calibration. Real RTSP interruption and long soak remain environment-specific qualification work.

---

# 1. System purpose and current topology

The repository is a centralized multi-camera security platform with two distinct application workflows that share one AI runtime:

1. **LIVE monitoring** — persistent camera/source definitions, continuous preview, optional Fight and/or Speed analytics and live Incident/routing behavior.
2. **OFFLINE / historical analysis** — private uploaded/local media represented by `OfflineAsset`, processed only through explicit one-shot `OfflineRun` jobs and persisted as `OfflineResult`.

Uploaded media is not a live camera. The separation is a product/domain boundary, not a second model-runtime topology.

```text
Django / application control plane
    |
    +--> LIVE Camera registry (source_kind=LIVE)
    |      -> Camera Registry Reconciler
    |      -> durable desired LIVE camera state
    |
    +--> Historical media domain
    |      -> OfflineAsset
    |      -> explicit OfflineRun request
    |      -> durable one-shot job state
    |
    +--> Runtime Supervisor
           -> one global run_multiprocess parent
               -> SharedServices over current LIVE demand + active offline demand
               -> CameraRuntimeManager
                    LIVE source:
                      one CameraIngest source/decode owner
                      + Preview always while monitorable
                      + Fight consumer when required
                      + Speed consumer when required

                    explicit OFFLINE run:
                      one one-shot CameraIngest for that run identity
                      + required Fight/Speed consumer(s)
                      + ordered FILE EOF/drain semantics

               -> parent-owned private PreviewGateway
                    camera_preview -> bounded latest JPEG
                    -> loopback bearer-protected transport
                    -> Django authorization proxy
                    -> browser

               -> runtime-global Reporter + Incident worker
               -> bounded health/performance/attribution reporting
    |
    +--> durable incident outbox
           -> Django Incident Dispatcher
                LIVE envelope    -> Incident ORM -> routing/authorization
                offline_*        -> OfflineResult -> historical UI only
```

The production design is deliberately **not** “one complete AI pipeline per camera” and is also **not** “one AI runtime per historical job”. Expensive/stateless inference remains shared across current demand. Temporal interpretation that depends on one camera/run history remains local to that consumer.

The runtime distinguishes separate lifetimes:

```text
SOURCE / CAMERA INCARNATION
  CameraIngest + source + Preview + camera generation

FIGHT CONSUMER INCARNATION
  camera_worker + Fight-local temporal state + Fight consumer epoch

SPEED CONSUMER INCARNATION
  speed_worker + tracking/calibration/speed state + Speed consumer epoch

FIGHT SERVICE INCARNATION
  Person / router / optional Pose/router / optional Stage3
  -> Fight service epoch

VEHICLE SERVICE INCARNATION
  Vehicle worker/model
  -> Vehicle service epoch

OFFLINE RUN IDENTITY
  persisted OfflineRun UUID
  -> runtime camera_id = offline_<uuid.hex>
  -> one-shot durable claim/state
  -> no transparent replay under a new parent
```

A LIVE Fight-consumer replacement is not a source replacement. A shared Fight-service replacement is not a camera-generation replacement. A Speed replacement is not a Fight replacement. An explicit historical re-analysis is a **new OfflineRun identity**, not a replay of an old one.

Target scale remains large multi-camera deployment, but this contract does **not** assert a production camera-count guarantee. RTX 3050 results are characterization evidence for specific workloads, not an RTX 5090 estimate, sustained RTSP SLA, or proof that 200/300 real cameras fit one node.

---

# 2. Non-negotiable invariants

1. Runtime workers must not depend on Django ORM.
2. Django/Gunicorn is the application/control plane, not the AI child-process owner.
3. Runtime Supervisor owns the global production AI runtime lifecycle.
4. `run_multiprocess` owns multiprocessing topology below the Supervisor.
5. One active runtime source has exactly one intended `CameraIngest` decode/source owner for its camera/run incarnation.
6. Fight and Speed on the same LIVE physical camera share one desired camera entry and one CameraIngest decode path.
7. Recovery must never create a second CameraIngest as a workaround.
8. `camera_worker`, `speed_worker`, Preview, PreviewGateway and web views must not independently reopen the production LIVE source.
9. Expensive/stateless inference models are shared services, not model-per-camera or model-per-offline-run instances.
10. Fight temporal tracking/pair/ROI/event state and Speed tracking/calibration/speed state remain camera/run-local where history matters.
11. Shared services are capability-aware and run only while current LIVE or active OFFLINE demand requires them.
12. Capability changes do not require a global runtime restart solely because capabilities changed.
13. Source/camera incarnation is fenced by stable slot + camera generation.
14. Fight consumer incarnation adds a separate parent-owned Fight epoch when the Fight consumer can change without changing source generation.
15. Speed consumer identity uses its own parent-owned epoch.
16. Recoverable shared services use service epochs where required.
17. Fight durable publication is fenced by shared-service and per-Fight-consumer publication floors.
18. Old work may physically finish, but stale work cannot become current health/result/durable-publication truth.
19. LIVE and ordered-file workloads intentionally use different backpressure/recovery semantics.
20. Correctness must not depend on OS `Queue.qsize()` or `Queue.empty()` observations.
21. Telemetry, queues, retries, health scans, preview caches and cleanup remain bounded.
22. Windows `spawn` compatibility is a first-class requirement.
23. Runtime incident truth crosses to Django through the durable incident outbox; runtime workers do not create Django ORM rows.
24. LIVE Fight and Speed share the same live Incident/routing/authorization domain.
25. OFFLINE Fight and Speed results belong to the historical `OfflineResult` domain and must not masquerade as current live Incidents.
26. Optional service absence is healthy when that service is not required.
27. Incident and Reporter remain runtime-global liveness-critical processes.
28. Synthetic camera-equivalents are not production inference capacity.
29. One GPU/workload result must not be linearly extrapolated to another GPU/workload.
30. Benchmark/qualification code must not silently retune production thresholds, batching, calibration or ownership merely to obtain better results.
31. PostgreSQL, Docker, Nginx and deployment/service packaging remain frozen unless explicitly promoted. The Phase-23/24/25 operator/preview/offline UI is accepted current architecture; **further** redesign is frozen unless promoted.
32. Shared-memory transport remains deferred until controlled measurement proves transport is materially limiting.
33. Ordered non-looping file EOF is correctness state, not telemetry.
34. CameraIngest sets authoritative generation-local EOF before consumer EOF signals.
35. A dead required file consumer is clean only with authoritative EOF + exit code 0.
36. Clean EOF does not advance generation, reopen source, replay the file or synthesize watchdog recovery.
37. Fight/Vehicle failures during ordered-file work remain fail-closed/no-replay.
38. Attribution/performance telemetry never drives health, admission, epochs, recovery, EOF, durable publication or benchmark/qualification correctness.
39. Missing/disabled/no-sample metrics remain null/unavailable rather than fabricated zero.
40. Normal graceful finalization remains distinct from failure teardown.
41. Final telemetry is best-effort observability, not durability or EOF truth.
42. Health snapshot publication failure is non-fatal but observable; retry stays bounded and narrow.
43. Failure reporting preserves the pre-teardown cause when available.
44. Forced-kill exit codes must not be misreported as the original stall cause.
45. Person microbatching remains configurable but **OFF by default**.
46. Fight recovery preserves healthy mixed-camera Speed state whenever source/Speed ownership is healthy.
47. Camera-local Fight failure does not automatically recycle the shared Fight bundle.
48. Shared Fight-bundle failure does not automatically recycle CameraIngest, Preview, Speed or Vehicle.
49. Global stop is authoritative: reconnect/recovery/backoff may not recreate work after shutdown begins.
50. Runtime parent owns orderly child teardown. Group signals must not allow children to bypass parent-owned shutdown sequencing.
51. Qualification fault injection is opt-in and may target only processes verified to belong to the current qualification run.
52. Qualification output directories are immutable-by-convention: do not reuse/overwrite an existing run directory.
53. Generated/live-like input must be explicitly labeled synthetic and must never be presented as RTSP/network proof.
54. A monitorable LIVE camera may remain in desired state with Fight=false and Speed=false; that is valid **preview-only** operation, not “camera stopped”.
55. LIVE desired-camera publication includes only `source_kind=LIVE` Camera rows. Uploaded/local historical files are not live desired cameras.
56. The primary LIVE preview path is lossy/latest-frame, source-preserving and non-blocking; preview slowness must not backpressure CameraIngest or analytics.
57. PreviewGateway is private runtime transport: loopback-only, bearer-protected, bounded and run/generation aware. Django remains the browser authorization boundary.
58. Pausing LIVE analytics withdraws Fight/Speed branches while preserving monitorable LIVE CameraIngest + Preview where possible.
59. LIVE analytics pause does not cancel or rewrite an already-running historical OfflineRun.
60. Technical hard stop is distinct from analytics pause: it may stop acquisition/preview and interrupt current historical work; automatic monitoring is held until explicitly resumed.
61. Uploading historical media creates an `OfflineAsset`, not a new LIVE Camera.
62. Historical analysis starts only from an explicit `OfflineRun`; re-analysis creates a new run UUID/runtime identity.
63. Offline job ownership/state is durable. A different runtime parent may fail an interrupted processing claim but must not silently replay it.
64. Current offline request transport is intentionally single-outstanding-job; no multi-job concurrency guarantee is implied.
65. Successful OfflineRun completion requires ordered required-consumer completion, authoritative FILE EOF and downstream drain acknowledgement; Django does not mark completion until its dispatcher cursor reaches the acknowledged outbox offset.
66. Offline cancellation/failure is terminal for that run identity; recovery never reopens the same partial run as if it were clean.
67. Offline event position is source/video-relative metadata. A relative position such as 5.53 seconds must never be formatted as a 1970 wall-clock timestamp.
68. Offline evidence filenames use compact deterministic identity-derived names; the full offline UUID must not be redundantly repeated into long filesystem paths.
69. Auxiliary Fight clip serialization failure is not an inference-consumer crash. If Stage3 can continue from in-memory frames it may do so, while the evidence error remains explicit.
70. Missing/failed required historical evidence must not be reported as a successful historical result. The run may finish ordered inference/drain and then fail explicitly with an evidence error.
71. Historical playback/evidence remains private and authorization-scoped; raw local filesystem paths are not a browser authorization mechanism.
72. Modern Location/SecurityUnit/UserSecurityAssignment scope remains fail-closed for non-admin users. Profile/faculty does not silently grant modern location camera access; legacy fallback exists only for legacy location-less camera records.

---

# 3. Ownership planes

## 3.1 Django/application plane

Django owns persisted LIVE `Camera` configuration, `SpeedCameraConfig`, Location/security organization, `OfflineAsset` / `OfflineRun` / `OfflineResult`, live Incident ORM rows, routing/audit/ACK/resolve state, LIVE desired-camera publication, historical job request/state reconciliation, Supervisor control requests, operator-facing status/preview/action endpoints, protected historical media, retention/reference protection and the independent Incident Dispatcher service loop.

Django classifies source intent before publication:

```text
Camera.source_kind = LIVE
  -> eligible for LIVE desired-camera registry

uploaded/local historical file
  -> OfflineAsset / explicit OfflineRun
  -> not a LIVE desired camera
```

Django may observe runtime health and consume preview/evidence. It must not become a hidden second AI runtime or open a production camera source in web requests. `RUNTIME_CONTROL_MODE=direct` remains compatibility/rollback behavior, not the production ownership contract.

Authorization is centralized through the existing access-scope services. Modern non-admin physical scope derives from active `UserSecurityAssignment -> SecurityUnit -> SecurityUnitCoverage -> Location`; browser preview, events, historical assets and protected media re-check the appropriate scope.

## 3.2 Runtime Supervisor

Primary implementation:

```text
fight/runtime_supervisor/core.py::RuntimeSupervisor
```

The Supervisor owns exactly one common `fight.pipeline_mp.run_multiprocess` parent in normal operation.

States:

```text
STOPPED
STARTING
RUNNING
STOPPING
FAILED
BACKOFF
```

Local state under `.runtime_supervisor/` is operational data, not source code.

The Supervisor owns process-group start/stop semantics, desired LIVE camera state, runtime status/health projection and the final bounded fallback when the runtime parent does not stop within the configured grace period.

## 3.3 Runtime parent

Primary implementation:

```text
fight/pipeline_mp/run_multiprocess.py
```

The runtime parent owns:

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
- LIVE desired-state reconcile,
- `OfflineJobs` one-shot job ownership when configured,
- the private `PreviewGateway` and its bounded latest-frame channel when enabled,
- health registry/watchdog/snapshot publication,
- fair admissions,
- ordered-file EOF/drain/finalization,
- performance summary,
- orderly child shutdown.

The dynamic runtime calculates service demand over:

```text
current LIVE desired cameras
+ the currently PROCESSING offline runtime camera, if any
```

so an explicit historical run reuses the current shared models rather than launching a second model topology.

Lifecycle policy remains parent-owned. Children receive only spawn-safe queues, Events/Arrays, simple identities and configuration required for their role.

## 3.4 Preview transport boundary

`PreviewGateway` is owned by the runtime parent but is **not** a source owner or AI service. It keeps only the newest valid JPEG per slot/generation, listens on loopback, requires a per-run bearer token and publishes a private descriptor outside Django static/media paths.

Django's `live_preview` bridge validates the active run/descriptor, connects only to loopback, validates bounded JPEG/stream payloads and periodically re-checks user authorization while streaming. Browser clients never receive the gateway bearer token.

## 3.5 Durable dispatcher boundary

The runtime writes durable outbox envelopes without importing Django. The independent Django dispatcher decides the application domain:

```text
camera_id starts with offline_
  -> OfflineResult import
  -> no live Incident row / route

otherwise
  -> LIVE Incident import
  -> normal routing / authorization domain
```

This split is part of the durability contract, not merely UI filtering.

---

# 4. Identity and stale-work fencing

## 4.1 Camera/source generation

Base source identity:

```text
(camera_id, slot_id, generation)
```

Generation changes when the source/runtime incarnation changes: remove/re-add, source change or full camera/source restart.

Generation must **not** change merely because a LIVE Fight consumer or shared Fight bundle was replaced while the source stayed alive.

## 4.2 Fight consumer epoch

Phase 20/21 introduced a parent-owned per-slot Fight consumer epoch because camera generation may remain stable while `camera_worker` changes.

Primary helper:

```text
fight/pipeline_mp/fight_identity.py
```

`FightChannel` tags request/result/report traffic with the current Fight consumer epoch and rejects results from older consumer incarnations. `FightGenerations` extends generation checks with the per-slot Fight publication floor.

The Fight epoch is distinct from both camera generation and Fight service epoch.

## 4.3 Speed consumer epoch

Speed uses its own epoch. Replacing/stopping the Speed consumer advances the Speed epoch without implying a Fight or source transition.

## 4.4 Shared-service epochs

Fight and Vehicle bundles each have service incarnation identity. Shared worker health/results from older service epochs cannot overwrite current service truth.

## 4.5 Fight publication floor

Fight durable publication uses a vector conceptually containing:

```text
index 0            -> minimum current Fight service epoch
index slot_id + 1  -> minimum current Fight consumer epoch for that slot
```

Stage3/Incident output must satisfy current generation plus the applicable service/consumer floors before durable publication.

Old Fight work may finish physically; it must fail closed logically.

---

# 5. Desired LIVE camera state and capability reconciliation

Primary files:

```text
fight/runtime_supervisor/camera_state.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/analytics_control.py
Fight_backend_project/backend_frontend_project/streams/source_kind.py
fight/pipeline_mp/camera_lifecycle.py
```

Desired camera schema version remains `1` with canonical camera fields including:

```text
camera_id
source
name
enabled
use_fight_detection
use_speed_detection
speed_config
```

and durable global LIVE intent:

```text
speed_paused
analytics_paused
```

The Django registry publishes only:

```text
Camera.is_active == true
AND Camera.source_kind == LIVE
AND runtime source exists
```

Fight/Speed are independent analytics capabilities, **not camera existence**. A desired LIVE camera is therefore valid in all four branch modes:

```text
Preview-only:  Fight=false, Speed=false
Fight-only:    Fight=true,  Speed=false
Speed-only:    Fight=false, Speed=true
Fight + Speed: Fight=true,  Speed=true
```

A preview-only camera keeps its CameraIngest/Preview runtime without loading Fight/Vehicle services solely because the source is present.

Speed is advertised only when the Camera requests Speed and its current enabled `SpeedCameraConfig` resolves a usable speed configuration/calibration.

Desired state has a monotonic revision. Stale/equal revisions must not create duplicate lifecycle work. Equal revisions with different cameras or pause intent are conflicts.

## 5.1 LIVE analytics pause vs technical stop

`analytics_paused=true` is durable **LIVE analytics intent**. The registry keeps monitorable LIVE cameras in desired state but publishes both analytics capabilities false while paused.

```text
analytics pause
 -> keep LIVE desired camera
 -> keep CameraIngest + Preview
 -> withdraw Fight/Speed branches
 -> shared models may idle if no other demand exists
```

An active historical OfflineRun is separate demand and may keep shared services alive while LIVE analytics are paused.

Technical hard stop is different: it sets the monitoring hold and stops the Supervisor runtime, including acquisition/preview and any in-flight historical runtime work. Explicit resume clears that hold.

## 5.2 Source-preserving LIVE capability changes

When source identity is unchanged and the source is LIVE/non-file, capability/config transitions can reconfigure branch consumers without replacing the source runtime solely because branch composition changed.

```text
same LIVE source
 + capability/config change
 -> preserve CameraRuntime/source generation
 -> preserve CameraIngest
 -> preserve Preview
 -> stop/start only affected Fight/Speed consumers
```

A genuine source change is a source-runtime replacement and advances camera generation.

Ordered files remain conservative and do not use transparent live-style consumer recovery after ordered work may have been lost.

## 5.3 Historical jobs are not desired LIVE cameras

`OfflineAsset` and `OfflineRun` are deliberately absent from the Supervisor's durable LIVE desired-camera list. During one active historical job, `OfflineJobs` exposes the job's synthetic runtime camera to the runtime parent only:

```text
live_cameras = durable LIVE desired set
combined     = live_cameras + offline.cameras()
```

`SharedServices.prepare(combined)` and `CameraRuntimeManager.reconcile(combined)` allow the one-shot job to reuse the same runtime without converting it into a persistent camera.

`MAX_CAMERAS = 512` remains a registry/schema bound, not a capacity claim.

---

# 6. CameraIngest, source ownership and preview

Primary implementations:

```text
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_preview.py
fight/pipeline_mp/preview_gateway.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/live_preview.py
```

For a monitorable LIVE source:

```text
                         +--> Fight branch when enabled
LIVE physical/network ---> CameraIngest
                         +--> Speed branch when enabled
                         +--> Preview always
```

CameraIngest is the sole source/decode/reconnect owner in the Supervisor-managed LIVE production path. Fight or Vehicle service recovery is never permission to open a second source.

For an explicit historical run, the same CameraIngest implementation is intentionally reused as the **single one-shot decoder for that OfflineRun identity**. This does not turn the asset into a LIVE Camera and does not permit web/preview consumers to reopen it independently.

## 6.1 Independent branch gating

Fight and Speed branch queues are allocated for the source runtime and gated independently.

Fight publication uses parent-owned Fight pause/epoch state. Disabled Fight is not counted as a dropped Fight frame merely because work was intentionally not offered. Speed uses its own stop/epoch semantics. Preview is independent.

This separation allows CameraIngest + Preview to remain alive while Fight and/or Speed consumers are withdrawn, replaced or intentionally paused.

## 6.2 Primary LIVE preview transport — Phase 24

The primary Supervisor preview path is in-memory and latest-frame oriented:

```text
CameraIngest preview queue (bounded/latest)
 -> camera_preview
 -> JPEG encode
 -> bounded preview_live_channel with replace-old semantics
 -> parent PreviewGateway cache (one current JPEG per slot/generation)
 -> private loopback authenticated stream
 -> Django authorization proxy
 -> browser
```

Properties:

- `camera_preview` never opens the physical/network source.
- The primary path does not require a per-frame disk write/read round trip.
- Preview is deliberately lossy: freshness wins over completeness.
- A slow/absent viewer cannot backpressure CameraIngest or ordered inference.
- Preview cache entries are slot/generation and frame-sequence fenced and expire when stale.
- The gateway is loopback-only, bearer-protected and bounded; it is not exposed as public media.
- Django re-checks current user/camera authorization during long-lived streams.
- Multi-camera browser preview uses one bounded multiplexed page stream for up to the configured page limit rather than one independent source connection per card.
- The old disk preview writer remains only as a fallback path when no live preview channel is supplied; it is not the primary Phase-24 browser path.

Historical playback does **not** use the LIVE PreviewGateway. It serves the original authorized asset through protected byte-range responses.

## 6.3 LIVE reconnect

Reconnect is CameraIngest-owned, bounded and exponential. Phase 20/21 made reconnect waits stop-aware so shutdown does not block on an uninterruptible sleep.

Reconnect delay resets only after real frame flow resumes, not merely after reopening a handle that still produces no frames.

Temporary source silence must not independently trigger Fight/Vehicle bundle replacement simply because no frames are arriving.

## 6.4 Ordered-file EOF

For a non-looping local file, each `CameraRuntime` owns a fresh generation-local `multiprocessing.Event` representing authoritative EOF.

```text
legitimate file EOF
 -> set EOF Event
 -> publish consumer EOF signal(s)
 -> consumers drain/exit
 -> manager classifies completion
```

A later Fight consumer epoch cannot replace or reinterpret source-generation EOF truth.

Blocked ordered Fight error/EOF publication observes Fight withdrawal/stop guards so an intentionally removed Fight reader cannot deadlock CameraIngest indefinitely.

For explicit OfflineRun work, the generic FILE rules are strengthened by one-shot job ownership and downstream drain/cursor completion described in Section 13.

---

# 7. CameraRuntimeManager lifecycle

Primary implementation:

```text
fight/pipeline_mp/camera_lifecycle.py::CameraRuntimeManager
```

Per-camera runtime state includes desired camera definition, slot/generation, source stop controls, Fight/Speed/Preview queues, generation-local file EOF, Fight epoch/pause/stop/failure/retry state, Speed epoch/stop/failure/retry state, process handles and lifecycle state.

Normal composition:

```text
Preview-only: CameraIngest + Preview
Fight-only:   CameraIngest + camera_worker + Preview
Speed-only:   CameraIngest + speed_worker + Preview
Fight+Speed:  CameraIngest + camera_worker + speed_worker + Preview
```

The same manager temporarily owns an explicit `offline_<run-uuid>` runtime camera. That runtime may also contain a Preview process as part of the common source lifecycle, but OFFLINE IDs are never exposed through the LIVE browser-preview authorization path.

## 7.1 Fight consumer replacement

A local Fight consumer replacement:

```text
pause Fight publication
 -> raise per-consumer floor when failure requires fencing
 -> stop old camera_worker
 -> prove old reader withdrawn / transport safe
 -> clear abandoned Fight frame transport only while paused
 -> increment Fight consumer epoch
 -> start replacement camera_worker
 -> resume Fight branch
```

Unsafe withdrawal fails closed rather than creating another reader/source.

For ordered FILE, local Fight failure is latched incomplete/no-replay rather than transparently reattached.

## 7.2 Shared Fight recovery

Confirmed shared Fight failure:

```text
capture failing component + pre-teardown cause/exit identity
 -> raise Fight service publication floor
 -> pause/fence affected Fight consumers
 -> preserve healthy CameraIngest/Preview/Speed/Vehicle
 -> terminate failed Fight transport without graceful finalization
 -> bounded retry/backoff
 -> create fresh Fight bundle + fresh service epoch
 -> attach only still-desired eligible LIVE Fight consumers
```

For a healthy mixed LIVE camera, expected identity behavior is:

```text
CameraIngest PID          preserved
Preview PID               preserved
Speed worker PID/state    preserved
Vehicle service           preserved
camera generation         preserved
Speed epoch               preserved
Fight camera_worker       replaced
Fight consumer epoch      advanced
Fight service epoch       advanced
```

## 7.3 Vehicle recovery

Vehicle recovery remains independent:

```text
capture Vehicle failure identity
 -> mark Vehicle unavailable
 -> withdraw affected Speed consumers
 -> invalidate Speed epochs
 -> replace Vehicle transport/service epoch
 -> bounded retry/backoff
 -> resume eligible LIVE Speed consumers
```

Fight remains alive where safe. Ordered FILE Speed is never replayed after partial failure.

## 7.4 Capability churn during recovery

Current desired state wins over pending recovery.

- Removing Fight during Fight recovery prevents Fight resurrection.
- Adding Speed can start Vehicle/Speed independently of Fight recovery when source state is valid.
- Removing a camera prevents pending recovery from recreating it.
- Source change creates a new camera generation; old recovery identity cannot attach to it.
- Re-enabling Fight creates exactly one current Fight consumer.
- Global stop prevents new recovery/reconnect children.

A Speed-consumer spawn failure is not silently left as “enabled but absent”; LIVE mode uses the existing bounded local retry policy.

## 7.5 Explicit OfflineRun lifecycle

An offline runtime identity is one-shot. `restart_camera()` treats `offline_*` specially: watchdog/source restart requests terminate/fail that run instead of reopening/replaying the asset. FILE Fight/Speed consumer failures remain latched and are not transparently reattached.

Only a new explicit OfflineRun may intentionally decode the asset again.


---

# 8. SharedServices and service lifecycle

Primary implementation:

```text
fight/pipeline_mp/shared_services.py::SharedServices
```

Required service bundles derive from enabled capabilities over **all current runtime demand**, not merely persistent LIVE cameras:

```text
runtime demand = current LIVE desired cameras + active offline runtime camera

fight   = any enabled demand requiring Fight
vehicle = any enabled demand requiring Speed
```

Fight bundle:

```text
Person
Person router
Pose + Pose router when configured
Stage3 when configured
```

Vehicle bundle:

```text
Vehicle worker/model
```

Runtime-global Incident and Reporter are outside these optional bundles.

Behavior:

```text
first demand -> hot-start bundle
continued demand -> reuse incarnation
last demand removed -> normal idle/graceful withdrawal
confirmed failure -> non-graceful bounded teardown + recovery
```

Default shared-service idle grace remains 5 seconds.

Normal service finalization retains the Phase-19 8-second shared grace budget per bundle. Failure/recovery does not reuse graceful finalization on potentially poisoned transport.

Consequences of the current product split:

- Preview-only LIVE cameras do not require Fight or Vehicle solely because the runtime exists.
- LIVE analytics pause may make both optional bundles idle.
- A concurrently processing historical job can legitimately keep Fight and/or Vehicle required while LIVE analytics are paused.
- When an offline job becomes terminal, `OfflineJobs` recomputes service requirements from LIVE cameras so historical-only demand does not leak.

---

# 9. Health architecture

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
 -> Supervisor/qualification observation
```

Health is bounded and epoch-aware.

Camera components include ingest, Fight worker, Preview and Speed worker. Shared records include Person/router, Pose/router, Stage3, Incident and Vehicle. Reporter remains runtime-global liveness-critical.

## 9.1 Stale health rejection

Camera worker health must match slot + generation. Fight health additionally matches Fight consumer epoch. Speed health additionally matches Speed epoch. Shared-worker health matches service epoch.

Old Fight consumer health therefore cannot overwrite replacement Fight consumer state while camera generation remains stable.

## 9.2 Phase-22 observation-only fields

Phase 22 exposes additional observation fields used by qualification, including current process IDs and ingest/Preview progress in the health/status projection.

These fields are **observation only**. They do not become recovery correctness state.

## 9.3 Windows snapshot reliability — Phase 19.1

`HealthSnapshotStore` retains the bounded Windows atomic-replace policy:

```text
retry only winerror 5 / 32 / 33
4 total replace attempts
delays 20 / 40 / 80 ms
maximum added sleep 140 ms
```

The old complete snapshot remains until replacement succeeds. Unrelated/persistent errors surface. Parent reporting includes errno/winerror. Snapshot publication remains best-effort/non-fatal.

## 9.4 LIVE operator projection vs offline job truth

Runtime health may contain temporary `offline_*` camera records because historical jobs reuse `CameraRuntimeManager`. Operator LIVE status intentionally excludes those IDs from LIVE camera cards and LIVE analytics-confirmation calculations.

Historical job completion/failure is proven by durable offline job state, FILE EOF and downstream drain/cursor state. Health/telemetry alone is never proof that an OfflineRun completed.


---

# 10. Runtime shutdown ownership and Windows hardening — Phase 22

Phase-22 qualification exposed a Windows process-group shutdown gap and established explicit ownership rules.

Primary files:

```text
fight/pipeline_mp/common.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/core.py
```

## 10.1 Runtime parent signal handling

The runtime parent handles:

```text
SIGINT
SIGTERM
SIGBREAK when available on Windows
```

Signal callbacks do **not** directly perform `multiprocessing.Event.set()`. A signal may interrupt Event code while the main thread holds a non-reentrant multiprocessing lock. Re-entering that lock from the callback can deadlock.

Instead, the first signal schedules one daemon helper thread which sets the parent stop Event outside the signal callback.

This preserves parent-owned orderly shutdown sequencing.

## 10.2 Runtime children and CTRL_BREAK

On Windows, Supervisor launches the runtime parent in a new process group and uses `CTRL_BREAK_EVENT` for graceful group stop.

Spawned runtime children enter through `_runtime_child_main`. When `SIGBREAK` exists, children ignore it:

```text
CTRL_BREAK broadcast
 -> runtime parent handles stop request
 -> children do not abort independently
 -> parent signals child Events / performs ordered teardown
 -> Phase-19 Reporter/shared-worker finalization remains reachable on normal shutdown
```

Ignoring group SIGBREAK does **not** make children independent owners. Parent teardown still terminates/kills children if required.

Non-Windows behavior remains guarded by platform/signal availability.

## 10.3 Windows timeout fallback

A final audit found a second edge case: if children correctly ignore SIGBREAK but the parent fails to finish before the Supervisor grace timeout, terminating the parent first can orphan those children. Once the root is gone, a later tree-kill may no longer discover the process tree.

The Windows fallback is now:

```text
send CTRL_BREAK_EVENT
 -> wait stop_grace_sec
 -> if parent still alive:
      taskkill /PID <runtime-parent> /T /F
      while the root still exists
 -> bounded command timeout = kill_grace_sec
 -> bounded wait for parent exit
 -> if still alive / tree stop fails: surface failure
```

The Supervisor must not report successful stop if the owned tree fallback fails.

POSIX behavior remains the existing process-group SIGTERM followed by SIGKILL fallback.

No shutdown timeout or recovery budget was increased by Phase 22.

---

# 11. Fight inference ownership

Person:

```text
camera_worker[N]
 -> fair per-slot Person admission
 -> one shared Person model/worker
 -> Person router
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
 -> shared Stage3/X3D worker
 -> Incident worker
 -> IncidentAggregator
```

Fight consumer epoch is propagated through correctness-relevant request/result/health/report/incident paths. Shared Fight service epoch remains a separate boundary.

Fight logical event identity includes source generation and Fight consumer incarnation so a restarted camera worker cannot reuse a local counter value as current work.

On-disk Fight evidence names use `fight/pipeline/evidence_metadata.py::compact_evidence_name`: a deterministic 128-bit BLAKE2b identity token plus generation, Fight epoch, event counter and a compact time token. OFFLINE names use a video-relative millisecond token rather than `datetime.fromtimestamp(relative_seconds)`, avoiding both 1970 dates and the reproduced UUID-duplication/277-character filename failure.

A temporary clip-serialization failure is carried as `evidence_error`. Stage3 may still infer from the job's in-memory frames; however the historical run cannot silently claim durable evidence success and is failed explicitly after ordered EOF/downstream drain when evidence is required.

---

# 12. Speed inference ownership

Primary worker:

```text
fight/pipeline_mp/speed_worker.py
```

`speed_worker` owns camera-local:

- ROI/calibration,
- motion gate,
- tracker,
- speed estimator,
- violation/cooldown history,
- evidence buffer/writer.

It does not own a YOLO Vehicle model.

```text
speed_worker[N]
 -> fair/bounded per-slot Vehicle admission
 -> shared Vehicle worker/model
 -> per-slot Vehicle result
 -> speed_worker[N]
```

Vehicle result payloads do not send full frame pixels back.

A Fight-only recovery must preserve Speed worker identity/state, Speed epoch, Vehicle service and existing source feed on a healthy mixed LIVE camera.

## 12.1 Historical Speed

Historical Speed uses the same camera-local Speed processor and shared Vehicle service, but a Speed/BOTH OfflineRun is not queued until a calibration document is supplied and validated. The run snapshots that calibration into private job storage; a historical run does not borrow mutable LIVE camera calibration implicitly.


---

# 13. LIVE vs ordered FILE vs explicit historical-run semantics

## 13.1 LIVE / RTSP / HTTP camera

Priority:

```text
freshness > completeness
```

Expected behavior:

- bounded queues,
- stale LIVE work may be shed explicitly,
- reconnect is ingest-owned,
- bounded local retries,
- eligible LIVE Speed resumes after Vehicle recovery,
- eligible LIVE Fight resumes after Fight recovery,
- unrelated branches/services survive recoverable faults where safe,
- same-source capability churn preserves source generation where possible,
- preview may remain active with both analytics branches intentionally disabled.

## 13.2 Generic ordered FILE

Priority:

```text
correctness + ordering > freshness
```

Expected behavior:

- ordered work waits/defer instead of silently dropping required work,
- EOF is generation-local Event state,
- EOF Event is set before consumer EOF delivery,
- clean required consumer exit = authoritative EOF + exit code 0,
- clean EOF does not restart, advance generation, reopen or replay source,
- mixed Fight+Speed waits for all required consumers,
- pre-EOF/non-zero failures stay failed,
- later EOF cannot rewrite prior branch failure as clean,
- Fight/Vehicle service failures do not replay partial files,
- benchmark deadline truncation remains `INCOMPLETE`.

These generic FILE rules remain valid for benchmark/local-file execution. Django's product workflow adds the stronger explicit historical domain below.

## 13.3 OFFLINE / historical application workflow — Phase 25

Persistent domain:

```text
OfflineAsset
  -> original private file + optional Location + optional legacy Camera link

OfflineRun (UUID)
  -> explicit analysis_type: FIGHT / SPEED / BOTH
  -> configuration snapshot
  -> QUEUED / PROCESSING / COMPLETED / FAILED / CANCELLED

OfflineResult
  -> run
  -> event_id
  -> analysis_type
  -> video_time_sec
  -> confidence
  -> evidence_path
  -> payload
```

An upload creates an `OfflineAsset`; it does **not** create a new LIVE Camera. Migration `0009_backfill_offline_assets` links historical non-live Camera records to assets without moving original files, deleting legacy Incidents, or creating implicit analysis runs.

Current job protocol is intentionally one outstanding request at a time:

```text
Django create_run
 -> immutable OfflineRun UUID / offline_<uuidhex>
 -> request.json

runtime OfflineJobs
 -> persist PROCESSING claim + owner BEFORE source/model launch
 -> SharedServices.prepare(live + offline)
 -> CameraRuntimeManager.start_camera(one-shot FILE)
 -> ordered processing
 -> authoritative generation-local EOF
 -> required consumer clean drain/exit
 -> OfflineDrain through Stage3/Incident path
 -> IncidentAggregator finalization
 -> durable outbox offset acknowledgement

Django reconcile_jobs
 -> waits until Incident Dispatcher cursor >= acknowledged outbox offset
 -> only then marks run COMPLETED
```

The cursor wait prevents the UI from declaring completion before historical results already published by the runtime have crossed the durable dispatcher boundary.

Failure/restart rules:

- a different parent encountering an old PROCESSING claim marks it `runtime_interrupted`; it does not replay it,
- watchdog/source/required-consumer failure for `offline_*` is terminal for that run,
- cancellation is terminal for that run identity,
- explicit re-analysis creates a new UUID and may intentionally read the asset again,
- technical global stop fails/interrupts in-flight offline ownership rather than pretending it completed,
- LIVE analytics pause does not cancel the historical job,
- evidence-write failure may allow ordered inference/Stage3/drain to finish but the run becomes `FAILED / evidence_write_failed`, not `COMPLETED`.

## 13.4 Historical time semantics

Historical Fight event positions are video-relative seconds. Reporting uses explicit source-time fields and `OfflineResult.video_time_sec`; it does not format video position as Unix wall clock.

LIVE event timestamps remain wall-clock timestamps.

The durable outbox may still carry a real wall-clock `detected_at`/finalization time for transport/audit while `video_time_sec` carries historical position. These meanings must not be conflated.

---

# 14. Incident durability and live-vs-historical persistence boundary

Primary files:

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/speed_worker.py
Fight_backend_project/backend_frontend_project/incidents/services/ingest.py
Fight_backend_project/backend_frontend_project/incidents/services/routing.py
Fight_backend_project/backend_frontend_project/services/incident_access.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/offline_analysis.py
```

Runtime produces evidence and durable outbox envelopes before Django ingestion.

Core runtime semantics remain:

- append-only JSONL incident outbox,
- serialized writers,
- flush/fsync where designed before successful publication,
- partial-tail preservation,
- persistence failure surfacing,
- required evidence durability before successful publication,
- stale generation/epoch guards,
- Fight service + consumer publication floors,
- Speed generation + consumer-epoch fencing,
- optional `video_time_sec` for historical position.

Runtime never directly creates Django `Incident` or `OfflineResult` ORM rows.

## 14.1 LIVE envelope path

For normal LIVE camera IDs:

```text
outbox
 -> Django dispatcher
 -> Incident ORM
 -> routing / audit / ACK / resolve
 -> access-scoped Olaylar / evidence
```

Routing itself refuses non-LIVE camera incidents, and operator-visible Incident queries filter to `camera.source_kind=LIVE`.

## 14.2 OFFLINE envelope path

`incidents.services.ingest.ingest_envelope` recognizes `offline_*` before normal Camera lookup:

```text
outbox envelope camera_id=offline_<run>
 -> offline_analysis.import_result
 -> OfflineResult (idempotent event_id)
 -> NO live Incident ORM row
 -> NO IncidentRoute / live alarm
 -> Video Analizleri only
```

Historical playback/evidence is served through scope-checked protected endpoints with byte-range support. Retention reference checks protect evidence referenced by either live `Incident` or historical `OfflineResult`.

This is a persistence-domain separation, not just presentation filtering.

## 14.3 Phase-25.1 evidence failure semantics

A Fight event may have enough in-memory frames for Stage3 even if temporary MP4 serialization fails.

```text
temp clip serialization fails
 -> report explicit evidence_write_failed
 -> Stage3 may infer from in-memory frames
 -> do not fabricate clip_path/evidence
 -> Incident worker records offline failure
 -> ordered FILE reaches EOF/drain if otherwise healthy
 -> OfflineRun becomes FAILED / evidence_write_failed
```

A true Fight consumer crash before EOF remains a required-consumer failure and stays fail-closed/no-replay.

## 14.4 Qualification evidence limitation

The current outbox schema does not retain publication-floor history sufficient for an external qualification harness to prove, after the fact, that every stale Fight item was rejected at the exact floor transition.

Therefore Phase-22 summary field:

```text
stale_durable_publication_verified
```

remains explicitly `null` when that proof is unavailable.

The harness does not synthesize incidents merely to manufacture proof and does not interpret “zero incidents” as proof of stale-work fencing. Deterministic Phase-20/21 tests remain the executable proof for that boundary; real incident artifacts can be reviewed separately.

---

# 15. Graceful finalization and Reporter ordering — Phase 19

Normal healthy service withdrawal still uses bounded graceful finalization.

`SharedServices.FINALIZE_TIMEOUT_SEC` remains:

```text
8.0 seconds
```

This is one common grace budget per service bundle.

Normal clean dynamic-runtime exit remains conceptually:

```text
camera producers finish
 -> shared services close gracefully
 -> workers publish final summaries
 -> Reporter sentinel
 -> Reporter final flush/exit
 -> performance_summary.json construction
```

Failure/recovery teardown skips graceful finalization on poisoned transport.

Phase-22 SIGBREAK ownership is specifically designed so normal Windows stop can still reach this parent-owned sequence instead of children aborting mid-queue/lock operation.

---

# 16. Performance and attribution model

Primary files:

```text
fight/pipeline_mp/attribution.py
fight/pipeline_mp/performance.py
benchmarks/real_inference.py
benchmarks/telemetry.py
```

The runtime keeps distinct populations for camera-local call timings, shared-worker queue/inference/result-enqueue timings, worker all-request and steady-state distributions, batch distributions, per-camera attribution and host/GPU samples.

Important distinctions remain:

- Vehicle admission-inclusive wait is not pure post-enqueue wait.
- Frame-delivery age is not pure IPC-copy latency.
- Detector-call wall time is not CUDA-kernel duration.
- A mean of run-level p95 values is not a pooled p95.
- Per-request inference and per-batch inference are different populations.
- Missing samples remain unavailable.

Attribution is observation only.

---

# 17. Person microbatching contract

Support exists, but production defaults remain:

```text
person_batch_enabled = false
person_batch_size = 1
person_batch_max_wait_ms = 0
```

Phase 19 characterization showed Batch-2 / 5 ms could reduce Person queue pressure and modestly improve total Mixed-8 throughput on the tested RTX 3050 traffic workload. Batch-4 reduced Person queue pressure further but worsened Vehicle contention and was not the best total-system tradeoff.

No Phase-22 tool silently enables or retunes batching. The LIVE qualification harness refuses configs with Person batching enabled rather than silently changing them.

---

# 18. Phase-22 LIVE qualification subsystem

Primary files:

```text
benchmarks/live_qualification.py
benchmarks/live_source.py
benchmarks/README.md
```

Phase 22 adds a bounded qualification harness around the **real** production Supervisor/runtime path:

```text
RuntimeSupervisor
 -> run_multiprocess
 -> CameraRuntimeManager
 -> real shared services / camera consumers / health
```

It does not implement a duplicate runtime.

The tool qualifies one selected mixed Fight+Speed camera at a time.

## 18.1 Input modes

Private real LIVE config:

- one enabled mixed camera must be selected,
- RTSP/RTSPS is labeled as RTSP input,
- HTTP/HTTPS is labeled live HTTP and not called RTSP,
- ordered local files are refused by this LIVE tool,
- original source is preserved unless `--generated` is explicit.

Generated mode:

```text
--generated
```

creates a local MJPEG fixture and is always labeled:

```text
LIVE-LIKE / SYNTHETIC SOURCE
```

Generated input exercises real runtime/process/decode/lifecycle plumbing, but stationary/generated imagery does not prove detection quality, full inference contention, vehicle tracking continuity or network behavior.

## 18.2 Scenarios

Supported scenarios:

```text
baseline
fight-shared
fight-local
capability-churn
shutdown-backoff
```

### baseline

No injected fault. Expected: advancing required branches, stable identities, no recovery/restart violation and clean shutdown.

### fight-shared

Terminate the verified current Person worker. Expected: shared Fight bundle recovers, Fight service epoch and Fight consumer epoch advance, while ingest/Preview/Speed/Vehicle/camera generation/Speed epoch remain stable.

### fight-local

Terminate only the verified current Fight camera worker. Expected: shared Fight services stay alive and only that Fight consumer is recreated.

### capability-churn

Publish revisioned desired-state transitions:

```text
Fight+Speed
 -> Speed-only
 -> Fight+Speed
```

Expected: source/Preview/Speed remain alive; Fight returns with a fresh valid consumer incarnation.

### shutdown-backoff

Inject shared Fight failure, observe pending recovery/backoff, then request Supervisor stop. Expected: no replacement starts after shutdown becomes authoritative and no child survives.

## 18.3 Fault-injection safety

Fault injection is opt-in. The baseline command never kills a worker.

The harness does not accept an arbitrary PID from the user. A target is resolved from a fresh matching-run health snapshot and verified against the current runtime process tree.

Process identity includes PID + creation time. Stale, reused, ambiguous or non-owned targets are refused.

The runtime command/launch path must match the current qualification run before fault action.

No administrator privilege is required.

## 18.4 Isolation and output ownership

Qualification refuses conflicting active runtime/held capacity ownership where applicable.

Every run requires a **new output directory**. Existing result directories are not overwritten.

Raw runtime config/logs may contain real private source details and must remain private/ignored. Compact qualification summary is redacted.

Generated runtime/benchmark outputs remain operational artifacts, not source files.

---

# 19. Phase-22 qualification observations

The main machine-readable artifact is:

```text
qualification_summary.json
```

It records, where available:

```text
scenario
classification
source label/type
requested/measured/total duration
camera_id
camera generation start/end
Fight consumer epoch start/end
Speed epoch start/end
Fight service epoch start/end
Vehicle service epoch start/end
process identities and observed changes
branch progress start/end
recovery reasons/events
camera/Fight/Speed/service restart counts
source reconnect count
capacity/drop/stale/rejection counters already exposed by runtime
runtime exit code
surviving children after shutdown
RSS start/end/peak/sample count
host CPU mean/p95
optional GPU utilization/VRAM distributions
health failures
health snapshot write failures
outbox observation status
```

Unavailable evidence remains null.

RSS delta is an observation, not an automatic memory-leak diagnosis. A short run or startup allocation must not be labeled a leak merely because end RSS exceeds start RSS.

CPU is host CPU unless explicitly documented otherwise.

Phase-22 health fields such as PIDs/progress are used to observe ownership and continuity; they do not drive runtime recovery.

---

# 20. Qualification classification

Phase-22 qualification classification is deliberately separate from capacity benchmark classification.

```text
PASS
PASS_WITH_RECOVERY
FAIL
INCOMPLETE
```

## PASS

Used for completed baseline qualification with required progress, expected stable identities, no unexpected recovery/ownership violation, clean exit and no surviving children.

## PASS_WITH_RECOVERY

Used for an explicit fault/churn scenario when the expected bounded recovery/isolation transition was observed and shutdown completed cleanly.

## FAIL

Examples include:

- unexpected source/camera generation change,
- unrelated Speed/Vehicle/source restart caused by Fight fault,
- required branch fails to resume,
- wrong identity transition,
- duplicate observed source ownership,
- restart/recovery storm,
- runtime nonzero/failure outcome,
- surviving owned child after shutdown,
- explicit scenario violates expected isolation.

## INCOMPLETE

Examples include:

- startup/observation deadline expires,
- snapshot freshness/required identity evidence unavailable,
- required scenario phase never observed,
- final shutdown evidence unavailable,
- insufficient evidence to classify safely.

Failures are latched: a later healthy-looking snapshot cannot erase an observed qualification violation.

The harness does not alter runtime behavior based on this classification.

---

# 21. Restart-storm and resource-leak observation

Qualification keeps bounded counters/events for:

```text
camera restarts
Fight consumer restarts
Fight service restarts
Speed consumer restarts
Vehicle restarts
source reconnects
```

The diagnostic rule is intentionally simple. Baseline expects no recovery. A single injected Fight fault may cause its intended bounded Fight recovery but must not cascade into unrelated repeated source/Speed/Vehicle/camera restarts.

Resource observation tracks the owned process tree with bounded identity history and RSS samples. The purpose is to expose obvious uncontrolled growth or orphaning, not to infer a statistical leak from a short sample.

---

# 22. Real RTSP qualification boundary

Automated generated smoke is not real RTSP qualification.

Manual real RTSP acceptance should include:

1. Start one mixed camera through the real Supervisor with a private valid configuration.
2. Record CameraIngest/Fight/Speed/Preview and shared-service identities/epochs.
3. Confirm one CameraIngest owns the source.
4. Temporarily interrupt only the test source/network.
5. Observe ingest-owned `source_offline` / reconnect/backoff behavior.
6. Confirm frame silence alone does not cause unrelated Fight/Vehicle replacement.
7. Restore the source and confirm ingest/Fight/Speed progress returns.
8. Confirm no duplicate source owner or restart storm.
9. Stop while reconnecting and confirm bounded shutdown/no surviving children.
10. Preserve logs/results in a new output directory.

A manual network interruption performed during `baseline` is intentionally an unexpected event to that scenario; it is not silently relabeled a passing synthetic recovery test.

---

# 23. Capacity benchmark subsystem

Primary files:

```text
benchmarks/__main__.py
benchmarks/control_plane.py
benchmarks/real_inference.py
benchmarks/telemetry.py
benchmarks/README.md
```

Capacity benchmarking remains separate from Phase-22 LIVE qualification.

## Real inference

Real Supervisor/runtime + ordered-file decode + real model workers + real queue/recovery/health behavior.

## Control-plane/synthetic

Real parent-side lifecycle/scheduling structures with inert/fake execution. Synthetic camera-equivalents are not inference capacity.

Capacity classifications remain:

```text
HEALTHY
PRESSURED
SATURATED
INCOMPLETE
```

Do not confuse these with Phase-22 PASS/PASS_WITH_RECOVERY/FAIL/INCOMPLETE.

---

# 24. Measured characterization evidence

Development characterization hardware:

```text
NVIDIA GeForce RTX 3050 Laptop GPU, 6 GB
Intel Core i7-13700H
64 GB RAM
Windows
```

Full-run aggregate ordered-file FPS includes startup/drain/EOF effects and is not steady live RTSP FPS.

## Fight-only selected points

```text
2 cameras    28.67 FPS
4 cameras    49.89 FPS
8 cameras    65.55 FPS
12 cameras   76.80 FPS
```

Post-Phase-17 Fight-12 completed 903 frames/camera, 10,836 total frames, Person/Pose/Stage3 counts 5004/3732/72, no replay/recovery/restart, classification HEALTHY.

## Speed-only Phase-18 selected points

```text
8 cameras
  aggregate FPS 82.70
  Vehicle queue mean/p95 77.19 / 119.28 ms
  Vehicle inference mean/p95 29.13 / 42.27 ms

12 cameras
  aggregate FPS 89.25
  Vehicle queue mean/p95 125.50 / 256.63 ms
  Vehicle inference mean/p95 29.89 / 51.55 ms
```

8 -> 12 aggregate growth is approximately 7.92% despite 50% more cameras. Queue pressure rises materially while sampled GPU utilization remains moderate; this is consistent with shared Vehicle serialization/scheduling/arrival/backpressure rather than simple raw-GPU saturation alone.

## Mixed-8 / batching context

Mixed traffic content exercised common ingest + Person + Vehicle + Speed-local contention but did not produce Pose/Stage3 work in the measured runs.

Across the two explicit OFF vs Batch-2 comparison pairs, arithmetic means of displayed run values were approximately:

```text
aggregate FPS      OFF 41.6802   B2 44.3643   +6.44%
wall seconds       OFF 130.1669  B2 122.2924  -6.05%
Person queue mean  OFF 169.2263  B2 103.8942  -38.61%
mean run p95       OFF 201.9597  B2 132.4978  -34.39%
```

The last row is a mean of run-level p95 values, **not** a pooled percentile.

Production batching remains OFF.

---

# 25. Phase ledger

## Phase 1 — Shared Person

One shared Person model/worker; camera-local motion/stabilizer/tracking/pair/ROI/event/prebuffer; explicit request identity.

## Phase 2 — Shared Pose

One shared Pose service/router; camera-local Pose temporal interpretation.

## Phase 3 — Performance observability

Bounded timing/queue/inference/delivery metrics and machine-readable summaries.

## Phase 4 — Microbatch capability

Latency-bounded Person microbatch support; conservative default remains OFF.

## Phase 5 — Centralized CameraIngest

One source/decode owner feeding Fight/Preview and later Speed; live-vs-file publication policy.

## Phase 6 — Runtime Supervisor

Standalone owner with durable state/config/PID and platform-aware start/stop behavior.

## Phase 7 — Organization/access

Location/SecurityUnit/Coverage/UserAssignment and `Camera.location`.

## Phase 8 — Durable incidents/routing

Evidence + outbox -> Django dispatcher -> common Incident/routing domain; runtime remains ORM-free.

## Phase 9 — Dynamic lifecycle

Desired revisions, stable slots, camera generations and in-parent camera add/remove/restart.

## Phase 10 — Health/watchdog

Bounded health events, HealthRegistry/Watchdog and atomic runtime snapshot.

## Phase 11 — Fair scheduling/capacity

Per-slot bounded admission/round-robin fairness; live shedding vs file defer semantics.

## Phase 12 — Operational durability/retention

Bounded cleanup/retention, locks, disk-pressure health and resilient service loops.

## Phase 13 — Shared Speed integration

Baseline:

```text
418f65bf137cfb31aa92629ac8fe1e03a0a1c54a
```

Added shared Vehicle inference, camera-local Speed state, Speed epoch fencing, common source/incident/Supervisor ownership.

## Phase 14 — Capability lifecycle / Vehicle recovery

Baseline:

```text
7b395ef94f04b435862ab013f9226b0982de34b6
```

Added capability-aware `SharedServices`, Vehicle transport/service epoch replacement, bounded recovery and live Speed resume/file no-replay.

## Phase 15 — Fight service recovery

Baseline:

```text
c3c019b2871dcd3891b24ef242a2c5e93fe9212f
```

Added whole-Fight bundle recovery, Fight service epoch, Stage3->Incident epoch propagation, publication floor and eligible LIVE resume.

## Phase 16 — Capacity benchmark harness

Baseline:

```text
022d2fd5a3a6cef4ece0ac1b7434b9b9493512a0
```

Separated real inference from synthetic/control-plane measurements and added explicit result classification.

## Phase 17 — Ordered-file EOF hardening

Baseline:

```text
3b9733f20a3ca4d3773c63fed0caba7939f2f27c
```

Made generation-local EOF Event authoritative and eliminated clean-EOF replay/restart races.

## Phase 18 — Bottleneck attribution

Baseline:

```text
823f87da3b4663e915085da8fd2145043e84175a
```

Added bounded non-correctness attribution for ingest, shared model services, Fight local work, Speed local work and Vehicle.

## Phase 19 / 19.1 — graceful finalization + Windows snapshot/failure identity

Baseline:

```text
fefedcc81095a5b808f048d1bc7daa13e04c0b1f
Finalize Phase 19 shared worker telemetry and Windows health reliability
```

Established normal shared-worker sentinel finalization, Reporter-before-summary ordering, retained worker/batch timing evidence, bounded Windows health-snapshot replace retry and pre-teardown failure-cause preservation.

## Phase 20/21 — LIVE Fight recovery isolation

Baseline:

```text
cbeb45066e8d25b6b6d2eeeda757e61b5e23e562
Finalize Phase 20/21 live recovery and Fight-Speed isolation
```

Established:

- source generation separate from Fight consumer incarnation,
- parent-owned Fight consumer epoch,
- Fight-only consumer replacement without source/Speed/Preview restart,
- mixed LIVE shared-Fight recovery preserving healthy Speed/Vehicle/source state,
- per-consumer durable publication floor,
- stale result/health/incident fencing,
- capability churn without source replacement where safe,
- stop-aware reconnect,
- evidence ID uniqueness across Fight consumer replacement,
- FILE fail-closed/no-replay preservation,
- bounded Speed spawn-failure recovery.

Final reviewed validation before commit: 236 passed + 26 subtests, compileall and diff check passed.

## Phase 22 — LIVE qualification + Windows shutdown hardening

Phase-22 baseline:

```text
06a67cc4328b602493ef9ef6d87916ec11e9b630
Add Phase 22 live runtime qualification and shutdown hardening
```

Added:

- `benchmarks.live_qualification` using the real Supervisor/runtime path,
- generated local MJPEG fixture explicitly labeled LIVE-like/synthetic,
- baseline/shared-Fight/local-Fight/capability-churn/shutdown-backoff scenarios,
- PID + creation-time/run ownership verification before fault injection,
- new-output-directory requirement,
- machine-readable qualification summary,
- PASS/PASS_WITH_RECOVERY/FAIL/INCOMPLETE classification,
- bounded process identity/restart/RSS/CPU/GPU observation,
- observation-only PID and ingest/Preview progress fields in health projection,
- explicit null semantics for unavailable stale-durable-publication proof,
- parent SIGBREAK handling via helper-thread stop request,
- child SIGBREAK ignore under Windows process-group stop,
- Windows timeout fallback that kills the owned tree while root PID still exists,
- failure reporting instead of false success when bounded tree-stop fallback fails.

Reported final validation:

```text
257 passed, 26 subtests passed
compileall passed
git diff --check passed
generated baseline PASS
generated shared-Fight PASS_WITH_RECOVERY
generated shutdown-backoff PASS_WITH_RECOVERY
```

Real RTSP interruption and long-duration soak remain separate manual/environment-specific qualification.

## Phase 23 — consolidated operator workspace

Accepted implementation baseline:

```text
57dc5db9393f9791184fa4433501588ebe016634
Integrate operator UI, live preview and offline analysis
```

Promoted the operator-facing UI from deferred work into architecture:

- consolidated navigation: Genel Bakış; Kameralar/Lokasyonlar/Kullanıcılar; Canlı İzleme/Olaylar/Video Analizleri; Sistem Durumu,
- one presentation layer over existing runtime/Incident/access services rather than duplicate Fight/Speed dashboards,
- unified LIVE event history/detail with camera scope,
- legacy route compatibility while primary ACK/routing internals remain out of the normal operator UI,
- redacted/friendly status projection,
- LIVE camera form no longer accepts uploaded/local historical media as a camera source,
- public authentication/error templates separated from the authenticated workspace.

## Phase 24 — source-preserving LIVE preview, analytics control and access UI

Same accepted baseline commit above.

Established:

- CameraIngest/Preview can run with Fight=false and Speed=false,
- primary in-memory latest-frame preview transport,
- parent-owned private loopback PreviewGateway,
- one bounded multiplexed browser stream for page previews,
- browser preview authorization continuously rechecked through Django scope,
- LIVE analytics pause distinct from technical runtime stop,
- analytics pause preserves LIVE source/preview and fences removed Fight/Speed consumers,
- SecurityUnit assignment management in admin user create/edit,
- unassigned approved viewers fail closed with an explicit no-access message,
- source/capability churn remains generation/epoch fenced,
- no web/preview source reopen.

## Phase 25 — explicit historical-video analysis domain

Same accepted baseline commit above.

Established:

- `Camera.source_kind` compatibility classification,
- `OfflineAsset`, `OfflineRun`, `OfflineResult`,
- migrations `0008_offline_analysis_domain` and `0009_backfill_offline_assets`,
- LIVE registry excludes uploaded/local historical sources,
- explicit FIGHT/SPEED/BOTH run creation; re-analysis = new run,
- one-shot parent-owned `OfflineJobs` using the existing runtime and shared services,
- durable claim/no-silent-replay semantics,
- downstream `OfflineDrain` + outbox-offset + Django dispatcher-cursor completion,
- historical envelopes import into `OfflineResult`, never live Incident/routing,
- protected scoped original playback/evidence,
- historical Speed requires validated calibration before queueing.

## Phase 25.1 — historical evidence path/time hardening

Same accepted baseline commit above.

Established:

- compact deterministic evidence filenames using a 128-bit identity hash plus generation/Fight epoch/counter/time token,
- video-relative Fight metadata instead of 1970 epoch formatting,
- explicit `evidence_error` propagation,
- Stage3 continuation from in-memory frames after auxiliary temp-clip serialization failure,
- evidence failure separated from Fight consumer/process failure,
- historical evidence failure terminal only after authoritative EOF/downstream drain when the inference path otherwise remains healthy,
- genuine pre-EOF consumer failure remains fail-closed/no-replay.

Focused Phase-25.1 development validation reported 102 passed, 1 skipped when ffmpeg was unavailable in that test environment. The final repository-wide validation supersedes intermediate counts: 278 passed + 26 subtests, compileall/check/migration-drift/diff checks clean as reported at the top of this contract.

Manual accepted historical Fight run processed 903/903 source frames and reached clean authoritative EOF with six Stage3 submissions and one historical result. This validates that reproduced path only; it is not a general throughput claim.


---

# 26. Task router for coding agents

Read this document first and inspect current Supervisor-managed ownership paths rather than inferring architecture from legacy helpers. Coding agents must not modify this contract.

## Supervisor / Windows shutdown / desired state

```text
fight/runtime_supervisor/core.py
fight/runtime_supervisor/camera_state.py
fight/runtime_supervisor/client.py
fight/runtime_supervisor/locking.py
fight/pipeline_mp/common.py
fight/pipeline_mp/run_multiprocess.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/analytics_control.py
tests/test_runtime_supervisor.py
```

## Dynamic lifecycle / source ownership / Fight isolation

```text
fight/pipeline_mp/camera_ingest.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/fight_identity.py
fight/pipeline_mp/generation.py
fight/pipeline_mp/health.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
tests/test_live_fight_isolation.py
tests/test_dynamic_camera_lifecycle.py
tests/test_fight_service_recovery.py
tests/test_runtime_health.py
tests/test_speed_integration.py
```

## LIVE preview / operator status — Phase 24

```text
fight/pipeline_mp/camera_preview.py
fight/pipeline_mp/preview_gateway.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/live_preview.py
Fight_backend_project/backend_frontend_project/guvenlik/operator_views.py
Fight_backend_project/backend_frontend_project/guvenlik/presentation.py
Fight_backend_project/backend_frontend_project/static/operations/preview_stream.js
Fight_backend_project/backend_frontend_project/static/operations/workspace.js
Fight_backend_project/backend_frontend_project/templates/operations/
tests/test_live_preview.py
tests/preview_stream_test.cjs
Fight_backend_project/backend_frontend_project/guvenlik/phase23_tests.py
Fight_backend_project/backend_frontend_project/guvenlik/phase24_tests.py
```

## Historical/offline analysis — Phase 25/25.1

```text
Fight_backend_project/backend_frontend_project/streams/models.py
Fight_backend_project/backend_frontend_project/streams/source_kind.py
Fight_backend_project/backend_frontend_project/streams/offline_views.py
Fight_backend_project/backend_frontend_project/streams/protected_media.py
Fight_backend_project/backend_frontend_project/streams/migrations/0008_offline_analysis_domain.py
Fight_backend_project/backend_frontend_project/streams/migrations/0009_backfill_offline_assets.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/offline_analysis.py
fight/pipeline_mp/offline_jobs.py
fight/pipeline/evidence_metadata.py
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/incident_worker.py
fight/pipeline/incident_aggregator.py
fight/pipeline/incident_outbox.py
Fight_backend_project/backend_frontend_project/incidents/services/ingest.py
Fight_backend_project/backend_frontend_project/streams/phase25_tests.py
tests/test_offline_jobs.py
tests/test_offline_evidence.py
```

## Fight inference / durable fencing

```text
fight/pipeline_mp/camera_worker.py
fight/pipeline_mp/person_worker.py
fight/pipeline_mp/pose_worker.py
fight/pipeline_mp/stage3_worker.py
fight/pipeline_mp/messages.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/fight_identity.py
```

## Speed / Vehicle

```text
fight/pipeline_mp/speed_worker.py
fight/pipeline_mp/camera_lifecycle.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
Fight_backend_project/backend_frontend_project/services/speed_bridge/speed_runner.py
tests/test_speed_integration.py
```

## Health / snapshot / recovery identity

```text
fight/pipeline_mp/health.py
fight/pipeline_mp/messages.py
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/core.py
tests/test_runtime_health.py
tests/test_health_snapshot_reliability.py
```

## Graceful finalization / Reporter / performance

```text
fight/pipeline_mp/shared_services.py
fight/pipeline_mp/run_multiprocess.py
fight/pipeline_mp/reporter.py
fight/pipeline_mp/performance.py
tests/test_shared_service_finalization.py
```

## LIVE qualification — Phase 22

```text
benchmarks/live_qualification.py
benchmarks/live_source.py
benchmarks/README.md
fight/pipeline_mp/health.py
fight/pipeline_mp/common.py
fight/pipeline_mp/run_multiprocess.py
fight/runtime_supervisor/core.py
tests/test_live_qualification.py
tests/test_runtime_supervisor.py
```

## Capacity / attribution

```text
benchmarks/real_inference.py
benchmarks/control_plane.py
benchmarks/telemetry.py
fight/pipeline_mp/attribution.py
fight/pipeline_mp/performance.py
tests/test_capacity_benchmarks.py
tests/test_attribution_telemetry.py
```

## Incident durability / Django dispatcher

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/speed_worker.py
Fight_backend_project/backend_frontend_project/incidents/services/ingest.py
Fight_backend_project/backend_frontend_project/incidents/services/retention.py
Fight_backend_project/backend_frontend_project/incidents/services/routing.py
Fight_backend_project/backend_frontend_project/incidents/models.py
```

## Location / authorization

```text
Fight_backend_project/backend_frontend_project/adminx/models.py
Fight_backend_project/backend_frontend_project/adminx/forms.py
Fight_backend_project/backend_frontend_project/streams/models.py
Fight_backend_project/backend_frontend_project/services/access_scope.py
Fight_backend_project/backend_frontend_project/services/incident_access.py
Fight_backend_project/backend_frontend_project/incidents/models.py
```

---

# 27. Cross-phase acceptance checklist

Before accepting architecture-affecting work, verify as applicable:

```text
[ ] ARCHITECTURE.md was read first and coding agent did not modify it
[ ] one Runtime Supervisor owns the global AI runtime
[ ] runtime remains Django-ORM-free
[ ] one CameraIngest remains source/decode/reconnect owner per active runtime source incarnation
[ ] no recovery or web/preview path opens a duplicate LIVE physical source
[ ] LIVE Fight+Speed still share one source decode
[ ] monitorable LIVE preview-only cameras remain valid with Fight=false and Speed=false
[ ] LIVE registry publishes source_kind=LIVE cameras only
[ ] uploaded/local historical media is not inserted into LIVE desired camera state
[ ] camera-local temporal/calibration state stays local
[ ] optional shared services start only when LIVE + active OFFLINE demand requires them
[ ] source generation, Fight epoch, Speed epoch and service epochs retain distinct meanings
[ ] same-source LIVE Fight recovery does not advance camera generation
[ ] Fight-only recovery does not advance Speed epoch
[ ] stale Fight result/health/incident output is fenced by consumer/service identity
[ ] Fight publication floors are raised before unsafe failed work can durably publish
[ ] Vehicle recovery remains independent of Fight where safe
[ ] camera-local Fight failure does not recycle the whole Fight service unnecessarily
[ ] capability churn cannot resurrect removed/disabled/stale consumers
[ ] source change creates a new source generation
[ ] global stop prevents pending recovery/reconnect recreation
[ ] LIVE analytics pause preserves CameraIngest/Preview while withdrawing analytics
[ ] LIVE analytics pause does not silently cancel an active OfflineRun
[ ] technical hard stop remains distinct from analytics pause and sets monitoring hold
[ ] primary preview path never opens the source and does not require per-frame disk round trip
[ ] preview latest-frame replacement is bounded and cannot backpressure CameraIngest
[ ] preview cache/packets are generation and frame-sequence fenced
[ ] PreviewGateway remains loopback-only, bearer-protected, bounded and private
[ ] Django preview proxy validates active run and continuously rechecks camera authorization
[ ] historical upload creates OfflineAsset, not a new LIVE Camera
[ ] explicit OfflineRun UUID is required for processing/re-analysis
[ ] current single-outstanding offline request protocol is not misrepresented as concurrent job scheduling
[ ] offline claim is persisted before source/model launch
[ ] interrupted/failed OfflineRun cannot silently replay under a new parent
[ ] ordered-file EOF remains generation-local authoritative state
[ ] clean file completion still requires EOF + exitcode 0 for required consumers
[ ] failed ordered work cannot later become clean because EOF arrived
[ ] ordered FILE recovery never replays lost work
[ ] OfflineRun completion additionally waits downstream drain acknowledgement
[ ] Django does not mark OfflineRun complete before dispatcher cursor reaches acknowledged outbox offset
[ ] offline_* outbox envelopes create OfflineResult only, never live Incident/routing
[ ] operator LIVE incident feeds remain source_kind=LIVE
[ ] historical video position remains video-relative, never formatted as 1970 wall time
[ ] compact historical evidence names preserve uniqueness without repeated full UUID paths
[ ] temp evidence serialization failure is distinct from inference-consumer failure
[ ] historical evidence failure cannot be reported as successful durable evidence
[ ] historical playback/evidence is access-scoped and path traversal/reference escape is rejected
[ ] historical Speed cannot queue without validated calibration
[ ] fair scheduling/capacity stays bounded
[ ] no correctness dependency on OS qsize()/empty()
[ ] health remains generation/consumer/service-epoch aware
[ ] OfflineRun completion truth is durable job/EOF/drain state, not health telemetry
[ ] snapshot retry remains limited to documented transient Windows replacement errors
[ ] normal shared-service withdrawal retains Phase-19 bounded graceful finalization
[ ] failure teardown does not pretend to be graceful just to preserve telemetry
[ ] Reporter final flush precedes final performance-summary construction
[ ] missing metrics remain null/unavailable
[ ] Person batching remains OFF by default
[ ] Windows runtime parent handles SIGBREAK safely outside multiprocessing lock re-entry
[ ] spawned runtime children do not independently abort on group CTRL_BREAK
[ ] Windows timeout fallback kills the owned process tree while root still exists
[ ] Supervisor does not report successful stop when tree fallback fails
[ ] POSIX SIGTERM/SIGKILL group behavior is not accidentally replaced by Windows logic
[ ] qualification fault injection is opt-in and verifies current run/PID/create-time ownership
[ ] qualification never accepts arbitrary/stale PID targets
[ ] qualification output directories are not reused
[ ] generated source is labeled LIVE-like/synthetic
[ ] synthetic/generated smoke is not called real RTSP proof
[ ] qualification observations never become runtime correctness control
[ ] stale durable publication verification stays null if evidence is unavailable
[ ] benchmark and qualification classifications remain distinct
[ ] synthetic counts are not called real capacity
[ ] one GPU's measurements are not extrapolated to another GPU
[ ] generated runtime/benchmark/job secrets are not staged
[ ] accepted Phase-23/24/25 UI semantics are preserved unless a new phase explicitly changes them
[ ] PostgreSQL/Docker/Nginx/deployment and further UX redesign remain frozen unless explicitly promoted
```

---

# 28. Generated artifacts / repository hygiene

Operational/generated paths include:

```text
.runtime_supervisor/
.runtime_supervisor/preview_gateway.json
.runtime_supervisor/offline/
Fight_backend_project/backend_frontend_project/media/camera_uploads/
Fight_backend_project/backend_frontend_project/media/pipeline_runs/.run_locks/
Fight_backend_project/backend_frontend_project/media/runtime_spool/
benchmarks/results/
benchmarks/.capacity.lock
phase*_review.diff
```

The preview-gateway descriptor contains run-private connection metadata/token and is operational state, not a public/static artifact.

The offline job root contains private uploaded assets, calibration snapshots, request/state files and run ownership metadata. It is not source code and must not be exposed as a generic media directory.

Each benchmark/qualification run gets a new result directory. Historical benchmark results are not overwritten to obtain a cleaner number.

Private LIVE configs/raw logs may contain source credentials or network details and must not be committed. Raw local filesystem paths are not authorization.

A tracked root helper currently present at the Phase-25.1 reference commit, `mp4_mjpeg_camera.py`, is a manual/local MJPEG diagnostic fixture. It is **not** part of the production source-ownership topology, not a second production CameraIngest, and must not be used by coding agents as an architectural source owner.

---

# 29. Deferred / frozen work and known risks after Phase 25.1

Highest-value next qualification work:

- real external RTSP baseline with representative FPS/resolution/network path,
- deliberate RTSP/network interruption and reconnect validation,
- long-duration soak with source churn, capability churn, preview clients, Fight/Vehicle recovery, offline jobs and storage growth,
- repeated orphan/process-tree checks under real Windows long runs,
- mixed Fight+Speed workloads that actually exercise Pose and Stage3,
- target-hardware RTX 5090 characterization using the real machine,
- realistic incident/evidence end-to-end latency and duplicate-incident validation,
- real historical Speed acceptance with representative calibration/evidence,
- production-class storage/retention/operator-observability sizing.

Known implementation risks at the current reference commit:

1. **Windows stale-PID probe gap.** `fight/runtime_supervisor/core.py::_pid_exists` currently catches `ProcessLookupError`, `PermissionError` and `OSError` around `os.kill(pid, 0)`, but not `SystemError`. A manual Windows restart previously reproduced `WinError 87 / SystemError: built-in kill returned a result with an exception set` for a stale persisted PID. The architecture requires stale PID reconciliation to be safe, but this specific implementation edge still needs a focused fix/regression before it should be called fully hardened.
2. **Arbitrarily deep Windows output roots are not proven safe.** Phase 25.1 substantially shortens evidence filenames and fixed the reproduced 277-character path case, but exceptionally deep base/run directories can still exceed a codec/platform path limit.
3. **Offline concurrency is intentionally not claimed.** The current durable protocol has one outstanding `request.json`; multi-job scheduling requires a separately designed queue/ownership model.
4. **Manual historical acceptance is Fight-focused.** Unit/integration coverage enforces Speed calibration and common lifecycle behavior, but representative real historical Speed calibration/evidence acceptance remains outstanding.

Optimization work remains evidence-driven:

- model-worker concurrency/partitioning only if service queue/time dominates on target hardware,
- shared-memory transport only after controlled attribution isolates transport overhead as material,
- Speed CPU optimization only if preprocessing/tracking/decision/evidence attribution justifies it,
- microbatch default changes only after representative target-workload validation,
- multi-GPU partitioning only after single-node bottlenecks are measured on production-class hardware.

Explicitly frozen unless separately promoted:

```text
PostgreSQL migration
Docker redesign
Nginx/media offload
production deployment/service packaging
further frontend/dashboard redesign
further incident UX redesign
further preview/offline UX redesign
```

The Phase-23/24/25 workspace, preview and historical-video workflows themselves are **not deferred**; they are the accepted current architecture.

---

# 30. Qualification strategy after Phase 25.1

Phase 22 established the real Supervisor-path LIVE qualification method. Phases 23-25.1 add operator-preview and historical-workflow boundaries that must be qualified without weakening the same ownership contract.

On a real RTSP environment, qualification should capture at minimum:

```text
source continuity / reconnect behavior
CameraIngest identity
camera generation
Fight consumer epoch
Speed epoch
Fight service epoch
Vehicle service epoch
Fight/Speed/Preview progress
preview freshness and bounded viewer behavior
restart/recovery counters
source reconnect counters
health/snapshot failures
runtime exit code
surviving child processes
RSS trend and peak
CPU distribution
GPU utilization / VRAM where available
incident/evidence artifacts when naturally produced
```

Historical acceptance should separately capture:

```text
explicit OfflineRun identity
asset authorization and protected original playback
one-shot claim before decode
source frame count vs consumed frame count where known
authoritative EOF
required Fight/Speed consumer completion
Stage3/Incident downstream drain acknowledgement
dispatcher cursor >= acknowledged outbox offset
OfflineResult persistence
absence of corresponding live Incident/routing rows
video-relative result time
evidence-path validity
restart/interruption => terminal no-replay
explicit re-analysis => new run identity
Speed calibration validation for SPEED/BOTH
```

For target-hardware capacity, add:

```text
aggregate and per-camera service rate
Person/Pose/Stage3 queue + inference + result enqueue
Vehicle admission-inclusive wait + inference + result enqueue
Fight/Speed camera-local processing
preview encoding/fan-out cost under representative viewers
queue high-water / rejection / live shedding
Reporter/snapshot reliability over long duration
historical + LIVE contention where both are allowed
```

Decision rule:

```text
If shared model service time/queueing dominates:
    evaluate batching, worker concurrency or model partitioning.

If camera-local CPU work dominates:
    optimize that subsystem first.

If preview encoding/fan-out becomes material:
    optimize preview separately; do not make source ownership or inference wait on viewers.

If transport remains dominant after controlling backlog/fanout:
    run a shared-memory transport experiment before adopting it.

If one service harms another under mixed load:
    optimize total-system fairness/throughput, not one queue in isolation.

If target GPU behavior differs materially from RTX 3050:
    prefer target measurements over laptop tuning conclusions.
```

No camera-count, RTSP SLA, historical-throughput or multi-job concurrency claim belongs in this contract without a clearly described real workload, hardware, source characteristics, duration and acceptance criterion.
