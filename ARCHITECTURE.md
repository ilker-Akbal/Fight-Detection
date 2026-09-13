# Fight-Detection Architecture Contract

## Document ownership and maintenance discipline

This file is the architecture contract for the repository. It describes the **current Supervisor-managed production architecture**, ownership and failure boundaries, identity/fencing rules, live-vs-file semantics, durability boundaries, health/recovery behavior, shutdown ownership, qualification methodology, performance interpretation, measured characterization evidence, and intentionally deferred work.

**Maintenance rule:** coding agents (including Codex) must read this file before architecture-affecting work and **must not modify it**. The project owner and ChatGPT maintain it from committed repository state.

Architecture refreshes are not a narrow “append the latest commit” exercise. Before changing this file, the maintainer must inspect the committed implementation and tests for the affected ownership paths, remove stale/contradictory claims, keep measurements separate from estimates, and preserve inherited invariants unless a later accepted phase explicitly changes them.

Current production-code reference commit:

```text
06a67cc4328b602493ef9ef6d87916ec11e9b630
Add Phase 22 live runtime qualification and shutdown hardening
```

**Phase 22 is committed on `master` and is the current production architecture baseline.** It does not introduce another runtime. It adds a bounded LIVE qualification harness around the existing `RuntimeSupervisor -> run_multiprocess -> CameraRuntimeManager` path and hardens Windows shutdown ownership discovered during that qualification.

Phase 20/21 remains the recovery-isolation foundation: camera/source lifetime, Fight-consumer lifetime, Speed-consumer lifetime, Fight service lifetime and Vehicle service lifetime are distinct. Phase 22 measures and validates those boundaries without changing model thresholds, calibration, ordered-file semantics, Django models, UI, deployment topology or the default Person batching policy.

Final reported validation for the current baseline:

```text
full pytest:
  257 passed, 26 subtests passed

compileall fight benchmarks tests:
  passed

git diff --check:
  passed

Phase-22 generated LIVE-like smoke:
  baseline                 PASS
  fight-shared recovery    PASS_WITH_RECOVERY
  shutdown-backoff         PASS_WITH_RECOVERY

Person batching default:
  OFF
```

Earlier in Phase 22, focused qualification/recovery suites and dedicated harness tests also passed. The final full-suite count above supersedes earlier intermediate counts.

Generated/local qualification is **not** proof of real RTSP/network behavior, detection quality, full Fight Pose/Stage3 contention, or target-hardware capacity. Real RTSP interruption and long-duration soak remain environment-specific acceptance work.

---

# 1. System purpose and current topology

The repository is a centralized multi-camera security platform in which Fight Detection and Speed Detection share source/runtime infrastructure while preserving camera-local temporal state.

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
            -> bounded health/performance/attribution reporting
    -> durable incident outbox
    -> Django Incident Dispatcher
    -> common Incident / routing / authorization domain
