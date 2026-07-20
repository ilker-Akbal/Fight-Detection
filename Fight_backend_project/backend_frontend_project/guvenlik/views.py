from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
from pathlib import Path

import cv2
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.timezone import now
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import role_required
from accounts.models import UserProfile
from services.pipeline_bridge.fight_runner import start_pipeline, stop_pipeline
from services.pipeline_bridge.report_reader import build_dashboard_report
from services.pipeline_bridge.runtime_state import runtime
from streams.models import Camera
MAX_HISTORY_RUNS = 10
MAX_HISTORY_STAGE3 = 200
MAX_HISTORY_INCIDENTS = 200

SYSTEM_NOTICE_CACHE_KEY = "dashboard_system_notice"
SYSTEM_NOTICE_TTL_SECONDS = 30


def _set_system_notice(kind: str, title: str, message: str):
    notice = {
        "id": f"{int(time.time() * 1000)}",
        "kind": kind,
        "title": title,
        "message": message,
        "created_at": now().isoformat(),
    }

    cache.set(SYSTEM_NOTICE_CACHE_KEY, notice, SYSTEM_NOTICE_TTL_SECONDS)


def _get_system_notice():
    return cache.get(SYSTEM_NOTICE_CACHE_KEY)


def _get_user_profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def _is_admin_user(user) -> bool:
    if not user or not user.is_authenticated:
        return False

    if user.is_superuser or user.is_staff:
        return True

    profile = _get_user_profile(user)
    return profile.role == "admin"


def _camera_queryset_for_user(user, active_only=False):
    qs = Camera.objects.all()

    if active_only:
        qs = qs.filter(is_active=True, use_fight_detection=True)

    if _is_admin_user(user):
        return qs.order_by("-created_at")

    profile = _get_user_profile(user)

    if not profile.faculty:
        return qs.none()

    return qs.filter(faculty=profile.faculty).order_by("-created_at")


def _allowed_camera_ids_for_user(user) -> set[str]:
    return set(
        _camera_queryset_for_user(user, active_only=True)
        .values_list("camera_id", flat=True)
    )


def _user_can_access_camera_id(user, camera_id: str) -> bool:
    if _is_admin_user(user):
        return True

    if not camera_id:
        return False

    return camera_id in _allowed_camera_ids_for_user(user)


def _filter_rows_by_camera(rows, allowed_camera_ids):
    return [
        row for row in rows
        if row.get("camera_id") in allowed_camera_ids
    ]


def _filter_report_for_user(report: dict, user) -> dict:
    if not report:
        return report

    if _is_admin_user(user):
        return report

    allowed_camera_ids = _allowed_camera_ids_for_user(user)
    filtered = dict(report)

    filtered["sources"] = [
        item for item in report.get("sources", [])
        if item.get("camera_id") in allowed_camera_ids
    ]

    filtered["cameras"] = [
        item for item in report.get("cameras", [])
        if item.get("camera_id") in allowed_camera_ids
    ]

    filtered["recent_stage3"] = _filter_rows_by_camera(
        report.get("recent_stage3", []),
        allowed_camera_ids,
    )

    filtered["recent_incidents"] = _filter_rows_by_camera(
        report.get("recent_incidents", []),
        allowed_camera_ids,
    )

    filtered["recent_events"] = _filter_rows_by_camera(
        report.get("recent_events", []),
        allowed_camera_ids,
    )

    filtered["recent_status"] = _filter_rows_by_camera(
        report.get("recent_status", []),
        allowed_camera_ids,
    )

    filtered["camera_count"] = len(allowed_camera_ids)
    filtered["source_count"] = len(allowed_camera_ids)

    return filtered


def _serialize_camera(camera):
    return {
        "id": camera.id,
        "name": camera.name,
        "camera_id": camera.camera_id,
        "source": camera.source,
        "description": camera.description,
        "faculty": camera.faculty,
        "faculty_label": camera.get_faculty_display() if camera.faculty else "-",
        "is_active": camera.is_active,
    }


