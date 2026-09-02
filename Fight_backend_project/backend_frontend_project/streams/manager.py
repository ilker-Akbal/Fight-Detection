from django.conf import settings

from services.pipeline_bridge.fight_runner import (
    get_active_run,
    get_pipeline_status,
    start_pipeline,
    stop_pipeline,
)
from services.pipeline_bridge.report_reader import build_dashboard_report


def _empty_report():
    return {
        "running": False,
        "pid": None,
        "return_code": None,
        "started_at": None,
        "run_dir": "",
        "source_count": 0,
        "camera_count": 0,
        "sources": [],
        "cameras": [],
        "recent_events": [],
        "recent_stage3": [],
    }


def _normalize_sources(raw_sources):
    result = []
    used_ids = set()
    if not isinstance(raw_sources, list):
        return result
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        camera_id = str(item.get("camera_id", "")).strip()
        source = str(item.get("source", "")).strip()
        if not camera_id or not source or camera_id in used_ids:
            continue
        used_ids.add(camera_id)
        result.append({"camera_id": camera_id, "source": source})
    return result


def _active_report(active_run, control_status):
    return build_dashboard_report(
        run_dir=active_run.run_dir,
        sources=active_run.sources,
        process_alive=active_run.runtime_state
        in {"STARTING", "RUNNING", "STOPPING", "BACKOFF"},
        pid=active_run.runtime_pid,
        started_at=active_run.started_at,
        return_code=control_status.get("runtime_exit_code"),
        media_root=settings.MEDIA_ROOT,
    )


def start_sources(raw_sources):
    sources = _normalize_sources(raw_sources)
    if not sources:
        return _empty_report()
    active_run = start_pipeline(sources)
    return _active_report(active_run, get_pipeline_status())


def stop_sources():
    control = get_pipeline_status()
    if not control.get("available"):
        return {
            "ok": False,
            "message": "Runtime supervisor ulaşılamıyor; pipeline durumu bilinmiyor.",
            "running": None,
            "runtime_state": "UNKNOWN",
            "error": "supervisor_unavailable",
        }
    try:
        result = stop_pipeline(get_active_run())
    except Exception as exc:
        return {
            "ok": False,
            "message": f"Pipeline durdurulurken hata oluştu: {exc}",
            "running": None,
        }
    already_stopped = result.get("result") == "already_stopped"
    return {
        "ok": True,
        "already_stopped": already_stopped,
        "message": (
            "Sistem zaten durdurulmuş."
            if already_stopped
            else "Kavga tespit sistemi durduruldu."
        ),
        "running": False,
    }


def get_status():
    control = get_pipeline_status()
    if not control.get("available"):
        report = _empty_report()
        report.update(
            {
                "running": None,
                "runtime_state": "UNKNOWN",
                "error": "supervisor_unavailable",
            }
        )
        return report
    active_run = get_active_run()
    if active_run is None:
        return _empty_report()
    return _active_report(active_run, control)


def get_camera_source(camera_id: str):
    active_run = get_active_run()
    if active_run is None:
        return None
    for item in active_run.sources:
        if item.get("camera_id") == camera_id:
            return item.get("source")
    return None
