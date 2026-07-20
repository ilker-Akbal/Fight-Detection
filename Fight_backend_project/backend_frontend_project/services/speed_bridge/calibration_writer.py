from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from django.conf import settings

from HizTespiti.calibration.src.camera_calibration import (
    CameraCalibration,
    MeasurementConfig,
    RoadRoi,
    ScaleConfig,
)
from HizTespiti.calibration.src.calibration_store import CalibrationStore


def _safe_name(value: str) -> str:
    value = str(value or "").strip()

    if not value:
        return "camera"

    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)

    return value[:120] or "camera"


def _repo_root() -> Path:
    return Path(getattr(settings, "REPO_ROOT", Path(settings.BASE_DIR).parent.parent)).resolve()


def _media_root() -> Path:
    return Path(settings.MEDIA_ROOT).resolve()


def _to_path_text(value: str | Path | None) -> str:
    return str(value or "").strip().replace("\\", "/")


def speed_calibration_dir() -> Path:
    """
    Yeni kalibrasyonların tek merkezi.
    Django içindeki yanlış/ikincil HizTespiti klasörüne yazmayı engellemek için
    varsayılan dosyaları MEDIA_ROOT altında mutlak path olarak tutuyoruz.
    """
    root = _media_root() / "speed_calibrations"
    root.mkdir(parents=True, exist_ok=True)
    return root


def default_speed_calibration_path(camera_id: str) -> Path:
    return speed_calibration_dir() / f"{_safe_name(camera_id)}_calibration.json"


def resolve_speed_calibration_path(
    path_value: str | Path | None,
    camera_id: str | None = None,
) -> Path:
    """
    Kalibrasyon yolu hem Django hem pipeline tarafından aynı dosyaya çözülsün.

    Sorunun ana nedeni şuydu:
    - Django göreli `HizTespiti/...` yolunu backend çalışma dizinine göre yazabiliyor.
    - Pipeline ise aynı göreli yolu repo köküne göre okuyordu.

    Bu yüzden `HizTespiti/...` ile başlayan göreli yolları daima REPO_ROOT altında çözüyoruz.
    Boş path gelirse MEDIA_ROOT/speed_calibrations altında mutlak varsayılan dosya kullanıyoruz.
    """
    text = _to_path_text(path_value)

    if not text:
        return default_speed_calibration_path(camera_id or "camera")

    path = Path(text)

    if path.is_absolute():
        return path.resolve()

    parts = path.parts

    if parts and parts[0] == "HizTespiti":
        return (_repo_root() / path).resolve()

    if parts and parts[0] in {"speed_calibrations", "speed_runs"}:
        return (_media_root() / path).resolve()

    media_candidate = (_media_root() / path).resolve()
    if media_candidate.exists():
        return media_candidate

    return (_repo_root() / path).resolve()


def _normalize_points(value: Any, expected_len: int | None = None) -> list[list[int]]:
    if not isinstance(value, list):
        return []

    out: list[list[int]] = []

    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue

        try:
            x = int(round(float(point[0])))
            y = int(round(float(point[1])))
        except Exception:
            continue

        out.append([x, y])

    if expected_len is not None and len(out) != expected_len:
        return []

    return out


def _normalize_distance(value: Any) -> float:
    try:
        distance = float(value)
    except Exception:
        raise ValueError("Gerçek mesafe sayısal olmalı.")

    if distance <= 0:
        raise ValueError("Gerçek mesafe 0'dan büyük olmalı.")

    if distance > 1000:
        raise ValueError("Gerçek mesafe çok büyük görünüyor. Metre cinsinden girildiğinden emin ol.")

    return distance


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists() and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass

    return {}


