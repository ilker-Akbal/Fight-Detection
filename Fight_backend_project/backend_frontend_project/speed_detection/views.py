from __future__ import annotations

import json
import mimetypes
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Iterator

import cv2
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import role_required
from services.speed_bridge.calibration_writer import (
    default_speed_calibration_path,
    is_speed_calibration_ready,
    resolve_speed_calibration_path,
    write_two_line_speed_calibration,
)
from services.speed_bridge.report_reader import build_speed_dashboard_report
from services.speed_bridge.runtime_state import speed_runtime
from services.speed_bridge.speed_runner import start_speed_pipeline, stop_speed_pipeline
from speed_detection.models import SpeedCameraConfig


MAX_SPEED_HISTORY_RUNS = 10


@never_cache
@login_required
def index(request):
    report = _pipeline_report()

    return render(
        request,
        "speed_detection/index.html",
        {
            "speed": report,
            "camera_cards": report.get("cameras", []),
            "events": report.get("events", []),
        },
    )


def _speed_runs_root() -> Path:
    return Path(settings.MEDIA_ROOT) / "speed_runs"


def _speed_run_dirs(limit: int = MAX_SPEED_HISTORY_RUNS) -> list[Path]:
    root = _speed_runs_root()

    if not root.exists() or not root.is_dir():
        return []

    dirs = [p for p in root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    return dirs[:limit]


def _active_speed_cameras() -> list[dict]:
    qs = (
        SpeedCameraConfig.objects
        .select_related("camera")
        .filter(
            enabled=True,
            camera__is_active=True,
            camera__use_speed_detection=True,
        )
        .order_by("camera__camera_id")
    )

    cameras = []

    for item in qs:
        cam = item.camera

        calibration_path = resolve_speed_calibration_path(
            item.calibration_path or default_speed_calibration_path(cam.camera_id),
            camera_id=cam.camera_id,
        )

        calibration_ready, calibration_reason = is_speed_calibration_ready(calibration_path)

        cameras.append(
            {
                "camera_id": cam.camera_id,
                "name": cam.name,
                "source": cam.source,
                "description": cam.description,
                "faculty": cam.faculty or "",
                "speed_limit_kmh": item.speed_limit_kmh,
                "tolerance_kmh": item.tolerance_kmh,
                "calibration_path": str(calibration_path),
                "calibration_ready": calibration_ready,
                "calibration_reason": calibration_reason,
                "roi_enabled": item.roi_enabled,
                "roi_polygon": item.roi_polygon or [],
                "save_snapshot": item.save_snapshot,
                "save_clip": item.save_clip,
                "admin_url": reverse("adminx:camera_edit", args=[cam.pk]),
                "stream_url": reverse("speed_detection:camera_stream", args=[cam.camera_id]),
            }
        )

    return cameras


def _tail_text(path: Path, max_chars: int = 4000) -> str:
    try:
        if not path.exists() or not path.is_file():
            return ""

        size = path.stat().st_size

        with path.open("rb") as f:
            f.seek(max(0, size - max_chars * 2))
            data = f.read()

        return data.decode("utf-8", errors="ignore")[-max_chars:].strip()
    except Exception:
        return ""


def _speed_run_error_text(run_dir: Path) -> str:
    candidates = [
        run_dir / "stderr.log",
        run_dir / "error.log",
        run_dir / "logs" / "stderr.log",
        run_dir / "logs" / "error.log",
    ]

    for path in candidates:
        text = _tail_text(path)

        if text:
            return text

    return ""


def _empty_report() -> dict:
    cameras = _active_speed_cameras()

    return {
        "running": False,
        "pid": None,
        "return_code": None,
        "started_at": None,
        "started_at_text": "-",
        "run_dir": "",
        "run_name": "",
        "source_count": len(cameras),
        "camera_count": len(cameras),
        "cameras": cameras,
        "events": [],
        "recent_status": [],
        "config": {},
        "last_error": "",
    }


def _safe_url_part(value: str, label: str) -> str:
    if not value:
        raise Http404(f"{label} boş.")

    value = str(value).strip()
    safe = Path(value).name

    if safe != value or "/" in value or "\\" in value:
        raise Http404(f"{label} geçersiz.")

    return safe


def _find_existing_event_file(run_name: str, file_name: str, kind: str) -> Path | None:
    try:
        run_name = _safe_url_part(run_name, "Run adı")
        file_name = _safe_url_part(file_name, "Dosya adı")
    except Exception:
        return None

    run_dir = (_speed_runs_root() / run_name).resolve()

    if not run_dir.exists() or not run_dir.is_dir():
        return None

    if kind == "snapshot":
        candidates = [
            run_dir / "snapshots" / file_name,
            run_dir / "events" / file_name,
        ]
    elif kind == "clip":
        candidates = [
            run_dir / "clips" / file_name,
            run_dir / "events" / file_name,
        ]
    elif kind == "preview":
        candidates = [
            run_dir / "previews" / file_name,
        ]
    else:
        candidates = []

    for path in candidates:
        path = path.resolve()

        try:
            path.relative_to(run_dir)
        except ValueError:
            continue

        if path.exists() and path.is_file():
            return path

    return None


def _event_media_url(run_name: str, path_value: str, kind: str) -> str:
    if not run_name or not path_value:
        return ""

    file_name = Path(str(path_value)).name

    if not file_name:
        return ""

    existing = _find_existing_event_file(run_name, file_name, kind)

    if existing is None:
        return ""

    if kind == "snapshot":
        return reverse("speed_detection:snapshot", args=[run_name, file_name])

    if kind == "clip":
        return reverse("speed_detection:clip", args=[run_name, file_name])

    return ""


def _preview_media_url(run_name: str, camera_id: str) -> str:
    if not run_name or not camera_id:
        return ""

    existing = _find_existing_event_file(
        run_name=run_name,
        file_name=f"{Path(str(camera_id)).name}.jpg",
        kind="preview",
    )

    if existing is None:
        return ""

    return reverse("speed_detection:preview", args=[run_name, camera_id])


def _normalize_report_media_urls(report: dict) -> dict:
    run_name = str(report.get("run_name") or "")

    normalized_cameras = []

    for camera in report.get("cameras", []) or []:
        row = dict(camera)
        camera_id = str(row.get("camera_id") or "")
        row["preview_url"] = _preview_media_url(run_name, camera_id)
        normalized_cameras.append(row)

    report["cameras"] = normalized_cameras

    normalized_events = []

    for event in report.get("events", []) or []:
        row = dict(event)
        event_run_name = str(row.get("run_name") or run_name or "")

        snapshot_path = (
            row.get("snapshot_path")
            or row.get("snapshot")
            or row.get("snapshot_file")
            or ""
        )

        clip_path = (
            row.get("clip_path")
            or row.get("clip")
            or row.get("clip_file")
            or ""
        )

        row["run_name"] = event_run_name
        row["snapshot_url"] = _event_media_url(event_run_name, snapshot_path, "snapshot")
        row["clip_url"] = _event_media_url(event_run_name, clip_path, "clip")

        normalized_events.append(row)

    report["events"] = normalized_events

    return report


def _read_speed_report(
    run_dir: Path,
    cameras: list[dict],
    running: bool = False,
    pid: int | None = None,
    started_at: float | None = None,
    return_code: int | None = None,
) -> dict | None:
    try:
        report = build_speed_dashboard_report(
            run_dir=run_dir,
            cameras=cameras,
            process_alive=running,
            pid=pid,
            started_at=started_at,
            return_code=return_code,
            media_root=settings.MEDIA_ROOT,
        )

        if report is None:
            return None

        report = _normalize_report_media_urls(report)
        report.setdefault("last_error", "")

        if not running and return_code is not None:
            report["last_error"] = _speed_run_error_text(run_dir)

        return report

    except Exception as exc:
        return {
            "running": running,
            "pid": pid,
            "return_code": return_code,
            "started_at": started_at,
            "started_at_text": "-",
            "run_dir": str(run_dir),
            "run_name": run_dir.name,
            "source_count": len(cameras),
            "camera_count": len(cameras),
            "cameras": cameras,
            "events": [],
            "recent_status": [],
            "config": {},
            "last_error": f"Dashboard raporu okunamadı: {exc}",
        }


def _merge_camera_config_fields(report_cameras: list[dict], config_cameras: list[dict]) -> list[dict]:
    """
    build_speed_dashboard_report canlı preview/status alanlarını üretebilir.
    Kalibrasyon bilgisi ise DB/config tarafında olduğu için ikisini camera_id ile birleştiriyoruz.
    """
    config_by_id = {
        str(cam.get("camera_id")): cam
        for cam in config_cameras
        if cam.get("camera_id")
    }

    merged = []

    for row in report_cameras or []:
        camera_id = str(row.get("camera_id") or "")
        base = dict(config_by_id.get(camera_id, {}))
        base.update(dict(row))
        merged.append(base)

    seen = {str(row.get("camera_id") or "") for row in merged}

    for camera_id, cam in config_by_id.items():
        if camera_id not in seen:
            merged.append(dict(cam))

    return merged


def _pipeline_report() -> dict:
    active = speed_runtime.get()

    active_report = None
    active_run_dir = None
    active_return_code = None

    config_cameras = _active_speed_cameras()

    if active is not None:
        proc = active.process
        active_run_dir = Path(active.run_dir)
        active_return_code = proc.poll()
        is_running = active_return_code is None

        active_report = _read_speed_report(
            run_dir=active_run_dir,
            cameras=config_cameras,
            running=is_running,
            pid=proc.pid,
            started_at=active.started_at,
            return_code=active_return_code,
        )

        if not is_running:
            speed_runtime.set(None)

    history_reports = []

    for run_dir in _speed_run_dirs():
        if active_run_dir is not None and run_dir.resolve() == active_run_dir.resolve():
            continue

        rep = _read_speed_report(
            run_dir=run_dir,
            cameras=config_cameras,
            running=False,
            pid=None,
            started_at=None,
            return_code=None,
        )

        if rep:
            history_reports.append(rep)

    if active_report:
        base = active_report
    elif history_reports:
        base = history_reports[0]
    else:
        base = _empty_report()

    merged = dict(base)

    merged["cameras"] = _merge_camera_config_fields(
        report_cameras=merged.get("cameras", []),
        config_cameras=config_cameras,
    )
    merged["camera_count"] = len(merged["cameras"])
    merged["source_count"] = len(merged["cameras"])

    all_events = []

    for rep in ([active_report] if active_report else []) + history_reports:
        if not rep:
            continue

        for event in rep.get("events", []):
            row = dict(event)
            row["run_name"] = row.get("run_name") or rep.get("run_name") or ""
            all_events.append(row)

    seen = set()
    deduped = []

    for event in all_events:
        key = (
            event.get("run_name"),
            event.get("camera_id"),
            event.get("track_id"),
            event.get("frame_idx"),
            event.get("snapshot_path"),
            event.get("clip_path"),
        )

        if key in seen:
            continue

        seen.add(key)
        deduped.append(event)

    deduped.sort(
        key=lambda x: float(x.get("created_at") or 0),
        reverse=True,
    )

    merged["events"] = deduped[:300]

    if active_report is None:
        merged["running"] = False
        merged["pid"] = None

    if active_run_dir is not None and active_return_code is not None:
        merged["last_error"] = _speed_run_error_text(active_run_dir)

    merged = _normalize_report_media_urls(merged)
    merged.setdefault("last_error", "")

    return merged


def _get_speed_config_by_camera_id(camera_id: str) -> SpeedCameraConfig:
    return get_object_or_404(
        SpeedCameraConfig.objects.select_related("camera"),
        camera__camera_id=camera_id,
    )


def _open_camera_source(source: str):
    source = str(source or "").strip()

    if not source:
        return None

    if source.isdigit():
        return cv2.VideoCapture(int(source))

    return cv2.VideoCapture(source)


@never_cache
@login_required
@role_required(["admin"])
@require_GET
def speed_calibration_frame(request, camera_id):
    item = _get_speed_config_by_camera_id(camera_id)
    camera = item.camera

    cap = None

    try:
        cap = _open_camera_source(camera.source)

        if cap is None or not cap.isOpened():
            raise Http404("Kamera görüntüsü açılamadı.")

        ok, frame = cap.read()

        if not ok or frame is None:
            raise Http404("Kameradan kare okunamadı.")

        speed_defaults = getattr(settings, "SPEED_PIPELINE_DEFAULTS", {})
        resize_width = int(speed_defaults.get("resize_width", 960) or 960)

        h, w = frame.shape[:2]

        if resize_width > 0 and w > resize_width:
            scale = resize_width / float(w)
            frame = cv2.resize(frame, (resize_width, int(h * scale)))

        ok, buffer = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 92],
        )

        if not ok:
            raise Http404("Kalibrasyon karesi hazırlanamadı.")

        response = FileResponse(BytesIO(buffer.tobytes()), content_type="image/jpeg")
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        response["X-Calibration-Frame-Width"] = str(frame.shape[1])
        response["X-Calibration-Frame-Height"] = str(frame.shape[0])

        return response

    finally:
        if cap is not None:
            cap.release()


