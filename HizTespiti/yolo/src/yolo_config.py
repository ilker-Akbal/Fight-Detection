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
class EventConfig:
    write_jsonl: bool
    min_track_age: int


@dataclass
class AppConfig:
    camera: CameraConfig
    runtime: RuntimeConfig
    motion: MotionConfig
    roi: RoiConfig
    yolo: YoloConfig
    tracker: TrackerConfig
    events: EventConfig


def _get(data: dict[str, Any], key: str, default: Any = None) -> Any:
    return data.get(key, default)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Config bulunamadı: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    camera = raw.get("camera", {})
    runtime = raw.get("runtime", {})
    motion = raw.get("motion", {})
    roi = raw.get("roi", {})
    yolo = raw.get("yolo", {})
    tracker = raw.get("tracker", {})
    events = raw.get("events", {})

    return AppConfig(
        camera=CameraConfig(
            camera_id=str(_get(camera, "camera_id", "cam_001")),
            source=str(_get(camera, "source", "0")),
        ),
        runtime=RuntimeConfig(
            show=bool(_get(runtime, "show", True)),
            save_debug_video=bool(_get(runtime, "save_debug_video", False)),
            output_dir=str(_get(runtime, "output_dir", "HizTespiti/yolo/out_yolo")),
            resize_width=int(_get(runtime, "resize_width", 960)),
            max_fps=float(_get(runtime, "max_fps", 0)),
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
        events=EventConfig(
            write_jsonl=bool(_get(events, "write_jsonl", True)),
            min_track_age=int(_get(events, "min_track_age", 3)),
        ),
    )