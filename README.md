# Shared Multi-Camera Fight + Speed Detection

A multi-camera video analytics system with shared inference services, camera-local
temporal processing, and a durable incident boundary into Django. Fight-only,
Speed-only and combined cameras run under one Supervisor-managed runtime.

[ARCHITECTURE.md](ARCHITECTURE.md) is the architecture contract.
[benchmarks/README.md](benchmarks/README.md) defines measurement methodology,
metric boundaries and reproducible benchmark commands.

## Architecture

```text
Django desired-camera registry -> Runtime Supervisor -> one runtime parent
  CameraIngest per physical source
    -> Fight consumer -> shared Person / optional Pose / optional Stage3
    -> Speed consumer -> shared Vehicle
    -> Preview consumer
  Evidence + durable incident outbox -> Django Incident Dispatcher
```

- **Shared models:** one Person inference service for Fight, shared Pose and
  Stage3/X3D when configured and required, and one Vehicle inference service for
  Speed. Models are shared across cameras, not loaded per camera. Fight temporal
  state and Speed tracking/calibration/decisions remain camera-local.
- **Single decode owner:** production sources are opened by CameraIngest, once
  per active physical source. Fight and Speed on the same camera share its decoded
  frames. Duplicate active physical-source ownership is rejected.
- **Capability-aware lifecycle:** services start when the desired cameras require
  them and can stop after demand disappears and required work drains.

| Camera mode | Required shared services |
|---|---|
| Fight-only | Person; Pose/Stage3 when configured; no Vehicle |
| Speed-only | Vehicle; no Person/Pose/Stage3 merely because the runtime exists |
| Fight + Speed | Both applicable service groups, sharing one CameraIngest |

The Django/Gunicorn process is the application/control plane, not the AI process
owner. The Camera Registry Reconciler publishes versioned desired camera state;
the runtime parent owns dynamic add/remove/reconfigure and stable camera slots.
Runtime workers have no Django ORM dependency.

Vehicle failure/recovery is isolated from Fight. Fight bundle recovery leaves
Speed-only cameras intact; a mixed camera currently incurs a camera-local restart
and brief Speed interruption during Fight recovery. Generations, Speed consumer
epochs, shared-service epochs and restart state fence stale work. Recovery uses
bounded retries/backoff; affected file workloads fail closed without replay.
Runtime-global Reporter/Incident failures and exhausted or unsafe Fight recovery
remain explicit failure boundaries, not silently ignored faults.

## Scheduling, observability and durability

- Fair per-camera admission and round-robin shared-service scheduling provide
  bounded backpressure. Ordered files defer rather than silently shed required
  inference; live sources may shed stale work explicitly. No correctness decision
  relies on OS queue `qsize()` or `empty()`.
- CameraIngest owns live reconnects. The watchdog observes heartbeats, progress,
  inference deadlines, warm-up grace and queue pressure without competing with
  reconnect ownership or restarting solely because of ordinary backpressure.
- Non-looping file EOF is a generation-local event published before consumer EOF
  signals. Required consumers must exit cleanly after authoritative EOF; mixed
  completion waits for both Fight and Speed. Telemetry never owns EOF correctness.
- Evidence and the append-only durable outbox precede legacy incident output.
  The independent Django dispatcher imports Fight/Speed events into the common
  Incident/routing domain. Serialized writes and fsync protect the persistence
  boundary; persistence failure is not reported as success.
- Bounded, best-effort attribution measures ingest read/fan-out, shared inference
  waits, client round trips and camera-local work. Missing metrics remain
  unavailable. Attribution cannot change health, admission, recovery, incidents
  or benchmark classification.
- Multiprocessing transport and lifecycle paths support Windows `spawn`; there
  is no shared-memory frame-transport optimization or extra model worker implied
  by these measurements.

### Phase 19 / 19.1 reliability

Normal shared-service withdrawal sends queue sentinels and grants one bounded
eight-second finalization grace per bundle while routers and Reporter remain
available. Normal worker joins flush their report feeders; Reporter is joined
and flushed before final performance-summary construction. Worker queue,
inference, enqueue, batch and existing steady-state telemetry survive normal
shutdown. Failure recovery retains bounded forced teardown, not an unlimited drain.

On Windows, HealthSnapshotStore retries only atomic replacement errors with
`winerror` 5, 32 or 33: four total attempts, with 20/40/80 ms delays (140 ms maximum
added backoff). An overlapping reader can deny replacement; the last complete
JSON remains intact until replacement succeeds. Persistent permission/disk errors
still surface. Snapshot publication failures remain best-effort/non-fatal and
include `errno`/`winerror`.

