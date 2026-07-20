from threading import Lock

from services.pipeline_bridge.fight_runner import start_pipeline, stop_pipeline
from services.pipeline_bridge.report_reader import build_dashboard_report


_active_run = None
_lock = Lock()


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
    """
    Beklenen format:
    [
        {"camera_id": "cam_001", "source": "http://127.0.0.1:5000/stream"},
        {"camera_id": "giris_kapi", "source": "rtsp://..."},
    ]
    """
    result = []
    used_ids = set()

    if not isinstance(raw_sources, list):
        return result

    for idx, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            continue

        camera_id = str(item.get("camera_id", "")).strip()
        source = str(item.get("source", "")).strip()

        if not camera_id or not source:
            continue

        if camera_id in used_ids:
            continue

        used_ids.add(camera_id)

        result.append({
            "camera_id": camera_id,
            "source": source,
        })

    return result


def start_sources(raw_sources):
    global _active_run

    sources = _normalize_sources(raw_sources)

    with _lock:
        if _active_run is not None:
            stop_pipeline(_active_run)
            _active_run = None

        if not sources:
            return _empty_report()

        _active_run = start_pipeline(sources)

        return build_dashboard_report(
            run_dir=_active_run.run_dir,
            sources=_active_run.sources,
            process_alive=_active_run.process.poll() is None,
            pid=_active_run.process.pid,
            started_at=_active_run.started_at,
            return_code=_active_run.process.poll(),
        )

def stop_sources():
    global _active_run

    if _active_run is None:
        return {
            "ok": True,
            "already_stopped": True,
            "message": "Sistem zaten durdurulmuş.",
            "running": False,
        }

    try:
        stop_pipeline(_active_run)
    except Exception as exc:
        if "Çalışan pipeline yok" in str(exc):
            _active_run = None
            return {
                "ok": True,
                "already_stopped": True,
                "message": "Sistem zaten durdurulmuş.",
                "running": False,
            }

        return {
            "ok": False,
            "message": f"Pipeline durdurulurken hata oluştu: {exc}",
            "running": False,
        }

    _active_run = None

    return {
        "ok": True,
        "message": "Kavga tespit sistemi durduruldu.",
        "running": False,
    }

def get_status():
    global _active_run

    with _lock:
        if _active_run is None:
            return _empty_report()

        return build_dashboard_report(
            run_dir=_active_run.run_dir,
            sources=_active_run.sources,
            process_alive=_active_run.process.poll() is None,
            pid=_active_run.process.pid,
            started_at=_active_run.started_at,
            return_code=_active_run.process.poll(),
        )


def get_camera_source(camera_id: str):
    global _active_run

    with _lock:
        if _active_run is None:
            return None

        for item in _active_run.sources:
            if item.get("camera_id") == camera_id:
                return item.get("source")

    return None