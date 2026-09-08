from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
CAMERA_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MAX_CAMERAS = 512
MAX_SOURCE_LENGTH = 2048


class InvalidDesiredCameraState(ValueError):
    pass


class StaleDesiredCameraRevision(InvalidDesiredCameraState):
    pass


class DesiredCameraRevisionConflict(InvalidDesiredCameraState):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_camera(camera: dict) -> dict:
    if not isinstance(camera, dict):
        raise InvalidDesiredCameraState("each camera must be an object")
    camera_id = str(camera.get("camera_id") or "").strip()
    if not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise InvalidDesiredCameraState("camera_id contains unsupported characters")
    source = str(camera.get("source") or "").strip()
    if not source or len(source) > MAX_SOURCE_LENGTH:
        raise InvalidDesiredCameraState("camera source is missing or too long")
    enabled = camera.get("enabled", True)
    fight_enabled = camera.get("use_fight_detection", True)
    speed_enabled = camera.get("use_speed_detection", False)
    if not isinstance(enabled, bool) or not isinstance(fight_enabled, bool):
        raise InvalidDesiredCameraState("camera enabled fields must be boolean")
    if not isinstance(speed_enabled, bool):
        raise InvalidDesiredCameraState("camera speed enable field must be boolean")
    speed_config = camera.get("speed_config") or {}
    if not isinstance(speed_config, dict) or len(json.dumps(speed_config)) > 16384:
        raise InvalidDesiredCameraState("invalid speed configuration")
    return {
        "camera_id": camera_id,
        "source": source,
        "name": str(camera.get("name") or camera_id).strip()[:200],
        "enabled": enabled,
        "use_fight_detection": fight_enabled,
        "use_speed_detection": speed_enabled,
        "speed_config": {key: speed_config[key] for key in (
            "speed_limit_kmh", "tolerance_kmh", "calibration_path", "roi_enabled", "roi_polygon",
            "save_snapshot", "save_clip", "calibration_revision") if key in speed_config},
    }


def normalize_cameras(cameras) -> list[dict]:
    if not isinstance(cameras, list):
        raise InvalidDesiredCameraState("cameras must be an array")
    if len(cameras) > MAX_CAMERAS:
        raise InvalidDesiredCameraState("too many cameras")
    normalized = [normalize_camera(camera) for camera in cameras]
    ids = [camera["camera_id"] for camera in normalized]
    if len(ids) != len(set(ids)):
        raise InvalidDesiredCameraState("camera_id values must be unique")
    sources = [item["source"] for item in normalized if item["enabled"] and (
        item["use_fight_detection"] or item["use_speed_detection"])]
    if len(sources) != len(set(sources)):
        raise InvalidDesiredCameraState("enabled cameras must have distinct sources; enable both consumers on one camera")
    return sorted(normalized, key=lambda camera: camera["camera_id"])


def validate_desired_state(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise InvalidDesiredCameraState("desired camera state must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise InvalidDesiredCameraState("unsupported desired camera schema_version")
    revision = payload.get("revision")
    if not isinstance(payload.get("speed_paused", False), bool):
        raise InvalidDesiredCameraState("speed_paused must be boolean")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise InvalidDesiredCameraState("revision must be a non-negative integer")
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "cameras": normalize_cameras(payload.get("cameras")),
        "updated_at": str(payload.get("updated_at") or _utc_now()),
        "speed_paused": payload.get("speed_paused", False),
    }


@dataclass(frozen=True)
class DesiredCameraUpdate:
    state: dict
    changed: bool


class DesiredCameraStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @staticmethod
    def empty() -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "revision": 0,
            "cameras": [],
            "updated_at": "",
        }

    def load(self) -> dict:
        if not self.path.exists():
            return self.empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return validate_desired_state(payload)
        except InvalidDesiredCameraState:
            raise
        except Exception as exc:
            raise InvalidDesiredCameraState("desired camera state is unreadable") from exc

    def _write(self, state: dict) -> None:
        from fight.operations import atomic_json
        atomic_json(self.path, state)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def update(self, payload: dict) -> DesiredCameraUpdate:
        incoming = validate_desired_state(payload)
        current = self.load()
        if incoming["revision"] < current["revision"]:
            raise StaleDesiredCameraRevision("desired camera revision is stale")
        if incoming["revision"] == current["revision"]:
            if incoming["cameras"] != current["cameras"] or incoming["speed_paused"] != current.get("speed_paused", False):
                raise DesiredCameraRevisionConflict(
                    "desired camera revision already has different content"
                )
            return DesiredCameraUpdate(current, False)
        incoming["updated_at"] = _utc_now()
        self._write(incoming)
        return DesiredCameraUpdate(incoming, True)

    def bootstrap(self, cameras: list[dict]) -> dict:
        current = self.load()
        if current["revision"] > 0:
            return current
        normalized = normalize_cameras(cameras)
        update = self.update(
            {
                "schema_version": SCHEMA_VERSION,
                "revision": 1,
                "cameras": normalized,
            }
        )
        return update.state