Vehicle recovery status retains `component`, `reason`, `component_failure`,
`service_epoch`, retries and the pre-teardown exit code when available. A stalled,
still-live worker does not falsely acquire its later forced-kill exit code as the
original cause. Equivalent Fight health-failure identity is retained.

## Local development and operation

Use an environment with the repository's runtime/model dependencies and a
compatible PyTorch/CUDA installation for GPU work. Provision model weights and
camera-specific Speed calibration before starting inference; the benchmark
harness does not automatically download missing models. Backend dependencies are
listed in [requirements.txt](Fight_backend_project/backend_frontend_project/requirements.txt);
configuration examples are in [.env.example](.env.example). These are development
instructions, not production deployment packaging.

Run these in separate PowerShell terminals from the repository root:

```powershell
# 1. Runtime Supervisor
python -m fight.runtime_supervisor.server

# 2. Camera Registry Reconciler
Set-Location Fight_backend_project/backend_frontend_project
python manage.py run_camera_registry_reconciler

# 3. Incident Dispatcher (start from repository root)
Set-Location Fight_backend_project/backend_frontend_project
python manage.py run_incident_dispatcher

# 4. Django development server (start from repository root)
Set-Location Fight_backend_project/backend_frontend_project
python manage.py runserver
```

The four blocks belong in separate terminals. Configure authentication and desired
cameras before using the common start/stop controls. Publishing desired state does
not itself start a globally stopped runtime.

The authenticated Supervisor `/status` and `/runtime/health` endpoints expose
runtime health, camera/service identity, progress, capacity and reasons. Missing
or stale snapshots are explicitly identified. Health deadlines and recovery
budgets are configuration-driven; do not inflate them to conceal failed workers.

Operational cleanup is opt-in at the ORM-aware Django boundary:

```powershell
python manage.py run_operational_cleanup --once --dry-run
python manage.py run_operational_cleanup --once
```

Run cleanup from the backend directory. Active/current runs, recovery state,
unconsumed outbox data and Incident-referenced evidence are protected. Evidence
retention is indefinite by default; bounded transient cleanup and disk-pressure
status do not replace storage planning. Disk pressure alone does not trigger
watchdog restart storms.

Legacy single-camera developer tools remain available, but do not define the
shared production topology:

```powershell
python -m fight.pipeline.run_live --motion-config fight/motion/configs/motion.yaml --show
```

## Real-inference characterization

Hardware: **NVIDIA GeForce RTX 3050 Laptop GPU, 6 GB; Intel Core i7-13700H;
64 GB RAM; Windows**.

These are healthy, ordered local-file workload characterizations, not production
capacity guarantees or sustained RTSP service-level claims. Fight and Speed
workloads have been characterized up to 12 logical cameras on this hardware.
That does not establish a universally supported camera count. No RTX 5090/other-GPU
extrapolation or 200/300-camera real-inference capability is claimed.

