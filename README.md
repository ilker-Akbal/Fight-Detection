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

No Django ORM dependency is imported by the fight runtime. Gunicorn does not own
AI children, and the Runtime Supervisor does not own Django workers.

## Local Windows startup

Run these in three separate PowerShell terminals from the repository root.

Terminal 1 — Runtime Supervisor:

```powershell
python -m fight.runtime_supervisor.server
```

Terminal 2 — Incident Dispatcher:

```powershell
Set-Location Fight_backend_project/backend_frontend_project
python manage.py run_incident_dispatcher
```

Terminal 3 — Django:

```powershell
Set-Location Fight_backend_project/backend_frontend_project
python manage.py runserver
```

For a file camera, EOF produces a clean `STOPPED` runtime with exit code 0; this
is expected completion. A live RTSP camera remains active according to the
configured reconnect and explicit-stop policy.

## Deferred work

The following work belongs to dedicated later phases:

- dashboard redesign;
- incident table lazy media loading / video player UX;
- operational incident interaction redesign;
- preview UX and offline placeholders;
- production media offload/Nginx;
- Speed integration;
- PostgreSQL migration.
