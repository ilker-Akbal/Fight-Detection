from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any
import time


@dataclass
class RoadRoi:
    enabled: bool = False
    polygon: list[list[int]] = field(default_factory=list)


@dataclass
class MeasurementConfig:
    # Ürün akışında ana mod artık budur.
    # Kullanıcı iki çizgi seçer ve gerçek mesafeyi metre olarak girer.
    mode: str = "two_line_time_gate"

    # A_TO_B: line_a başlangıç, line_b bitiş
    # B_TO_A: line_b başlangıç, line_a bitiş
    # AUTO: ilk geçilen çizgi başlangıç kabul edilir
    direction: str = "A_TO_B"

    line_a: list[list[int]] = field(default_factory=list)
    line_b: list[list[int]] = field(default_factory=list)
    distance_m: float | None = None


@dataclass
class ScaleConfig:
    # Eski pixel_scale modu için geriye dönük destek.
    # Yeni iki çizgili modda meter_per_pixel gerekmez.
    source: str = "not_required"
    meter_per_pixel: float | None = None
    confidence: float = 1.0
    user_corrected: bool = True


@dataclass
class CameraCalibration:
    camera_id: str
    speed_limit_kmh: float = 30.0
    tolerance_kmh: float = 5.0
    road_roi: RoadRoi = field(default_factory=RoadRoi)
    measurement: MeasurementConfig = field(default_factory=MeasurementConfig)
    scale: ScaleConfig = field(default_factory=ScaleConfig)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)