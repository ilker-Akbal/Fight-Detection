from __future__ import annotations

from django.conf import settings
from django.db.models import Q

from fight.runtime_supervisor.camera_state import SCHEMA_VERSION, normalize_cameras
from fight.runtime_supervisor.client import RuntimeSupervisorClient
from streams.models import Camera


def desired_camera_snapshot() -> list[dict]:
    cameras = Camera.objects.filter(is_active=True).filter(
        Q(use_fight_detection=True) | Q(use_speed_detection=True, speed_config__enabled=True)
    ).select_related("speed_config").order_by("camera_id")
    items = []
    for camera in cameras:
        speed = getattr(camera, "speed_config", None)
        speed_enabled = bool(camera.use_speed_detection and speed and speed.enabled)
        speed_config = {}
        if speed_enabled:
            from services.speed_bridge.calibration_writer import resolve_speed_calibration_path
            path = resolve_speed_calibration_path(speed.calibration_path, camera_id=camera.camera_id)
            try:
                revision = f"{path.stat().st_mtime_ns}:{path.stat().st_size}"
            except OSError:
                revision = "missing"
            speed_config = {key: getattr(speed, key) for key in (
                "speed_limit_kmh", "tolerance_kmh", "roi_enabled", "roi_polygon", "save_snapshot", "save_clip")}
            speed_config.update(calibration_path=str(path), calibration_revision=revision)
        if camera.get_runtime_source():
            items.append({
                "camera_id": camera.camera_id,
                "source": camera.get_runtime_source(),
                "name": camera.name,
                "enabled": True,
                "use_fight_detection": camera.use_fight_detection,
                "use_speed_detection": speed_enabled,
                "speed_config": speed_config,
            })
    return normalize_cameras(items)


def supervisor_client() -> RuntimeSupervisorClient:
    return RuntimeSupervisorClient(
        settings.RUNTIME_SUPERVISOR_URL,
        settings.RUNTIME_SUPERVISOR_TOKEN,
        timeout=settings.RUNTIME_SUPERVISOR_TIMEOUT_SEC,
    )


class CameraRegistryReconciler:
    def __init__(self, client=None):
        self.client = client or supervisor_client()

    def tick(self) -> dict:
        desired = desired_camera_snapshot()
        current = self.client.desired_cameras()
        paused = bool(current.get("speed_paused", False))
        if paused:
            for camera in desired:
                camera["use_speed_detection"] = False
        current_cameras = normalize_cameras(current.get("cameras") or [])
        if current_cameras == desired:
            return {
                "changed": False,
                "revision": int(current.get("revision", 0)),
                "camera_count": len(desired),
            }
        revision = int(current.get("revision", 0)) + 1
        result = self.client.update_desired_cameras(
            {
                "schema_version": SCHEMA_VERSION,
                "revision": revision,
                "cameras": desired,
                "speed_paused": paused,
            }
        )
        return {
            "changed": bool(result.get("accepted", True)),
            "revision": int(result.get("revision", revision)),
            "camera_count": len(desired),
        }
