from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import time


@dataclass
class MotionEvent:
    camera_id: str
    event_type: str
    frame_idx: int
    timestamp_sec: float
    motion_score: float
    boxes_count: int
    created_at: float


class MotionEventWriter:
    def __init__(self, output_dir: str | Path, camera_id: str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.camera_id = camera_id
        self.path = self.output_dir / f"{camera_id}_motion_events.jsonl"

    def write(
        self,
        event_type: str,
        frame_idx: int,
        timestamp_sec: float,
        motion_score: float,
        boxes_count: int,
    ) -> None:
        event = MotionEvent(
            camera_id=self.camera_id,
            event_type=event_type,
            frame_idx=frame_idx,
            timestamp_sec=timestamp_sec,
            motion_score=motion_score,
            boxes_count=boxes_count,
            created_at=time.time(),
        )

        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")