def _active_cameras_qs():
    return (
        Camera.objects
        .filter(is_active=True, use_fight_detection=True)
        .order_by("-created_at")
    )


def _active_sources():
    cameras = _active_cameras_qs()

    return [
        {
            "camera_id": cam.camera_id,
            "source": cam.source,
            "name": cam.name,
            "description": cam.description,
        }
        for cam in cameras
    ]


def _pipeline_runs_root() -> Path:
    return Path(settings.MEDIA_ROOT) / "pipeline_runs"


def _run_dirs(limit: int = MAX_HISTORY_RUNS) -> list[Path]:
    root = _pipeline_runs_root()

    if not root.exists() or not root.is_dir():
        return []

    dirs = [p for p in root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    return dirs[:limit]


def _safe_clip_name(value: str) -> str:
    """
    Incident clip URL'sinde tam Windows/Linux path değil, sadece güvenli dosya adı kullanılmalı.
    Örnek:
      C:\...\incidents\cam002_incident_000001.mp4 -> cam002_incident_000001.mp4
      /home/.../incidents/cam002_incident_000001.mp4 -> cam002_incident_000001.mp4
    """
    s = str(value or "").strip().replace("\\", "/")
    name = Path(s).name

    if not name or name in {".", ".."}:
        return ""

    return name


def _format_ts(value):
    if value in (None, "", "-"):
        return "-"

    s = str(value).strip().replace("T", " ")

    if "." in s:
        s = s.split(".", 1)[0]

    return s


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def _normalize_report(report: dict) -> dict:
    if not report:
        return report

    for cam in report.get("cameras", []):
        cam["last_ts"] = _format_ts(cam.get("last_ts"))

    for row in report.get("recent_stage3", []):
        row["event_start"] = _format_ts(row.get("event_start"))
        row["event_end"] = _format_ts(row.get("event_end"))

    for row in report.get("recent_incidents", []):
        row["start_ts"] = _format_ts(row.get("start_ts"))
        row["end_ts"] = _format_ts(row.get("end_ts"))
        row["clip_name"] = _safe_clip_name(row.get("clip_name") or row.get("clip_path"))

    return report


def _empty_report():
    return {
        "running": False,
        "pid": None,
        "return_code": None,
        "started_at": None,
        "run_dir": "",
        "run_name": "",
        "source_count": 0,
        "camera_count": 0,
        "sources": _active_sources(),
        "cameras": [],
        "recent_events": [],
        "recent_stage3": [],
        "recent_incidents": [],
        "recent_status": [],
    }


def _read_run_report(run_dir: Path, running=False, pid=None, started_at=None, return_code=None):
    try:
        report = build_dashboard_report(
            run_dir=str(run_dir),
            sources=_active_sources(),
            process_alive=running,
            pid=pid,
            started_at=started_at,
            return_code=return_code,
            media_root=settings.MEDIA_ROOT,
        )

        report["running"] = running

        return _normalize_report(report)

    except Exception:
        return None


def _stage3_sort_key(row: dict):
    return (
        str(row.get("event_end") or ""),
        str(row.get("event_start") or ""),
        str(row.get("event_id") or ""),
    )


def _incident_sort_key(row: dict):
    return (
        str(row.get("end_ts") or ""),
        str(row.get("start_ts") or ""),
        str(row.get("incident_id") or ""),
    )


def _dedupe_stage3(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []

    for row in rows:
        key = (
            row.get("run_name"),
            row.get("camera_id"),
            row.get("event_id"),
            row.get("fight_prob"),
            row.get("fight_label"),
        )

        if key in seen:
            continue

        seen.add(key)
        out.append(row)

    return out


def _dedupe_incidents(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []

    for row in rows:
        key = (
            row.get("run_name"),
            row.get("camera_id"),
            row.get("incident_id"),
            row.get("clip_path"),
        )

        if key in seen:
            continue

        seen.add(key)
        out.append(row)

    return out


def _merge_history_reports(active_report: dict | None, history_reports: list[dict]) -> dict:
    base = active_report or (history_reports[0] if history_reports else _empty_report())

    merged = dict(base)
    merged["recent_events"] = []
    merged["recent_stage3"] = []
    merged["recent_incidents"] = []

    all_stage3 = []
    all_incidents = []

    for rep in ([active_report] if active_report else []) + history_reports:
        if not rep:
            continue

        run_name = rep.get("run_name") or ""

        for st in rep.get("recent_stage3", []):
            row = dict(st)
            row["run_name"] = run_name
            all_stage3.append(row)

        for inc in rep.get("recent_incidents", []):
            row = dict(inc)
            row["run_name"] = run_name
            all_incidents.append(row)

    all_stage3 = _dedupe_stage3(all_stage3)
    all_incidents = _dedupe_incidents(all_incidents)

    all_stage3.sort(key=_stage3_sort_key, reverse=True)
    all_incidents.sort(key=_incident_sort_key, reverse=True)

    merged["recent_stage3"] = all_stage3[:MAX_HISTORY_STAGE3]
    merged["recent_incidents"] = all_incidents[:MAX_HISTORY_INCIDENTS]

    return merged


def _pipeline_report():
    active = runtime.get()

    active_report = None
    active_run_dir = None

    if active is not None:
        proc = active.process
        active_run_dir = Path(active.run_dir)

        active_report = _read_run_report(
            active_run_dir,
            running=(proc.poll() is None),
            pid=proc.pid,
            started_at=active.started_at,
            return_code=proc.poll(),
        )

    history_reports = []

    for run_dir in _run_dirs():
        if active_run_dir is not None and run_dir.resolve() == active_run_dir.resolve():
            continue

        rep = _read_run_report(
            run_dir,
            running=False,
            pid=None,
            started_at=None,
            return_code=None,
        )

        if rep:
            history_reports.append(rep)

    merged = _merge_history_reports(active_report, history_reports)

    if active_report is None:
        merged["running"] = False
        merged["pid"] = None

    return merged


def _merge_camera_cards(cameras, pipeline_report):
    pipeline_map = {
        item.get("camera_id"): item
        for item in pipeline_report.get("cameras", [])
        if item.get("camera_id")
    }

    merged = []

    for camera in cameras:
        cam_pipe = pipeline_map.get(camera.camera_id, {})

        merged.append(
            {
                "id": camera.id,
                "name": camera.name,
                "camera_id": camera.camera_id,
                "source": camera.source,
                "description": camera.description,
                "faculty": camera.faculty,
                "faculty_label": camera.get_faculty_display() if camera.faculty else "-",
                "is_active": camera.is_active,
                "stage": cam_pipe.get("stage", "-"),
                "detail": cam_pipe.get("detail", "-"),
                "last_ts": _format_ts(cam_pipe.get("last_ts", "-")),
                "persons": _safe_int(cam_pipe.get("persons", 0)),
                "pair_ok": _safe_int(cam_pipe.get("pair_ok", 0)),
                "pose_score": _safe_float(cam_pipe.get("pose_score", 0.0)),
                "latest_event_status": cam_pipe.get("latest_event_status", "-"),
                "latest_stage3_label": cam_pipe.get("latest_stage3_label", "-"),
                "latest_stage3_prob": _safe_float(cam_pipe.get("latest_stage3_prob", 0.0)),
                "latest_incident_label": cam_pipe.get("latest_incident_label", "-"),
                "latest_incident_part_count": cam_pipe.get("latest_incident_part_count", "-"),
                "queue_status": cam_pipe.get("queue_status", "-"),
                "queue_reason": cam_pipe.get("queue_reason", "-"),
            }
        )

    return merged


def _report_payload(request):
    report = _pipeline_report()
    report = _filter_report_for_user(report, request.user)

    return {
        "ok": True,
        "running": report["running"],
        "events": [],
        "stage3": report["recent_stage3"],
        "incidents": report.get("recent_incidents", []),
        "cameras": report["cameras"],
        "run_name": report.get("run_name", ""),
        "pid": report.get("pid"),
        "camera_count": report.get("camera_count", 0),
        "notice": _get_system_notice(),
        "server_time": now().isoformat(),
    }


def _report_signature(payload: dict) -> str:
    compact = {
        "running": payload.get("running"),
        "run_name": payload.get("run_name"),
        "pid": payload.get("pid"),
        "camera_count": payload.get("camera_count"),
        "notice_id": (payload.get("notice") or {}).get("id"),
        "stage3": [
            (
                x.get("run_name"),
                x.get("camera_id"),
                x.get("event_id"),
                x.get("event_end"),
                x.get("fight_prob"),
                x.get("fight_label"),
            )
            for x in payload.get("stage3", [])
        ],
        "incidents": [
            (
                x.get("run_name"),
                x.get("camera_id"),
                x.get("incident_id"),
                x.get("end_ts"),
                x.get("part_count"),
                x.get("final_label"),
                x.get("clip_path"),
                x.get("clip_name"),
            )
            for x in payload.get("incidents", [])
        ],
        "cameras": [
            (
                x.get("camera_id"),
                x.get("stage"),
                x.get("detail"),
                x.get("last_ts"),
                x.get("persons"),
                x.get("pair_ok"),
                x.get("pose_score"),
                x.get("latest_event_status"),
                x.get("latest_stage3_label"),
                x.get("latest_stage3_prob"),
                x.get("latest_incident_label"),
                x.get("latest_incident_part_count"),
                x.get("queue_status"),
                x.get("queue_reason"),
            )
            for x in payload.get("cameras", [])
        ],
    }

    raw = json.dumps(compact, ensure_ascii=False, sort_keys=True).encode("utf-8")

    return hashlib.md5(raw).hexdigest()


@never_cache
@login_required
def index(request):
    cameras = list(_camera_queryset_for_user(request.user, active_only=True))
    pipeline = _pipeline_report()
    pipeline = _filter_report_for_user(pipeline, request.user)
    camera_cards = _merge_camera_cards(cameras, pipeline)

    return render(
        request,
        "dashboard/index.html",
        {
            "cameras": cameras,
            "camera_cards": camera_cards,
            "pipeline": pipeline,
        },
    )


@never_cache
@login_required
@require_GET
def status(request):
    _make_session_readonly(request)

    cameras = _camera_queryset_for_user(request.user, active_only=True)
    report = _pipeline_report()
    report = _filter_report_for_user(report, request.user)

    data = {
        "count": cameras.count(),
        "cameras": [_serialize_camera(camera) for camera in cameras],
        "pipeline": report,
    }

    return JsonResponse(data)


@never_cache
@login_required
@role_required(["admin"])
@require_POST
def start_detection(request):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    active = runtime.get()

    if active is not None and active.process.poll() is None:
        message = "Kavga tespit sistemi zaten aktif durumda."

        _set_system_notice(
            kind="info",
            title="Sistem Aktif",
            message=message,
        )

        if is_ajax:
            return JsonResponse(
                {
                    "ok": True,
                    "message": message,
                    "running": True,
                    "already_running": True,
                    "pid": active.process.pid if getattr(active, "process", None) else None,
                    "run_dir": str(active.run_dir) if getattr(active, "run_dir", None) else "",
                },
                status=200,
            )

        messages.warning(request, message)

        return redirect("adminx:dashboard")

    sources = _active_sources()

    if not sources:
        message = "Aktif kamera bulunamadı."

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

        return redirect("adminx:dashboard")

    try:
        active_run = start_pipeline(sources)
        runtime.set(active_run)

        message = "Kavga tespit sistemi başlatıldı. Aktif kameralar izleniyor."

        _set_system_notice(
            kind="success",
            title="Sistem Başlatıldı",
            message=message,
        )

        if is_ajax:
            return JsonResponse(
                {
                    "ok": True,
                    "message": message,
                    "running": True,
                    "camera_count": len(sources),
                    "sources": sources,
                    "pid": active_run.process.pid if getattr(active_run, "process", None) else None,
                    "run_dir": str(active_run.run_dir) if getattr(active_run, "run_dir", None) else "",
                },
                status=200,
            )

        messages.success(request, message)

        return redirect("adminx:dashboard")

    except Exception as exc:
        message = f"Pipeline başlatılamadı: {exc}"

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

        return redirect("adminx:dashboard")


@never_cache
@login_required
@role_required(["admin"])
@require_POST
def stop_detection(request):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    active = runtime.get()

    if active is None:
        message = "Sistem zaten durdurulmuş."

        _set_system_notice(
            kind="warning",
            title="Sistem Zaten Durdurulmuş",
            message=message,
        )

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

        return redirect("adminx:dashboard")

    try:
        stop_pipeline(active)
        runtime.set(None)

        message = "Kavga tespit sistemi durduruldu. Eski kayıtlar korunuyor."

        _set_system_notice(
            kind="warning",
            title="Sistem Durduruldu",
            message=message,
        )

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

        return redirect("adminx:dashboard")

    except Exception as exc:
        message = f"Pipeline durdurulamadı: {exc}"

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

        return redirect("adminx:dashboard")


@never_cache
@login_required
@require_GET
def events(request):
    _make_session_readonly(request)

    return JsonResponse(_report_payload(request))


@never_cache
@login_required
@require_GET
def events_stream(request):
    _make_session_readonly(request)

    def event_generator():
        last_sig = None

        while True:
            try:
                payload = _report_payload(request)
                sig = _report_signature(payload)

                if sig != last_sig:
                    last_sig = sig
                    yield "event: dashboard\n"
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                else:
                    yield "event: ping\n"
                    yield f"data: {json.dumps({'ts': time.time()})}\n\n"

                time.sleep(1.0)

            except GeneratorExit:
                break

            except Exception as exc:
                err = {"ok": False, "error": str(exc)}
                yield "event: error\n"
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
                time.sleep(2.0)

    response = StreamingHttpResponse(
        event_generator(),
        content_type="text/event-stream",
    )

    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"

    return response


def _is_file_source(source: str) -> bool:
    s = str(source).strip()

    if not s:
        return False

    if s.isdigit():
        return False

    low = s.lower()

    if low.startswith(("rtsp://", "rtmp://", "http://", "https://", "udp://", "tcp://")):
        return False

    return Path(s).exists()


def _open_capture(source: str):
    if source is None:
        return None

    source = str(source).strip()

    if source.isdigit():
        cap = cv2.VideoCapture(int(source), cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        return cap

    cap = cv2.VideoCapture(source)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    return cap


def _mjpeg_generator(source: str):
    cap = _open_capture(source)

    if cap is None or not cap.isOpened():
        return

    is_file = _is_file_source(source)

    try:
        while True:
            ok, frame = cap.read()

            if not ok or frame is None:
                if is_file:
                    break

                time.sleep(0.05)
                continue

            ok, buffer = cv2.imencode(".jpg", frame)

            if not ok:
                if is_file:
                    break

                time.sleep(0.05)
                continue

            frame_bytes = buffer.tobytes()

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame_bytes
                + b"\r\n"
            )

            time.sleep(0.03)

    finally:
        cap.release()


@never_cache
@login_required
@require_GET
def stream(request, camera_id):
    _make_session_readonly(request)

    camera = get_object_or_404(
        _camera_queryset_for_user(request.user, active_only=True),
        camera_id=camera_id,
    )

    cap = _open_capture(camera.source)

    if cap is None or not cap.isOpened():
        if cap is not None:
            cap.release()

        raise Http404("Kamera akışı açılamadı")

    cap.release()

    return StreamingHttpResponse(
        _mjpeg_generator(camera.source),
        content_type="multipart/x-mixed-replace; boundary=frame",
    )


def _find_preview_path(camera_id: str) -> Path | None:
    for run_dir in _run_dirs(limit=20):
        preview_path = run_dir / "previews" / f"{camera_id}.jpg"

        if preview_path.exists() and preview_path.is_file():
            return preview_path

    return None


@never_cache
@login_required
@require_GET
def preview_image(request, camera_id):
    _make_session_readonly(request)

    get_object_or_404(
        _camera_queryset_for_user(request.user, active_only=True),
        camera_id=camera_id,
    )

    preview_path = _find_preview_path(camera_id)

    if preview_path is None:
        raise Http404("Preview bulunamadı")

    response = FileResponse(open(preview_path, "rb"), content_type="image/jpeg")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response


def _can_access_incident_clip(user, run_name, clip_name) -> bool:
    safe_clip_name = _safe_clip_name(clip_name)

    if not safe_clip_name:
        return False

    if _is_admin_user(user):
        return True

    allowed_camera_ids = _allowed_camera_ids_for_user(user)
    report = _pipeline_report()

    for row in report.get("recent_incidents", []):
        row_run_name = str(row.get("run_name") or "")
        row_clip_name = _safe_clip_name(row.get("clip_name") or row.get("clip_path"))
        row_camera_id = row.get("camera_id")

        if (
            row_run_name == run_name
            and row_camera_id in allowed_camera_ids
            and row_clip_name == safe_clip_name
        ):
            return True

    return False


def _range_file_response(request, file_path: Path, content_type: str):
    """
    HTML5 video player için Range request desteği.
    Chrome/Edge/Safari video preload ve seek sırasında Range ister.
    """
    file_size = file_path.stat().st_size
    range_header = request.headers.get("Range", "").strip()

    if not range_header:
        response = FileResponse(open(file_path, "rb"), content_type=content_type)
        response["Content-Length"] = str(file_size)
        response["Accept-Ranges"] = "bytes"
        response["Cache-Control"] = "no-cache"
        return response

    match = re.match(r"bytes=(\d+)-(\d*)", range_header)

    if not match:
        response = FileResponse(open(file_path, "rb"), content_type=content_type)
        response["Content-Length"] = str(file_size)
        response["Accept-Ranges"] = "bytes"
        response["Cache-Control"] = "no-cache"
        return response

    start = int(match.group(1))
    end_raw = match.group(2)
    end = int(end_raw) if end_raw else file_size - 1

    if start >= file_size:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response

    end = min(end, file_size - 1)
    length = end - start + 1

    with open(file_path, "rb") as f:
        f.seek(start)
        data = f.read(length)

    response = HttpResponse(data, status=206, content_type=content_type)
    response["Content-Length"] = str(length)
    response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "no-cache"
    return response


@never_cache
@login_required
@require_GET
def incident_video(request, run_name, clip_name):
    _make_session_readonly(request)

    safe_clip_name = _safe_clip_name(clip_name)

    if not safe_clip_name:
        raise Http404("Incident clip bulunamadı")

    if not _can_access_incident_clip(request.user, run_name, safe_clip_name):
        raise Http404("Incident clip bulunamadı")

    clip_path = (
        Path(settings.MEDIA_ROOT)
        / "pipeline_runs"
        / str(run_name)
        / "incidents"
        / safe_clip_name
    )

    if not clip_path.exists() or not clip_path.is_file():
        raise Http404("Incident clip bulunamadı")

    content_type, _ = mimetypes.guess_type(str(clip_path))

    if not content_type:
        content_type = "video/mp4"

    return _range_file_response(request, clip_path, content_type)

def _make_session_readonly(request):
    try:
        request.session.modified = False
    except Exception:
        pass