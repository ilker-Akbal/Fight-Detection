# ▶️ Pipeline Çalıştırma

## Motion Test

```text
python -m fight.pipeline.run_live --motion-config fight/motion/configs/motion.yaml --show
```

---

## Webcam ile Tam Pipeline

```text
python -m fight.pipeline.run_live --motion-config fight/motion/configs/motion.yaml --yolo-config fight/yolo/configs/yolo.yaml --use-pose --pose-weights fight/pose/weights/yolo11n-pose.pt --use-stage3 --stage3-config fight/3D_CNN/configs/stage3.yaml --show
```

---

## Video ile Pipeline

```text
python -m fight.pipeline.run_live --source fight/sample_2.mp4 --motion-config fight/motion/configs/motion.yaml --yolo-config fight/yolo/configs/yolo.yaml --use-pose --pose-weights fight/pose/weights/yolo11n-pose.pt --use-stage3 --stage3-config fight/3D_CNN/configs/stage3.yaml --show
```

---

# 📁 Proje Klasör Yapısı

```text
fight
 ├── motion
 ├── yolo
 ├── pose
 ├── 3D_CNN
 ├── pipeline
 ├── shared
 ├── tools
 └── clip_debug
```

---

# 📌 Not

Model `.pt` dosyalarına erişim yoksa modeli yeniden paketlemek için şu araç kullanılabilir:

```text
fight/tools/pack_pt_from_folder_v2.py
```

---

## Production service boundaries

The Django/Gunicorn process is the control and web plane. It does not own AI
processes or open physical fight-camera sources. The Runtime Supervisor owns one
`run_multiprocess` parent. Within that run, `CameraIngest` is the only physical
source/decode owner and fans frames out to one shared Person worker, one shared
Pose worker, and shared Stage3/X3D inference. `IncidentAggregator` finalizes
incident evidence and writes the durable incident outbox; the Django-side
Incident Dispatcher imports that boundary into the current SQLite database and
performs routing and escalation.

The Camera Registry Reconciler publishes a versioned snapshot of active fight
cameras to the Supervisor's atomic desired-state file. The running AI parent
assigns cameras to pre-created result-channel slots and starts, stops, or
restarts only the affected ingest/camera/preview trio. Shared inference workers
remain alive. Publishing camera state never starts a globally stopped runtime;
global start/stop remains owned by the Supervisor API.

No Django ORM dependency is imported by the fight runtime. Gunicorn does not own
AI children, and the Runtime Supervisor does not own Django workers.

## Local Windows startup

Run these in four separate PowerShell terminals from the repository root.

Terminal 1 — Runtime Supervisor:

```powershell
python -m fight.runtime_supervisor.server
```

Terminal 2 — Camera Registry Reconciler:

```powershell
Set-Location Fight_backend_project/backend_frontend_project
python manage.py run_camera_registry_reconciler
```

Terminal 3 — Incident Dispatcher:

```powershell
Set-Location Fight_backend_project/backend_frontend_project
python manage.py run_incident_dispatcher
```

Terminal 4 — Django:

```powershell
Set-Location Fight_backend_project/backend_frontend_project
python manage.py runserver
```

For a file camera, EOF produces a clean `STOPPED` runtime with exit code 0; this
is expected completion. A live RTSP camera remains active according to the
configured reconnect and explicit-stop policy.

## Runtime health and watchdog

The AI runtime owns a bounded health channel, an in-memory current-state
registry, and one periodic watchdog tick. Health decisions use monotonic time.
The atomic `runtime_health.json` snapshot contains generation-aware camera and
shared-worker status, progress ages, bounded counters, and reasons; it never
contains source URLs, credentials, frames, or heartbeat history.

Camera lifecycle and health remain separate. A running camera can be
`DEGRADED`; a non-looping file can finish as `EOF`; an RTSP source in its normal
ingest reconnect loop is `RECONNECTING`. The watchdog does not compete with
`CameraIngest` reconnects. A confirmed camera stall restarts only that camera
through the Phase-9 `CameraRuntimeManager`, with a generation increment,
cooldown, and bounded retry count. Preview failures use preview-only restart.
One unhealthy camera degrades the aggregate runtime but does not fail it.

Person, Pose, Stage3, Incident, and result-router workers remain healthy while
idle if their heartbeats are fresh. A dead worker, stale heartbeat, or pending
request whose completion stops progressing has a distinct reason. A fatal
critical shared-worker condition causes controlled runtime failure; the Runtime
Supervisor then applies its existing whole-runtime restart/backoff policy.
Shared CUDA workers are not hot-replaced independently in Phase 10.
While synchronous inference is in progress, its inference warning/failure
deadlines take precedence over the loop heartbeat timeout. The first inference
gets at least `HEALTH_STARTUP_GRACE_SEC` from request start for lazy CUDA warm-up;
later requests use `INFERENCE_STALL_FAIL_SEC`. Cameras waiting on that shared
inference remain degraded without a camera restart during this bounded window.
Actual process death is always fatal for a critical shared worker.
The Django-side Camera Registry Reconciler and Incident Dispatcher remain
outside this AI runtime registry and are monitored by deployment/service
management.