def is_speed_calibration_ready(path: str | Path) -> tuple[bool, str]:
    path = resolve_speed_calibration_path(path)

    if not path.exists():
        return False, "Kalibrasyon dosyası yok."

    raw = _read_json(path)

    measurement = raw.get("measurement", {}) or {}

    mode = str(measurement.get("mode") or "").lower()

    if mode != "two_line_time_gate":
        return False, "Kalibrasyon modu two_line_time_gate değil."

    line_a = _normalize_points(measurement.get("line_a"), expected_len=2)
    line_b = _normalize_points(measurement.get("line_b"), expected_len=2)

    try:
        distance_m = _normalize_distance(measurement.get("distance_m"))
    except Exception:
        distance_m = None

    if len(line_a) != 2:
        return False, "Başlangıç çizgisi seçilmemiş."

    if len(line_b) != 2:
        return False, "Bitiş çizgisi seçilmemiş."

    if distance_m is None:
        return False, "Gerçek mesafe girilmemiş."

    return True, "ok"


def sync_speed_calibration_file(camera, speed_config) -> Path:
    """
    Admin kamera kaydında path yoksa güvenli varsayılan path üretir.
    Path varsa da Django ve pipeline aynı dosyayı kullansın diye mutlak/çözülmüş path'e çevirir.
    Kullanıcı kalibrasyon yapmadan sahte ölçüm dosyası üretmez.
    """
    path = resolve_speed_calibration_path(
        getattr(speed_config, "calibration_path", "") or "",
        camera_id=getattr(camera, "camera_id", None),
    )

    path.parent.mkdir(parents=True, exist_ok=True)

    resolved_text = str(path)
    if str(getattr(speed_config, "calibration_path", "") or "").strip() != resolved_text:
        speed_config.calibration_path = resolved_text
        speed_config.save(update_fields=["calibration_path"])

    return path


def write_two_line_speed_calibration(
    *,
    camera,
    speed_config,
    roi_polygon: Any,
    line_a: Any,
    line_b: Any,
    distance_m: Any,
    direction: str = "A_TO_B",
) -> Path:
    path = sync_speed_calibration_file(camera, speed_config)

    roi = _normalize_points(roi_polygon, expected_len=None)
    line_a_points = _normalize_points(line_a, expected_len=2)
    line_b_points = _normalize_points(line_b, expected_len=2)
    distance = _normalize_distance(distance_m)

    if len(line_a_points) != 2:
        raise ValueError("Başlangıç çizgisi için 2 nokta seçilmeli.")

    if len(line_b_points) != 2:
        raise ValueError("Bitiş çizgisi için 2 nokta seçilmeli.")

    direction = str(direction or "A_TO_B").upper()

    if direction not in {"A_TO_B", "B_TO_A", "AUTO"}:
        direction = "A_TO_B"

    now = time.time()

    existing = _read_json(path)
    created_at = float(existing.get("created_at") or now)

    calibration = CameraCalibration(
        camera_id=str(camera.camera_id),
        speed_limit_kmh=float(speed_config.speed_limit_kmh),
        tolerance_kmh=float(speed_config.tolerance_kmh),
        road_roi=RoadRoi(
            enabled=len(roi) >= 3,
            polygon=roi,
        ),
        measurement=MeasurementConfig(
            mode="two_line_time_gate",
            direction=direction,
            line_a=line_a_points,
            line_b=line_b_points,
            distance_m=distance,
        ),
        scale=ScaleConfig(
            source="not_required_two_line_time_gate",
            meter_per_pixel=None,
            confidence=1.0,
            user_corrected=True,
        ),
        created_at=created_at,
        updated_at=now,
        meta={
            "created_by": "dashboard",
            "camera_name": str(getattr(camera, "name", "")),
            "source": str(getattr(camera, "source", "")),
            "frame_coordinate_space": "pipeline_resize_width",
            "resize_width": int(
                getattr(settings, "SPEED_PIPELINE_DEFAULTS", {}).get(
                    "resize_width",
                    960,
                )
            ),
        },
    )

    saved_path = CalibrationStore(path).save(calibration)

    speed_config.calibration_path = str(saved_path)
    speed_config.roi_enabled = len(roi) >= 3
    speed_config.roi_polygon = roi
    speed_config.save(
        update_fields=[
            "calibration_path",
            "roi_enabled",
            "roi_polygon",
        ]
    )

    return saved_path