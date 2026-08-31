from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReportMessage:
    kind: str
    row: dict[str, Any]


@dataclass
class Stage3Job:
    camera_id: str
    source: str
    event_id: str
    event_start_ts: float
    event_end_ts: float
    pose_score_max: float
    pose_score_mean: float
    clip_path: str
    frames: list
    positive_hits: int = 0
    frame_count: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class Stage3ResultMessage:
    camera_id: str
    source: str
    event_id: str
    event_start_ts: float
    event_end_ts: float
    clip_path: str
    fight_prob: float
    fight_label: str
    pose_score_max: float
    pose_score_mean: float
    processed_at: float = field(default_factory=time.time)


@dataclass
class ActiveEvent:
    event_id: str
    camera_id: str
    source: str
    start_ts: float
    last_ts: float
    last_positive_frame_idx: int
    frames: list = field(default_factory=list)
    pose_scores: list[float] = field(default_factory=list)
    positive_hits: int = 0