Aggregate throughput below is completed-file frames divided by runtime wall time
(the harness's `aggregate_decode_effective_fps_full_run`), including startup and
drain—not model requests per second or steady-state camera FPS. Shared latency
distributions retain bounded samples; host/GPU sampling excludes the configured
initial wall warm-up. Failed/incomplete runs are excluded from performance tables.

### Speed-only

| Metric | 8 cameras | 12 cameras |
|---|---:|---:|
| Aggregate throughput, FPS | 82.70 | 89.25 |
| Vehicle requests / delivered results | 1344 / 1344 | 2016 / 2016 |
| Vehicle queue wait mean, ms | 77.19 | 125.50 |
| Vehicle queue wait p95, ms | 119.28 | 256.63 |
| Vehicle inference mean, ms | 29.13 | 29.89 |
| Vehicle inference p95, ms | 42.27 | 51.55 |
| CPU mean | 51.45% | 56.69% |
| GPU mean | 22.47% | 26.01% |
| GPU p95 | 43.3% | 42.0% |
| GPU max | 45% | 53% |
| Classification / recovery | HEALTHY / none | HEALTHY / none |

With 50% more cameras, aggregate throughput increased only **7.92%**, while queue
latency rose materially. Similar inference duration and relatively low sampled
GPU utilization point toward shared Vehicle serialization/scheduling, arrival
pressure and backpressure rather than simple GPU saturation. This does not prove
IPC/memory-copy causality; sampled utilization can miss short GPU bursts. Vehicle
queue wait includes admission and transport, not just time after enqueue.

### Historical Fight-only

| Cameras | Aggregate throughput, FPS |
|---|---:|
| 2 | 28.67 |
| 4 | 49.89 |
| 8 | 65.55 |
| 12 | 76.80 |

The healthy post-Phase-17 Fight-12 run completed **903 frames per camera, 10,836
total**, without recovery, replay or restart. These are frame counts, not Person
request counts: Person/Pose/Stage3 processed 5004/3732/72 respectively. Person queue
p95 was about 206.9 ms, Pose queue p95 132.1 ms, GPU mean 57.5%, GPU p95 86%, and
VRAM about 640 MiB. These historical runs precede the newer attribution/finalization
changes and must not be treated as directly interchangeable microbatch baselines.

### Mixed Fight + Speed: optional Person microbatch profile

Production defaults remain `person_batch_enabled=false`,
`person_batch_size=1`, `person_batch_max_wait_ms=0`. **Batch-2 / 5 ms is an optional
characterized profile, not a global default or universal production recommendation.**

All four comparison runs below were HEALTHY, with no recovery. They used eight
logical cameras and the same traffic-file workload (678 frames per camera).
Pose/Stage3 received no work on this traffic content, so this is not a full
Fight-event/Pose/Stage3 contention test.

| Pair / profile | Aggregate FPS | Wall time, s | Person queue mean, ms | Person queue p95, ms |
|---|---:|---:|---:|---:|
| A / OFF | 40.99 | 132.32 | 171.57 | 197.40 |
| A / Batch-2, 5 ms | 45.15 | 120.12 | 105.22 | 132.13 |
| B / OFF, after 19.1 | 42.370409 | 128.013870 | 166.882547 | 206.519370 |
| B / Batch-2, 5 ms, after 19.1 | 43.578609 | 124.464735 | 102.568315 | 132.865525 |

Pair A OFF Person inference mean was 29.67 ms. Pair A Batch-2 had actual batch
size mean 1.83 and batch inference mean/p95 41.81/56.47 ms.

Pair B OFF Person inference mean was 24.744726 ms with batching disabled.
Pair B Batch-2 reported Person inference mean 45.442546 ms, size limit 2,
maximum collection wait 5 ms, actual size mean/p95 1.988281/2, collection-wait
mean 1.205650 ms, and batch inference mean/p95 47.651872/57.641125 ms.
A batch processes approximately two requests: greater per-batch latency is not
by itself a regression when throughput improves and request queueing falls.
Per-request and per-batch retained distributions are distinct, not interchangeable.

Arithmetic means across these two healthy comparison pairs, using the values
printed above:

| Metric | OFF mean | Batch-2 mean | Change relative to OFF |
|---|---:|---:|---:|
| Throughput, FPS | 41.6802045 | 44.3643045 | +6.4397% |
| Wall time, s | 130.166935 | 122.2923675 | -6.0496% |
| Person queue mean, ms | 169.2262735 | 103.8941575 | -38.6064% |
| Mean of run-level queue p95 values, ms | 201.959685 | 132.4977625 | -34.3940% |

The last row is a descriptive mean of two run-level statistics, **not a pooled
p95**. Two comparison pairs do not establish statistical significance or a universal
speedup. Pair A's displayed inputs are rounded; exact calculation conventions and
local evidence identifiers are in the [benchmark notes](benchmarks/README.md#characterization-evidence-and-calculations).

Batch-4 / 5 ms was also healthy, with actual batch mean about 3.68 and Person queue
mean about 37.51 ms, but total throughput was 44.67 FPS versus Batch-2's 45.15 FPS
in that experiment. Vehicle queue mean/p95 increased from about 39.19/103.19 ms
to 76.96/168.36 ms. Batch-2 was the better tested mixed-system tradeoff on this
hardware; reducing Person queue pressure alone was not the objective.

### Post-19.1 real Windows validation

Final Mixed-8 OFF and Batch-2 runs were both HEALTHY with no recovery. Each recorded:

- zero `health_snapshot_write_failed` occurrences;
- Person accepted/dispatched: 2600/2600; Vehicle accepted/dispatched: 1344/1344;
- zero capacity rejections, `dropped_live` and `stale_generation` in those stages;
- zero Person and Vehicle restarts.

This is successful real-runtime validation consistent with the targeted regression
tests, not proof that Windows sharing/permission failures can never recur.

## Validation and scope

```powershell
python -m pytest -q
python -m compileall fight benchmarks tests
git diff --check
```

The suite includes deterministic health/lifecycle/accounting tests and Windows
spawn integration tests; it is not a substitute for representative GPU, live/RTSP,
long-soak or detection-quality validation. Benchmark outputs and runtime state
remain local, ignored artifacts, not versioned source.

Further work should follow measured bottlenecks: representative live/RTSP and
long-running workloads, camera churn, mixed-workload recovery, storage sizing and
target-hardware validation. Shared memory, additional inference workers, model
tuning, PostgreSQL/deployment changes and UI redesign are separate scopes—not
implied by these characterization results.