@never_cache
@login_required
@role_required(["admin"])
@require_POST
@csrf_protect
def save_speed_calibration(request, camera_id):
    item = _get_speed_config_by_camera_id(camera_id)
    camera = item.camera

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse(
            {
                "ok": False,
                "message": "Geçersiz JSON verisi.",
            },
            status=400,
        )

    try:
        saved_path = write_two_line_speed_calibration(
            camera=camera,
            speed_config=item,
            roi_polygon=payload.get("roi_polygon") or [],
            line_a=payload.get("line_a") or [],
            line_b=payload.get("line_b") or [],
            distance_m=payload.get("distance_m"),
            direction=payload.get("direction") or "A_TO_B",
        )

        ready, reason = is_speed_calibration_ready(saved_path)

        return JsonResponse(
            {
                "ok": True,
                "message": "Hız kalibrasyonu kaydedildi.",
                "camera_id": camera.camera_id,
                "calibration_path": str(saved_path),
                "calibration_ready": ready,
                "calibration_reason": reason,
            }
        )

    except Exception as exc:
        return JsonResponse(
            {
                "ok": False,
                "message": f"Kalibrasyon kaydedilemedi: {exc}",
            },
            status=400,
        )

def _mjpeg_frame_generator(source: str) -> Iterator[bytes]:
    """
    Üst kamera kartı için ham canlı akış üretir.

    Burada pipeline'ın yazdığı preview jpg dosyasını okumuyoruz.
    Kamera/video kaynağını doğrudan Django tarafından açıp multipart MJPEG olarak
    tarayıcıya gönderiyoruz. Böylece üst kart clip/preview gibi yenilenmez, sürekli akar.
    """
    cap = None

    speed_defaults = getattr(settings, "SPEED_PIPELINE_DEFAULTS", {})
    stream_width = int(speed_defaults.get("stream_width", speed_defaults.get("resize_width", 960)) or 960)
    stream_fps = float(speed_defaults.get("stream_fps", 18.0) or 18.0)
    jpeg_quality = int(speed_defaults.get("stream_jpeg_quality", 82) or 82)

    stream_fps = max(1.0, min(stream_fps, 30.0))
    jpeg_quality = max(45, min(jpeg_quality, 95))
    delay = 1.0 / stream_fps

    try:
        cap = _open_camera_source(source)

        if cap is None or not cap.isOpened():
            return

        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        while True:
            started = time.monotonic()

            ok, frame = cap.read()

            if not ok or frame is None:
                # Kaynak video dosyasıysa başa saralım ki demo akış bitince siyaha düşmesin.
                try:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = cap.read()
                except Exception:
                    ok, frame = False, None

                if not ok or frame is None:
                    time.sleep(0.2)
                    continue

            h, w = frame.shape[:2]

            if stream_width > 0 and w > stream_width:
                scale = stream_width / float(w)
                frame = cv2.resize(frame, (stream_width, int(h * scale)))

            ok, buffer = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
            )

            if not ok:
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-cache\r\n\r\n"
                + buffer.tobytes()
                + b"\r\n"
            )

            elapsed = time.monotonic() - started

            if elapsed < delay:
                time.sleep(delay - elapsed)

    finally:
        if cap is not None:
            cap.release()


