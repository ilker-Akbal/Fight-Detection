from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CameraConfig:
    camera_id: str


@dataclass
class InputConfig:
    yolo_tracks_path: str


@dataclass
class OutputConfig:
    output_dir: str
    calibration_path: str


@dataclass
class ScaleEstimationConfig:
    enabled: bool
    min_samples: int
    max_samples: int
    min_conf: float
    min_box_width_px: int
    min_box_height_px: int
    max_box_width_px: int
    max_box_height_px: int
    reference_vehicle_lengths_m: dict[str, float]
    reference_vehicle_widths_m: dict[str, float]
    use_width: bool
    use_height: bool
    trim_ratio: float


@dataclass
class CalibrationDefaults:
    speed_limit_kmh: float
    tolerance_kmh: float
    road_roi: dict[str, Any]
    measurement: dict[str, Any]
    scale: dict[str, Any]


@dataclass
class AppConfig:
    camera: CameraConfig
    input: InputConfig
    output: OutputConfig
    scale_estimation: ScaleEstimationConfig
    calibration_defaults: CalibrationDefaults


def _get(d: dict[str, Any], key: str, default: Any = None) -> Any:
    return d.get(key, default)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Config bulunamadı: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    camera = raw.get("camera", {})
    input_cfg = raw.get("input", {})
    output = raw.get("output", {})
    scale = raw.get("scale_estimation", {})
    defaults = raw.get("calibration_defaults", {})

    return AppConfig(
        camera=CameraConfig(
            camera_id=str(_get(camera, "camera_id", "cam_001")),
        ),
        input=InputConfig(
            yolo_tracks_path=str(_get(input_cfg, "yolo_tracks_path", "")),
        ),
        output=OutputConfig(
            output_dir=str(_get(output, "output_dir", "HizTespiti/calibration/out_calibration")),
            calibration_path=str(
                _get(
                    output,
                    "calibration_path",
                    "HizTespiti/calibration/out_calibration/cam_001_calibration.json",
                )
            ),
        ),
        scale_estimation=ScaleEstimationConfig(
            enabled=bool(_get(scale, "enabled", True)),
            min_samples=int(_get(scale, "min_samples", 20)),
            max_samples=int(_get(scale, "max_samples", 300)),
            min_conf=float(_get(scale, "min_conf", 0.35)),
            min_box_width_px=int(_get(scale, "min_box_width_px", 30)),
            min_box_height_px=int(_get(scale, "min_box_height_px", 25)),
            max_box_width_px=int(_get(scale, "max_box_width_px", 500)),
            max_box_height_px=int(_get(scale, "max_box_height_px", 400)),
            reference_vehicle_lengths_m=dict(
                _get(
                    scale,
                    "reference_vehicle_lengths_m",
                    {
                        "car": 4.5,
                        "truck": 7.0,
                        "bus": 10.5,
                        "motorcycle": 2.1,
                    },
                )
            ),
            reference_vehicle_widths_m=dict(
                _get(
                    scale,
                    "reference_vehicle_widths_m",
                    {
                        "car": 1.8,
                        "truck": 2.5,
                        "bus": 2.55,
                        "motorcycle": 0.8,
                    },
                )
            ),
            use_width=bool(_get(scale, "use_width", True)),
            use_height=bool(_get(scale, "use_height", True)),
            trim_ratio=float(_get(scale, "trim_ratio", 0.15)),
        ),
        calibration_defaults=CalibrationDefaults(
            speed_limit_kmh=float(_get(defaults, "speed_limit_kmh", 30)),
            tolerance_kmh=float(_get(defaults, "tolerance_kmh", 5)),
            road_roi=dict(_get(defaults, "road_roi", {"enabled": False, "polygon": []})),
            measurement=dict(
                _get(
                    defaults,
                    "measurement",
                    {
                        "mode": "pixel_scale",
                        "direction": "AUTO",
                        "line_a": [],
                        "line_b": [],
                        "distance_m": None,
                    },
                )
            ),
            scale=dict(
                _get(
                    defaults,
                    "scale",
                    {
                        "source": "auto_vehicle_size",
                        "meter_per_pixel": None,
                        "confidence": 0.0,
                        "user_corrected": False,
                    },
                )
            ),
        ),
    )