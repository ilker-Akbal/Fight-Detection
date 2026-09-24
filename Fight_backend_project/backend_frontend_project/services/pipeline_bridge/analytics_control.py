"""Durable analytics intent; only Supervisor may own the common runtime."""
import logging

from fight.runtime_supervisor.camera_state import DuplicateCameraSources, InvalidDesiredCameraState
from fight.runtime_supervisor.client import SupervisorRequestError, SupervisorUnavailable

from .camera_registry import desired_camera_snapshot, supervisor_client
from .fight_runner import get_pipeline_status, start_pipeline


logger = logging.getLogger(__name__)


class AnalyticsControlError(RuntimeError):
    def __init__(self, code, message, *, stage, status=409, camera_groups=None, supervisor_status=None):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "error": code, "message": message, "stage": stage,
                        "camera_groups": camera_groups or [], "supervisor_http_status": supervisor_status}


def set_analytics(paused):
    stages = ["camera_registry"]
    try:
        return _set_analytics(paused, stages.append)
    except DuplicateCameraSources as exc:
        error = AnalyticsControlError("duplicate_camera_sources",
            "Aynı kaynak birden fazla etkin kameraya atanmış. Her kaynak için tek kamera etkin bırakın.",
            stage="camera_registry", camera_groups=exc.camera_groups)
    except InvalidDesiredCameraState:
        error = AnalyticsControlError("invalid_camera_registry", "Kamera yapılandırması geçersiz.", stage=stages[-1])
    except SupervisorRequestError as exc:
        denied = exc.http_status in {401, 403}
        error = AnalyticsControlError("supervisor_unauthorized" if denied else "supervisor_rejected",
            "Uygulama ile Supervisor erişim anahtarı eşleşmiyor." if denied else
            "Supervisor komutu reddetti. Yapılandırmayı ve çalışma zamanı durumunu kontrol edin.",
            stage=stages[-1], supervisor_status=exc.http_status)
    except SupervisorUnavailable:
        error = AnalyticsControlError("supervisor_unavailable", "Supervisor hizmetine erişilemiyor.",
                                      stage=stages[-1], status=503)
    except AnalyticsControlError as exc:
        error = exc
    except (OSError, ValueError, RuntimeError):
        error = AnalyticsControlError("runtime_control_failed", "Çalışma zamanı komutu tamamlanamadı.",
                                      stage=stages[-1], status=503)
    # Only stable reason/stage/HTTP status; no exception text, URL or bearer token.
    logger.warning("analytics_control_rejected action=%s code=%s stage=%s supervisor_http_status=%s",
                   "preview_only" if paused else "analytics_active", error.payload["error"],
                   error.payload["stage"], error.payload["supervisor_http_status"])
    raise error


def _set_analytics(paused, mark_stage):
    from .offline_analysis import set_monitoring_hold
    cameras = desired_camera_snapshot()
    mark_stage("supervisor_status")
    status = get_pipeline_status()
    state = status.get("runtime_state", "UNKNOWN")
    if status.get("orphan_detected") or state in {"UNKNOWN", "STOPPING"}:
        raise AnalyticsControlError("runtime_not_ready", "Çalışma zamanı sahipliği veya durması henüz doğrulanmadı.",
                                    stage="supervisor_status")
    client = supervisor_client()
    mark_stage("desired_read")
    current = client.desired_cameras()
    if paused:
        cameras = [{**camera, "use_fight_detection": False, "use_speed_detection": False}
                   for camera in cameras]
    mark_stage("desired_update")
    result = client.update_desired_cameras({
        "schema_version": 1, "revision": int(current.get("revision", 0)) + 1,
        "cameras": cameras, "analytics_paused": bool(paused), "speed_paused": bool(paused),
    })
    set_monitoring_hold(False)
    if state in {"STOPPED", "FAILED"}:
        mark_stage("runtime_start")
        active = start_pipeline(cameras)
        if active.runtime_state not in {"STARTING", "RUNNING"}:
            raise AnalyticsControlError("runtime_start_not_accepted", "Supervisor çalışma zamanını başlatmadı.",
                                        stage="runtime_start")
    return result