```

The production design is deliberately **not** “one complete AI pipeline per camera”. Expensive/stateless inference is shared across cameras. Temporal interpretation that depends on one camera’s history remains camera-local.

The runtime distinguishes separate lifetimes:

```text
SOURCE / CAMERA INCARNATION
  CameraIngest + physical source + Preview + camera generation

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
```

These identities are intentionally separate. A Fight-consumer replacement is not a source replacement. A shared Fight-service replacement is not a camera-generation replacement. A Speed replacement is not a Fight replacement.

Target scale is large multi-camera deployment, but this contract does **not** assert a production camera-count guarantee. RTX 3050 results are characterization evidence for specific workloads, not an RTX 5090 estimate, sustained RTSP SLA, or proof that 200/300 real cameras fit one node.

---

# 2. Non-negotiable invariants

1. Runtime workers must not depend on Django ORM.
2. Django/Gunicorn is the application/control plane, not the AI child-process owner.
3. Runtime Supervisor owns the global production AI runtime lifecycle.
4. `run_multiprocess` owns multiprocessing topology below the Supervisor.
5. One physical camera has exactly one intended `CameraIngest` source/decode owner in normal Supervisor operation.
6. Fight and Speed on the same physical camera share one desired camera entry and one CameraIngest decode path.
7. Recovery must never create a second CameraIngest as a workaround.
8. `camera_worker`, `speed_worker`, Preview and web views must not independently reopen the production source.
9. Expensive/stateless inference models are shared services, not model-per-camera instances.
10. Fight temporal tracking/pair/ROI/event state and Speed tracking/calibration/speed state remain camera-local where history matters.
11. Shared services are capability-aware and run only while desired cameras require them.
12. Capability changes do not require a global runtime restart solely because capabilities changed.
13. Source/camera incarnation is fenced by stable slot + camera generation.
14. Fight consumer incarnation adds a separate parent-owned Fight epoch when the Fight consumer can change without changing source generation.
15. Speed consumer identity uses its own parent-owned epoch.
16. Recoverable shared services use service epochs where required.
17. Fight durable publication is fenced by shared-service and per-Fight-consumer publication floors.
18. Old work may physically finish, but stale work cannot become current health/result/durable-incident truth.
19. Live and ordered-file workloads intentionally use different backpressure/recovery semantics.
20. Correctness must not depend on OS `Queue.qsize()` or `Queue.empty()` observations.
21. Telemetry, queues, retries, health scans and cleanup remain bounded.
22. Windows `spawn` compatibility is a first-class requirement.
23. Runtime incident truth crosses to Django through the durable incident outbox; runtime workers do not create Incident ORM rows.
24. Fight and Speed share the same Incident/routing/authorization domain.
25. Optional service absence is healthy when that service is not required.
26. Incident and Reporter remain runtime-global liveness-critical processes.
27. Synthetic camera-equivalents are not production inference capacity.
28. One GPU/workload result must not be linearly extrapolated to another GPU/workload.
29. Benchmark/qualification code must not silently retune production thresholds, batching, calibration or ownership merely to obtain better results.
30. PostgreSQL, Docker, Nginx, deployment/service packaging and UI redesign remain frozen unless explicitly promoted.
31. Shared-memory transport remains deferred until controlled measurement proves transport is materially limiting.
32. Ordered non-looping file EOF is correctness state, not telemetry.
33. CameraIngest sets authoritative generation-local EOF before consumer EOF signals.
34. A dead required file consumer is clean only with authoritative EOF + exit code 0.
35. Clean EOF does not advance generation, reopen source, replay the file or synthesize watchdog recovery.
36. Fight/Vehicle failures during ordered-file work remain fail-closed/no-replay.
37. Attribution/performance telemetry never drives health, admission, epochs, recovery, EOF, durable publication or benchmark/qualification correctness.
38. Missing/disabled/no-sample metrics remain null/unavailable rather than fabricated zero.
39. Normal graceful finalization remains distinct from failure teardown.
40. Final telemetry is best-effort observability, not durability or EOF truth.
41. Health snapshot publication failure is non-fatal but observable; retry stays bounded and narrow.
42. Failure reporting preserves the pre-teardown cause when available.
43. Forced-kill exit codes must not be misreported as the original stall cause.
44. Person microbatching remains configurable but **OFF by default**.
45. Fight recovery preserves healthy mixed-camera Speed state whenever source/Speed ownership is healthy.
46. Camera-local Fight failure does not automatically recycle the shared Fight bundle.
47. Shared Fight-bundle failure does not automatically recycle CameraIngest, Preview, Speed or Vehicle.
48. Global stop is authoritative: reconnect/recovery/backoff may not recreate work after shutdown begins.
49. Runtime parent owns orderly child teardown. Group signals must not allow children to bypass parent-owned shutdown sequencing.
50. Qualification fault injection is opt-in and may target only processes verified to belong to the current qualification run.
51. Qualification output directories are immutable-by-convention: do not reuse/overwrite an existing run directory.
52. Generated/live-like input must be explicitly labeled synthetic and must never be presented as RTSP/network proof.

---

# 3. Ownership planes

## 3.1 Django/application plane

Django owns persisted camera configuration, `SpeedCameraConfig`, Location/security organization, Incident ORM rows, routing/audit/ACK/resolve state, desired-camera publication, Supervisor start/stop requests, operator-facing status/preview/action endpoints, retention/reference protection and the independent Incident Dispatcher service loop.

Django may observe runtime state and consume preview/evidence. It must not become a hidden second AI runtime or source owner.

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

The Supervisor owns process-group start/stop semantics and the final bounded fallback when the runtime parent does not stop within the configured grace period.

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
- desired-state reconcile,
- health registry/watchdog/snapshot publication,
- fair admissions,
- ordered-file EOF/drain/finalization,
- performance summary,
- orderly child shutdown.

Lifecycle policy remains parent-owned. Children receive only spawn-safe queues, Events/Arrays, simple identities and configuration required for their role.

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

# 5. Desired camera state and capability reconciliation

Primary files:

```text
fight/runtime_supervisor/camera_state.py
Fight_backend_project/backend_frontend_project/services/pipeline_bridge/camera_registry.py
fight/pipeline_mp/camera_lifecycle.py
```

Schema version remains `1` with canonical fields including:

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

Desired state has a monotonic revision. Stale/equal revisions must not create duplicate lifecycle work. `speed_paused` remains durable desired intent.

## 5.1 Source-preserving LIVE capability changes

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

`MAX_CAMERAS = 512` remains a schema/registry bound, not a capacity claim.

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

Fight or Vehicle service recovery is never permission to open a second source.

## 6.1 Independent branch gating

Fight and Speed branch queues are allocated for the source runtime and gated independently.

Fight publication uses parent-owned Fight pause/epoch state. Disabled Fight is not counted as a dropped Fight frame merely because work was intentionally not offered. Speed uses its own stop/epoch semantics. Preview is independent.

This separation allows the source to remain alive while a Fight consumer is withdrawn/replaced.

## 6.2 LIVE reconnect

Reconnect is CameraIngest-owned, bounded and exponential. Phase 20/21 made reconnect waits stop-aware so shutdown does not block on an uninterruptible sleep.

Reconnect delay resets only after real frame flow resumes, not merely after reopening a handle that still produces no frames.

Temporary source silence must not independently trigger Fight/Vehicle bundle replacement simply because no frames are arriving.

## 6.3 Ordered-file EOF

For a non-looping local file, each `CameraRuntime` owns a fresh generation-local `multiprocessing.Event` representing authoritative EOF.

```text
legitimate file EOF
 -> set EOF Event
 -> publish consumer EOF signal(s)
 -> consumers drain/exit
 -> manager/watchdog classify completion