@never_cache
@login_required
@require_GET
def speed_camera_stream(request, camera_id):
    item = _get_speed_config_by_camera_id(camera_id)

    response = StreamingHttpResponse(
        _mjpeg_frame_generator(item.camera.source),
        content_type="multipart/x-mixed-replace; boundary=frame",
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    response["X-Accel-Buffering"] = "no"

    return response
def _payload() -> dict:
    report = _pipeline_report()

    return {
        "ok": True,
        "running": report.get("running", False),
        "pid": report.get("pid"),
        "return_code": report.get("return_code"),
        "run_name": report.get("run_name", ""),
        "run_dir": report.get("run_dir", ""),
        "camera_count": report.get("camera_count", 0),
        "cameras": report.get("cameras", []),
        "events": report.get("events", []),
        "recent_status": report.get("recent_status", []),
        "last_error": report.get("last_error", ""),
    }


@never_cache
@login_required
@role_required(["admin"])
@require_POST
def start_speed_detection(request):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    active = speed_runtime.get()

    if active is not None and active.process.poll() is None:
        message = "Hız tespiti sistemi zaten aktif durumda."

        if is_ajax:
            return JsonResponse(
                {
                    "ok": True,
                    "message": message,
                    "running": True,
                    "already_running": True,
                    "pid": active.process.pid,
                    "run_dir": str(active.run_dir),
                    "run_name": active.run_name,
                },
                status=200,
            )

        messages.warning(request, message)
        return redirect("speed_detection:index")

    cameras = _active_speed_cameras()

    if not cameras:
        message = (
            "Hız tespiti başlatılamadı: Hız tespiti aktif olan kamera bulunmuyor. "
            "Kamera ayarlarından kamerayı aktif et ve Hız Tespiti seçeneğini aç."
        )

        if is_ajax:
            return JsonResponse(
                {
                    "ok": False,
                    "message": message,
                    "running": False,
                },
                status=400,
            )

        messages.error(request, message)
        return redirect("speed_detection:index")

    not_ready = [
        cam for cam in cameras
        if not cam.get("calibration_ready")
    ]

    if not_ready:
        names = ", ".join(
            f"{cam.get('name') or cam.get('camera_id')} ({cam.get('calibration_reason')})"
            for cam in not_ready
        )

        message = (
            "Hız tespiti başlatılamadı: Aşağıdaki kameraların kalibrasyonu tamamlanmamış. "
            f"{names}. "
            "Her kamera için görüntü üzerinden ROI, başlangıç çizgisi, bitiş çizgisi ve gerçek mesafe gir."
        )

        if is_ajax:
            return JsonResponse(
                {
                    "ok": False,
                    "message": message,
                    "running": False,
                    "calibration_required": True,
                    "not_ready": not_ready,
                },
                status=400,
            )

        messages.error(request, message)
        return redirect("speed_detection:index")

    try:
        active_run = start_speed_pipeline()

        time.sleep(0.8)

        return_code = active_run.process.poll()

        if return_code is not None:
            speed_runtime.set(None)

            run_dir = Path(active_run.run_dir)
            err_text = _speed_run_error_text(run_dir)
            short_err = err_text[-1600:] if err_text else "Pipeline hemen kapandı fakat stderr.log içinde detay bulunamadı."

            message = (
                "Hız tespiti başlatılamadı. "
                f"Process hemen kapandı. Return code: {return_code}. "
                f"Hata: {short_err}"
            )

            if is_ajax:
                return JsonResponse(
                    {
                        "ok": False,
                        "message": message,
                        "running": False,
                        "return_code": return_code,
                        "run_dir": str(active_run.run_dir),
                        "run_name": active_run.run_name,
                        "last_error": short_err,
                    },
                    status=500,
                )

            messages.error(request, message)
            return redirect("speed_detection:index")

        speed_runtime.set(active_run)

        message = "Hız tespiti sistemi başlatıldı."

        if is_ajax:
            return JsonResponse(
                {
                    "ok": True,
                    "message": message,
                    "running": True,
                    "camera_count": len(active_run.cameras),
                    "pid": active_run.process.pid,
                    "run_dir": str(active_run.run_dir),
                    "run_name": active_run.run_name,
                },
                status=200,
            )

        messages.success(request, message)
        return redirect("speed_detection:index")

    except Exception as exc:
        speed_runtime.set(None)

        message = f"Hız tespiti başlatılamadı: {exc}"

        if is_ajax:
            return JsonResponse(
                {
                    "ok": False,
                    "message": message,
                    "running": False,
                },
                status=500,
            )

        messages.error(request, message)
        return redirect("speed_detection:index")


@never_cache
@login_required
@role_required(["admin"])
@require_POST
def stop_speed_detection(request):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    active = speed_runtime.get()

    if active is None:
        message = "Hız tespiti sistemi zaten durdurulmuş."

        if is_ajax:
            return JsonResponse(
                {
                    "ok": True,
                    "message": message,
                    "running": False,
                    "already_stopped": True,
                },
                status=200,
            )

        messages.warning(request, message)
        return redirect("speed_detection:index")

    try:
        stop_speed_pipeline(active)
        speed_runtime.set(None)

        message = "Hız tespiti sistemi durduruldu."

        if is_ajax:
            return JsonResponse(
                {
                    "ok": True,
                    "message": message,
                    "running": False,
                },
                status=200,
            )

        messages.success(request, message)
        return redirect("speed_detection:index")

    except Exception as exc:
        message = f"Hız tespiti durdurulamadı: {exc}"

        if is_ajax:
            return JsonResponse(
                {
                    "ok": False,
                    "message": message,
                    "running": True,
                },
                status=500,
            )

        messages.error(request, message)
        return redirect("speed_detection:index")


@never_cache
@login_required
@require_GET
def speed_status(request):
    return JsonResponse(_payload())


@never_cache
@login_required
@require_GET
def speed_events(request):
    payload = _payload()

    return JsonResponse(
        {
            "ok": True,
            "running": payload.get("running", False),
            "run_name": payload.get("run_name", ""),
            "events": payload.get("events", []),
        }
    )


def _safe_media_path(path_value: str) -> Path:
    if not path_value:
        raise Http404("Dosya yolu boş.")

    media_root = Path(settings.MEDIA_ROOT).resolve()
    path = Path(path_value)

    if not path.is_absolute():
        path = media_root / path

    resolved = path.resolve()

    try:
        resolved.relative_to(media_root)
    except ValueError:
        raise Http404("Dosyaya erişim izni yok.")

    if not resolved.exists() or not resolved.is_file():
        raise Http404("Dosya bulunamadı.")

    return resolved


def _find_event_file_path(run_name: str, file_name: str, kind: str) -> Path:
    path = _find_existing_event_file(run_name, file_name, kind)

    if path is None:
        raise Http404("Dosya bulunamadı.")

    return path


@never_cache
@login_required
@require_GET
def speed_snapshot(request, run_name, file_name):
    path = _find_event_file_path(run_name, file_name, "snapshot")

    response = FileResponse(open(path, "rb"), content_type="image/jpeg")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response


@never_cache
@login_required
@require_GET
def speed_preview(request, run_name, camera_id):
    camera_id = _safe_url_part(camera_id, "Kamera ID")
    file_name = f"{camera_id}.jpg"

    path = _find_event_file_path(run_name, file_name, "preview")

    response = FileResponse(open(path, "rb"), content_type="image/jpeg")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response


def _file_chunk_iterator(path: Path, start: int, length: int, chunk_size: int = 8192) -> Iterator[bytes]:
    with path.open("rb") as f:
        f.seek(start)
        remaining = length

        while remaining > 0:
            chunk = f.read(min(chunk_size, remaining))

            if not chunk:
                break

            remaining -= len(chunk)
            yield chunk


@never_cache
@login_required
@require_GET
def speed_clip(request, run_name, file_name):
    path = _find_event_file_path(run_name, file_name, "clip")

    content_type, _ = mimetypes.guess_type(str(path))

    if not content_type:
        content_type = "video/mp4"

    file_size = path.stat().st_size
    range_header = request.META.get("HTTP_RANGE", "").strip()

    if range_header:
        match = re.match(r"bytes=(\d*)-(\d*)", range_header)

        if match:
            start_text, end_text = match.groups()

            if start_text == "" and end_text:
                suffix_length = int(end_text)
                start = max(file_size - suffix_length, 0)
                end = file_size - 1
            else:
                start = int(start_text or 0)
                end = int(end_text) if end_text else file_size - 1

            start = max(0, min(start, file_size - 1))
            end = max(start, min(end, file_size - 1))
            length = end - start + 1

            response = StreamingHttpResponse(
                _file_chunk_iterator(path, start, length),
                status=206,
                content_type=content_type,
            )
            response["Content-Length"] = str(length)
            response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            response["Accept-Ranges"] = "bytes"
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"

            return response

    response = FileResponse(open(path, "rb"), content_type=content_type)
    response["Content-Length"] = str(file_size)
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response