The Supervisor's existing `/status` response adds `runtime_health`,
`runtime_health_updated_at`, and `health_summary`. The detailed bounded snapshot
is available from the authenticated endpoint:

```powershell
$headers = @{ Authorization = "Bearer $env:RUNTIME_SUPERVISOR_TOKEN" }
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8765/runtime/health" `
  -Headers $headers |
  ConvertTo-Json -Depth 10
```

When the runtime is stopped, detailed health is unavailable with
`runtime_health=STOPPED`. A missing snapshot while running is `UNKNOWN`; a stale
snapshot is explicitly marked `stale=true` and `DEGRADED` unless the last
reported aggregate state was already `FAILED`.

Health tuning is environment/config driven. The primary settings are
`HEALTH_HEARTBEAT_INTERVAL_SEC`, `HEALTH_WATCHDOG_INTERVAL_SEC`,
`HEALTH_STARTUP_GRACE_SEC`, `CAMERA_HEARTBEAT_TIMEOUT_SEC`,
`CAMERA_FRAME_STALL_WARN_SEC`, `CAMERA_FRAME_STALL_FAIL_SEC`,
`CAMERA_RECONNECT_GRACE_SEC`, `SHARED_WORKER_HEARTBEAT_TIMEOUT_SEC`,
`INFERENCE_STALL_WARN_SEC`, `INFERENCE_STALL_FAIL_SEC`,
`PREVIEW_HEARTBEAT_TIMEOUT_SEC`, `WATCHDOG_CAMERA_RESTART_COOLDOWN_SEC`, and
`WATCHDOG_CAMERA_RESTART_LIMIT`. Defaults are conservative; see `.env.example`.

## Capacity and backpressure (Phase 11)

The Supervisor-managed dynamic runtime enables `FAIR_SCHEDULING_ENABLED=true`.
Each shared Person/Pose worker receives one reserved pending FIFO position per
stable camera slot. Stage3 reserves `STAGE3_PENDING_PER_CAMERA` positions per
slot (default 1). Workers consume these FIFOs round-robin, then use the existing
size/wait-bounded microbatch collector and shape grouping. Model counts do not
change. Cameras retain one outstanding Person/Pose request; only a camera's own
producer endpoints are passed to its processes on Windows spawn.

Admission capacity is explicit: `DYNAMIC_CAMERA_SLOT_COUNT` positions per
Person/Pose stage and slots times `STAGE3_PENDING_PER_CAMERA` for Stage3, plus
the bounded active batch and each producer's current work. Legacy shared
`person_request_queue_size`, `pose_request_queue_size`, and `stage3_queue_size`
apply when fair scheduling is disabled; they do not override slot reservations.
Increasing slot count reserves queue handles and payload capacity, not models.

File inference remains ordered: a full slot defers its producer and accepted
work waits for its result. Stage3 candidates are also ordered for both source
types, so saturation does not discard incident evidence. Live ingest retains
its existing latest-frame replacement policy. `LIVE_FRAME_MAX_AGE_SEC` and
`LIVE_INFERENCE_MAX_AGE_SEC` (both default 2 seconds, 0 disables the age limit)
shed stale live frames/requests/results. A shed inference returns an explicit
outcome; it is never interpreted as a negative Person/Pose decision. An
in-flight synchronous model call is not preempted; live age bounds determine
whether its work remains useful when dispatching/receiving it.

Cooperative capacity waits report `DEGRADED / queue_pressure` without camera
restarts. Phase-10 shared-worker death, heartbeat, inference deadlines and
first-call warm-up grace remain authoritative. File EOF drains admitted Stage3
work, including active inference, while the parent continues health checks.
The existing incident finalization wait then runs before clean completion.

Health snapshots include per-stage and per-camera `capacity` counters:
accepted, rejected_capacity, deferred_file, dropped_live, stale_generation,
dispatches, and high_water. `CAPACITY_OVERLOAD_RATIO` (default 0.9) controls
aggregate saturation warnings; it is not a restart trigger. Outstanding/high
water observations include active work and a producer blocked in admission;
they are not multiprocessing `qsize()` correctness checks. Slot counters reset
on a new camera generation. Final `performance_summary.json` includes capacity
statistics even when periodic health snapshots are disabled.

These are fairness and bounded-memory guarantees, not a GPU throughput claim.
Decoding, frame/ROI serialization, Stage3 clip payloads, per-camera processes,
Windows queue handles and GPU service time remain real scale costs. Phase 12
should measure end-to-end latency, CPU/RAM/VRAM and sustained load on target
hardware before choosing shared-memory transport or multi-GPU partitioning.

## Deferred work

The following work belongs to dedicated later phases:

- dashboard redesign;
- incident table lazy media loading / video player UX;
- operational incident interaction redesign;
- preview UX and offline placeholders;
- production media offload/Nginx;
- Speed integration;
- PostgreSQL migration;
- production-scale capacity measurement and deployment sizing (Phase 12).