```

A later Fight consumer epoch cannot replace or reinterpret source-generation EOF truth.

Blocked ordered Fight error/EOF publication observes Fight withdrawal/stop guards so an intentionally removed Fight reader cannot deadlock CameraIngest indefinitely.

---

# 7. CameraRuntimeManager lifecycle

Primary implementation:

```text
fight/pipeline_mp/camera_lifecycle.py::CameraRuntimeManager
```

Per-camera runtime state includes desired camera definition, slot/generation, source stop controls, Fight/Speed/Preview queues, generation-local file EOF, Fight epoch/pause/stop/failure/retry state, Speed epoch/stop/failure/retry state, process handles and lifecycle state.

Normal composition:

```text
Fight-only:   CameraIngest + camera_worker + Preview
Speed-only:   CameraIngest + speed_worker + Preview
Fight+Speed:  CameraIngest + camera_worker + speed_worker + Preview
```

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

---

# 8. SharedServices and service lifecycle

Primary implementation:

```text
fight/pipeline_mp/shared_services.py::SharedServices
```

Required service bundles derive from active desired capabilities:

```text
fight   = any active camera requiring Fight
vehicle = any active camera requiring Speed
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

Fight event/evidence IDs include source generation and Fight consumer incarnation so a restarted camera worker cannot reuse a local counter value and overwrite earlier evidence.

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

---

# 13. Live vs ordered-file semantics

## 13.1 LIVE / RTSP

Priority:

```text
freshness > completeness
```

Expected behavior:

- bounded queues,
- stale live work may be shed explicitly,
- reconnect is ingest-owned,
- bounded local retries,
- eligible LIVE Speed resumes after Vehicle recovery,
- eligible LIVE Fight resumes after Fight recovery,
- unrelated branches/services survive recoverable faults where safe,
- same-source capability churn preserves source generation where possible.

## 13.2 FILE

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

Phase 22 does not change these rules.

---

# 14. Incident durability boundary

Primary files:

```text
fight/pipeline/incident_outbox.py
fight/pipeline/incident_aggregator.py
fight/pipeline_mp/incident_worker.py
fight/pipeline_mp/speed_worker.py
```

Runtime produces evidence and durable incident envelopes before Django ingestion.

Core semantics:

