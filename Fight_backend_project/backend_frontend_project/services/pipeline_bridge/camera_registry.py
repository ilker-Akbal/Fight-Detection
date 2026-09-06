from __future__ import annotations

from django.conf import settings

from fight.runtime_supervisor.camera_state import SCHEMA_VERSION, normalize_cameras
from fight.runtime_supervisor.client import RuntimeSupervisorClient
from streams.models import Camera


def desired_camera_snapshot() -> list[dict]:
    cameras = Camera.objects.filter(
        is_active=True,
        use_fight_detection=True,
    ).order_by("camera_id")
    return normalize_cameras(
        [
            {
                "camera_id": camera.camera_id,
                "source": camera.get_runtime_source(),
                "name": camera.name,
                "enabled": True,
                "use_fight_detection": True,
            }
            for camera in cameras
            if camera.get_runtime_source()
        ]
    )


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
            }
        )
        return {
            "changed": bool(result.get("accepted", True)),
            "revision": int(result.get("revision", revision)),
            "camera_count": len(desired),
        }
