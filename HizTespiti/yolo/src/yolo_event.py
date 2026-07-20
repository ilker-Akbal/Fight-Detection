from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import time

from .simple_tracker import Track


@dataclass
class YoloTrackEvent:
    camera_id: str
    frame_idx: int
    timestamp_sec: float
    track_id: int
    vehicle_class: str
    conf: float
    box_xyxy: list[int]
    center_xy: list[float]
    track_age: int
    created_at: float


class YoloEventWriter:
    def __init__(self, output_dir: str | Path, camera_id: str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.camera_id = camera_id
        self.path = self.output_dir / f"{camera_id}_yolo_tracks.jsonl"

    def write_track(self, frame_idx: int, timestamp_sec: float, track: Track) -> None:
        x1, y1, x2, y2 = track.box

        if track.history:
            _, cx, cy = track.history[-1]
        else:
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0

        event = YoloTrackEvent(
            camera_id=self.camera_id,
            frame_idx=frame_idx,
            timestamp_sec=timestamp_sec,
            track_id=track.track_id,
            vehicle_class=track.cls_name,
            conf=float(track.conf),
            box_xyxy=[int(x1), int(y1), int(x2), int(y2)],
            center_xy=[float(cx), float(cy)],
            track_age=int(track.age),
            created_at=time.time(),
        )

        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")