- append-only JSONL incident outbox,
- serialized writers,
- flush/fsync where designed before successful publication,
- partial-tail preservation,
- persistence failure surfacing,
- evidence durability before incident publication,
- stale generation/epoch guards,
- Fight service + consumer publication floors,
- Speed generation + consumer-epoch fencing.

Runtime never directly creates Incident ORM rows.

## 14.1 Qualification evidence limitation

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

Current baseline:

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

---

# 26. Task router for coding agents

Read this document first and inspect current Supervisor-managed ownership paths rather than inferring architecture from legacy helpers.

## Supervisor / Windows shutdown

```text
fight/runtime_supervisor/core.py
fight/runtime_supervisor/camera_state.py
fight/runtime_supervisor/locking.py
fight/pipeline_mp/common.py
fight/pipeline_mp/run_multiprocess.py
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

---

# 27. Cross-phase acceptance checklist

Before accepting architecture-affecting work, verify as applicable:

```text
[ ] ARCHITECTURE.md was read first and coding agent did not modify it
[ ] one Runtime Supervisor owns the global AI runtime
[ ] one CameraIngest remains the source/decode/reconnect owner per physical camera
[ ] no recovery path opens a duplicate physical source
[ ] Fight+Speed still share one source decode
[ ] runtime remains Django-ORM-free
[ ] camera-local temporal/calibration state stays local
[ ] optional shared services start only when required
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
[ ] ordered-file EOF remains generation-local authoritative state
[ ] clean file completion still requires EOF + exitcode 0
[ ] failed ordered work cannot later become clean because EOF arrived
[ ] ordered FILE recovery never replays lost work
[ ] fair scheduling/capacity stays bounded
[ ] no correctness dependency on OS qsize()/empty()
[ ] health remains generation/consumer/service-epoch aware
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
[ ] generated runtime/benchmark artifacts are not staged
[ ] UI/PostgreSQL/Docker/Nginx/deployment remain frozen unless explicitly promoted
```

---

# 28. Generated artifacts / repository hygiene

Operational/generated paths include:

```text
.runtime_supervisor/
Fight_backend_project/backend_frontend_project/media/camera_uploads/
Fight_backend_project/backend_frontend_project/media/pipeline_runs/.run_locks/
Fight_backend_project/backend_frontend_project/media/runtime_spool/
benchmarks/results/
benchmarks/.capacity.lock
phase*_review.diff
```

Each benchmark/qualification run gets a new result directory. Historical results are not overwritten to obtain a cleaner number.

Private LIVE configs and raw logs may contain source credentials or network details and must not be committed.

---

# 29. Deferred / frozen work after Phase 22

Highest-value next qualification work:

- real external RTSP baseline with representative FPS/resolution/network path,
- deliberate RTSP/network interruption and reconnect validation,
- long-duration soak with source churn, capability churn, Fight/Vehicle recovery and storage growth,
- repeated orphan/process-tree checks under real Windows long runs,
- mixed Fight+Speed workloads that actually exercise Pose and Stage3,
- target-hardware RTX 5090 characterization using the real machine,
- realistic incident/evidence end-to-end latency and duplicate-incident validation,
- production-class storage/retention/operator observability sizing.

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
frontend/dashboard redesign
incident UX redesign
preview/offline UX redesign
```

---

# 30. Qualification strategy after Phase 22

Phase 22 changes the next question from “can we simulate the recovery state machine?” to “does the same ownership contract hold under representative real live conditions for long enough to trust operationally?”

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

For target-hardware capacity, add:

```text
aggregate and per-camera service rate
Person/Pose/Stage3 queue + inference + result enqueue
Vehicle admission-inclusive wait + inference + result enqueue
Fight/Speed camera-local processing
queue high-water / rejection / live shedding
Reporter/snapshot reliability over long duration
```

Decision rule:

```text
If shared model service time/queueing dominates:
    evaluate batching, worker concurrency or model partitioning.

If camera-local CPU work dominates:
    optimize that subsystem first.

If transport remains dominant after controlling backlog/fanout:
    run a shared-memory transport experiment before adopting it.

If one service harms another under mixed load:
    optimize total-system fairness/throughput, not one queue in isolation.

If target GPU behavior differs materially from RTX 3050:
    prefer target measurements over laptop tuning conclusions.
```

No camera-count or RTSP SLA claim belongs in this contract without a clearly described real workload, hardware, source characteristics, duration and acceptance criterion.
