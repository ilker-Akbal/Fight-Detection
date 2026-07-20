from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CameraConfig:
    camera_id: str
    source: str


@dataclass
class RuntimeConfig:
    show: bool
    save_debug_video: bool
    output_dir: str
    resize_width: int
    max_fps: float


@dataclass
class CalibrationConfig:
    path: str


@dataclass
class MotionConfig:
    enabled: bool
    history: int
    var_threshold: float
    detect_shadows: bool
    blur_kernel: int
    threshold: int
    morph_open_kernel: int
    morph_close_kernel: int
    dilate_iterations: int
    min_area: int
    max_area_ratio: float
    min_motion_score: float
    open_frames: int
    close_frames: int
    ignore_first_frames: int


@dataclass
class RoiConfig:
    enabled: bool
    polygon: list[list[int]]


@dataclass
class YoloConfig:
    weights: str
    device: str
    imgsz: int
    conf: float
    iou: float
    stride: int
    vehicle_classes: list[str]


@dataclass
class TrackerConfig:
    iou_threshold: float
    max_age: int
    min_hits: int


@dataclass
class SpeedConfig:
    min_track_points: int
    min_time_delta_sec: float
    max_time_delta_sec: float
    smooth_window: int
    min_valid_speed_kmh: float
    max_valid_speed_kmh: float
    confirm_frames: int
    cooldown_sec: float


@dataclass
class EvidenceConfig:
    save_snapshot: bool
    save_clip: bool
    clip_pre_sec: float
    clip_post_sec: float
    jpeg_quality: int


@dataclass
class AppConfig:
    camera: CameraConfig
    runtime: RuntimeConfig
    calibration: CalibrationConfig
    motion: MotionConfig
    roi: RoiConfig
    yolo: YoloConfig
    tracker: TrackerConfig
    speed: SpeedConfig
    evidence: EvidenceConfig


def _get(d: dict[str, Any], key: str, default: Any = None) -> Any:
    return d.get(key, default)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Config bulunamadı: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    camera = raw.get("camera", {})
    runtime = raw.get("runtime", {})
    calibration = raw.get("calibration", {})
    motion = raw.get("motion", {})
    roi = raw.get("roi", {})
    yolo = raw.get("yolo", {})
    tracker = raw.get("tracker", {})
    speed = raw.get("speed", {})
    evidence = raw.get("evidence", {})

    return AppConfig(
        camera=CameraConfig(
            camera_id=str(_get(camera, "camera_id", "cam_001")),
            source=str(_get(camera, "source", "0")),
        ),
        runtime=RuntimeConfig(
            show=bool(_get(runtime, "show", True)),
            save_debug_video=bool(_get(runtime, "save_debug_video", True)),
            output_dir=str(_get(runtime, "output_dir", "HizTespiti/speed/out_speed")),
            resize_width=int(_get(runtime, "resize_width", 960)),
            max_fps=float(_get(runtime, "max_fps", 0)),
        ),
        calibration=CalibrationConfig(
            path=str(_get(calibration, "path", "HizTespiti/calibration/out_calibration/cam_001_calibration.json")),
        ),
        motion=MotionConfig(
            enabled=bool(_get(motion, "enabled", True)),
            history=int(_get(motion, "history", 500)),
            var_threshold=float(_get(motion, "var_threshold", 32)),
            detect_shadows=bool(_get(motion, "detect_shadows", True)),
            blur_kernel=int(_get(motion, "blur_kernel", 5)),
            threshold=int(_get(motion, "threshold", 35)),
            morph_open_kernel=int(_get(motion, "morph_open_kernel", 3)),
            morph_close_kernel=int(_get(motion, "morph_close_kernel", 9)),
            dilate_iterations=int(_get(motion, "dilate_iterations", 2)),
            min_area=int(_get(motion, "min_area", 900)),
            max_area_ratio=float(_get(motion, "max_area_ratio", 0.55)),
            min_motion_score=float(_get(motion, "min_motion_score", 0.004)),
            open_frames=int(_get(motion, "open_frames", 3)),
            close_frames=int(_get(motion, "close_frames", 8)),
            ignore_first_frames=int(_get(motion, "ignore_first_frames", 20)),
        ),
        roi=RoiConfig(
            enabled=bool(_get(roi, "enabled", False)),
            polygon=list(_get(roi, "polygon", [])),
        ),
        yolo=YoloConfig(
            weights=str(_get(yolo, "weights", "yolo11s.pt")),
            device=str(_get(yolo, "device", "0")),
            imgsz=int(_get(yolo, "imgsz", 640)),
            conf=float(_get(yolo, "conf", 0.30)),
            iou=float(_get(yolo, "iou", 0.50)),
            stride=max(1, int(_get(yolo, "stride", 3))),
            vehicle_classes=list(_get(yolo, "vehicle_classes", ["car", "motorcycle", "bus", "truck"])),
        ),
        tracker=TrackerConfig(
            iou_threshold=float(_get(tracker, "iou_threshold", 0.25)),
            max_age=int(_get(tracker, "max_age", 20)),
            min_hits=int(_get(tracker, "min_hits", 2)),
        ),
        speed=SpeedConfig(
            min_track_points=int(_get(speed, "min_track_points", 6)),
            min_time_delta_sec=float(_get(speed, "min_time_delta_sec", 0.25)),
            max_time_delta_sec=float(_get(speed, "max_time_delta_sec", 3.0)),
            smooth_window=int(_get(speed, "smooth_window", 6)),
            min_valid_speed_kmh=float(_get(speed, "min_valid_speed_kmh", 3)),
            max_valid_speed_kmh=float(_get(speed, "max_valid_speed_kmh", 140)),
            confirm_frames=int(_get(speed, "confirm_frames", 2)),
            cooldown_sec=float(_get(speed, "cooldown_sec", 8)),
        ),
        evidence=EvidenceConfig(
            save_snapshot=bool(_get(evidence, "save_snapshot", True)),
            save_clip=bool(_get(evidence, "save_clip", True)),
            clip_pre_sec=float(_get(evidence, "clip_pre_sec", 3)),
            clip_post_sec=float(_get(evidence, "clip_post_sec", 3)),
            jpeg_quality=int(_get(evidence, "jpeg_quality", 92)),
        ),
    )