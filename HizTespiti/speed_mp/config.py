from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SpeedMpCamera:
    camera_id: str
    name: str
    source: str
    description: str
    faculty: str

    speed_limit_kmh: float
    tolerance_kmh: float

    calibration_path: str

    roi_enabled: bool
    roi_polygon: list[list[int]]

    save_snapshot: bool
    save_clip: bool


@dataclass
class SpeedMpConfig:
    run_name: str
    output_dir: str
    base_config: str
    yolo_weights: str

    runtime: dict[str, Any]
    motion: dict[str, Any]
    yolo: dict[str, Any]
    tracker: dict[str, Any]
    speed: dict[str, Any]
    evidence: dict[str, Any]

    cameras: list[SpeedMpCamera]


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value

    if value is None:
        return default

    s = str(value).strip().lower()

    if s in {"1", "true", "yes", "on", "evet", "aktif"}:
        return True

    if s in {"0", "false", "no", "off", "hayır", "pasif"}:
        return False

    return default


def _as_polygon(value: Any) -> list[list[int]]:
    if not value:
        return []

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []

    if not isinstance(value, list):
        return []

    out: list[list[int]] = []

    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue

        try:
            x = int(float(point[0]))
            y = int(float(point[1]))
        except Exception:
            continue

        out.append([x, y])

    return out


def load_mp_config(path: str | Path) -> SpeedMpConfig:
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Speed multiprocess config bulunamadı: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))

    cameras_raw = raw.get("cameras", []) or []
    cameras: list[SpeedMpCamera] = []

    for cam in cameras_raw:
        camera_id = str(cam.get("camera_id") or "").strip()
        source = str(cam.get("source") or "").strip()

        if not camera_id or not source:
            continue

        cameras.append(
            SpeedMpCamera(
                camera_id=camera_id,
                name=str(cam.get("name") or camera_id),
                source=source,
                description=str(cam.get("description") or ""),
                faculty=str(cam.get("faculty") or ""),

                speed_limit_kmh=_as_float(cam.get("speed_limit_kmh"), 50.0),
                tolerance_kmh=_as_float(cam.get("tolerance_kmh"), 10.0),

                calibration_path=str(cam.get("calibration_path") or "").strip(),

                roi_enabled=_as_bool(cam.get("roi_enabled"), False),
                roi_polygon=_as_polygon(cam.get("roi_polygon")),

                save_snapshot=_as_bool(cam.get("save_snapshot"), True),
                save_clip=_as_bool(cam.get("save_clip"), True),
            )
        )

    if not cameras:
        raise RuntimeError("Speed config içinde çalıştırılacak kamera yok.")

    return SpeedMpConfig(
        run_name=str(raw.get("run_name") or path.parent.name),
        output_dir=str(raw.get("output_dir") or path.parent),
        base_config=str(raw.get("base_config") or "HizTespiti/speed/configs/speed.yaml"),
        yolo_weights=str(raw.get("yolo_weights") or "yolo11s.pt"),

        runtime=dict(raw.get("runtime") or {}),
        motion=dict(raw.get("motion") or {}),
        yolo=dict(raw.get("yolo") or {}),
        tracker=dict(raw.get("tracker") or {}),
        speed=dict(raw.get("speed") or {}),
        evidence=dict(raw.get("evidence") or {}),

        cameras=cameras,
    )