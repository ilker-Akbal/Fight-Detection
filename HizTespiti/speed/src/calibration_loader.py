from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json


@dataclass
class LoadedCalibration:
    camera_id: str

    speed_limit_kmh: float
    tolerance_kmh: float

    measurement_mode: str
    direction: str
    line_a: list[list[int]]
    line_b: list[list[int]]
    distance_m: float | None

    road_roi_enabled: bool
    road_roi_polygon: list[list[int]]

    meter_per_pixel: float | None
    scale_confidence: float
    user_corrected: bool

    ready: bool
    ready_reason: str

    raw: dict[str, Any]


def _as_points(value: Any, expected_len: int | None = None) -> list[list[int]]:
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

    if expected_len is not None and len(out) != expected_len:
        return []

    return out


def _as_float_or_none(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None

    try:
        return float(value)
    except Exception:
        return None


def _validate_calibration(
    measurement_mode: str,
    line_a: list[list[int]],
    line_b: list[list[int]],
    distance_m: float | None,
    meter_per_pixel: float | None,
) -> tuple[bool, str]:
    mode = measurement_mode.strip().lower()

    if mode == "two_line_time_gate":
        if len(line_a) != 2:
            return False, "Başlangıç çizgisi eksik veya geçersiz."

        if len(line_b) != 2:
            return False, "Bitiş çizgisi eksik veya geçersiz."

        if distance_m is None or distance_m <= 0:
            return False, "İki çizgi arası gerçek mesafe girilmemiş."

        return True, "ok"

    if mode == "pixel_scale":
        if meter_per_pixel is None or meter_per_pixel <= 0:
            return False, "pixel_scale modu için meter_per_pixel eksik."

        return True, "ok"

    return False, f"Desteklenmeyen kalibrasyon modu: {measurement_mode}"


def load_calibration(path: str | Path) -> LoadedCalibration:
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(
            f"Kalibrasyon dosyası bulunamadı: {path}\n"
            f"Bu kamera için hız tespiti başlatmadan önce dashboard üzerinden kalibrasyon yap."
        )

    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    measurement = raw.get("measurement", {}) or {}
    road_roi = raw.get("road_roi", {}) or {}
    scale = raw.get("scale", {}) or {}

    measurement_mode = str(measurement.get("mode") or "two_line_time_gate")
    direction = str(measurement.get("direction") or "A_TO_B").upper()

    line_a = _as_points(measurement.get("line_a"), expected_len=2)
    line_b = _as_points(measurement.get("line_b"), expected_len=2)
    distance_m = _as_float_or_none(measurement.get("distance_m"))

    roi_polygon = _as_points(road_roi.get("polygon"), expected_len=None)
    roi_enabled = bool(road_roi.get("enabled", False)) and len(roi_polygon) >= 3

    meter_per_pixel = _as_float_or_none(scale.get("meter_per_pixel"))

    ready, ready_reason = _validate_calibration(
        measurement_mode=measurement_mode,
        line_a=line_a,
        line_b=line_b,
        distance_m=distance_m,
        meter_per_pixel=meter_per_pixel,
    )

    return LoadedCalibration(
        camera_id=str(raw.get("camera_id", "cam_001")),

        speed_limit_kmh=float(raw.get("speed_limit_kmh", 30)),
        tolerance_kmh=float(raw.get("tolerance_kmh", 5)),

        measurement_mode=measurement_mode,
        direction=direction,
        line_a=line_a,
        line_b=line_b,
        distance_m=distance_m,

        road_roi_enabled=roi_enabled,
        road_roi_polygon=roi_polygon,

        meter_per_pixel=meter_per_pixel,
        scale_confidence=float(scale.get("confidence", 0.0)),
        user_corrected=bool(scale.get("user_corrected", False)),

        ready=ready,
        ready_reason=ready_reason,

        raw=raw,
    )