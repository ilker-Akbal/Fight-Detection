from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
import time


@dataclass
class CameraStatusMessage:
    type: str
    camera_id: str
    ts: float
    stage: str
    detail: str
    frame_idx: int = 0
    fps: float = 0.0
    motion_active: bool | None = None
    yolo_ran: bool | None = None
    detections: int = 0
    tracks: int = 0
    latest_speed_kmh: float | None = None
    latest_violation: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SpeedEventMessage:
    type: str
    event: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "event": self.event,
        }


@dataclass
class ReporterStopMessage:
    type: str = "stop"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type}


def status_message(
    camera_id: str,
    stage: str,
    detail: str,
    frame_idx: int = 0,
    fps: float = 0.0,
    motion_active: bool | None = None,
    yolo_ran: bool | None = None,
    detections: int = 0,
    tracks: int = 0,
    latest_speed_kmh: float | None = None,
    latest_violation: bool = False,
    error: str = "",
) -> dict[str, Any]:
    return CameraStatusMessage(
        type="status",
        camera_id=str(camera_id),
        ts=time.time(),
        stage=str(stage),
        detail=str(detail),
        frame_idx=int(frame_idx),
        fps=float(fps or 0.0),
        motion_active=motion_active,
        yolo_ran=yolo_ran,
        detections=int(detections or 0),
        tracks=int(tracks or 0),
        latest_speed_kmh=latest_speed_kmh,
        latest_violation=bool(latest_violation),
        error=str(error or ""),
    ).to_dict()


def speed_event_message(event: dict[str, Any]) -> dict[str, Any]:
    return SpeedEventMessage(type="speed_event", event=event).to_dict()


def stop_message() -> dict[str, Any]:
    return ReporterStopMessage().to_dict()