from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReportMessage:
    kind: str
    row: dict[str, Any]


@dataclass
class HealthEvent:
    component: str
    component_type: str
    event_type: str
    monotonic_ts: float = field(default_factory=time.monotonic)
    camera_id: str = ""
    slot_id: int = -1
    generation: int = -1
    progress: int = 0
    secondary_progress: int = 0
    queue_depth: int = -1
    dropped: int = 0
    reconnect_count: int = 0
    detail: str = ""
    consumer_epoch: int = 0
    service_epoch: int = 0


@dataclass
class CameraFrame:
    camera_id: str
    generation: int
    frame_seq: int
    captured_monotonic: float
    captured_wall_time: float
    frame: Any
    source_fps: float = 0.0
    source_width: int = 0
    source_height: int = 0
    source_frame_count: int = 0


@dataclass
class CameraIngestSignal:
    camera_id: str
    generation: int
    detail: str
    frame_seq: int = 0
    captured_monotonic: float = field(default_factory=time.perf_counter)
    captured_wall_time: float = field(default_factory=time.time)
    error: str | None = None


@dataclass
class PersonInferenceRequest:
    camera_id: str
    generation: int
    frame_idx: int
    request_id: int
    frame: Any
    created_at: float = field(default_factory=time.time)
    created_monotonic: float = 0.0
    put_started_monotonic: float = 0.0
    payload_width: int = 0
    payload_height: int = 0
    payload_channels: int = 0
    payload_bytes: int = 0
    slot_id: int = -1
    source_is_file: bool = True
    max_age_sec: float = 0.0


@dataclass
class PersonInferenceResult:
    camera_id: str
    generation: int
    frame_idx: int
    request_id: int
    detections: list[tuple[float, tuple[int, int, int, int]]] = field(default_factory=list)
    requested_at: float = 0.0
    started_at: float = 0.0
    processed_at: float = field(default_factory=time.time)
    inference_ms: float = 0.0
    error: str | None = None
    worker_received_monotonic: float = 0.0
    inference_started_monotonic: float = 0.0
    inference_ended_monotonic: float = 0.0
    result_put_started_monotonic: float = 0.0
    worker_request_index: int = 0
    slot_id: int = -1
    outcome: str = "accepted"


@dataclass
class PoseInferenceRequest:
    camera_id: str
    generation: int
    frame_idx: int
    request_id: int
    roi: Any
    created_at: float = field(default_factory=time.time)
    created_monotonic: float = 0.0
    put_started_monotonic: float = 0.0
    payload_width: int = 0
    payload_height: int = 0
    payload_channels: int = 0
    payload_bytes: int = 0
    slot_id: int = -1
    source_is_file: bool = True
    max_age_sec: float = 0.0


@dataclass
class PoseInferenceResult:
    camera_id: str
    generation: int
    frame_idx: int
    request_id: int
    pose_result: Any = None
    requested_at: float = 0.0
    started_at: float = 0.0
    processed_at: float = field(default_factory=time.time)
    inference_ms: float = 0.0
    error: str | None = None
    worker_received_monotonic: float = 0.0
    inference_started_monotonic: float = 0.0
    inference_ended_monotonic: float = 0.0
    result_put_started_monotonic: float = 0.0
    worker_request_index: int = 0
    slot_id: int = -1
    outcome: str = "accepted"


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
    created_monotonic: float = field(default_factory=time.perf_counter)
    generation: int = 0
    slot_id: int = -1


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
    generation: int = 0
    slot_id: int = -1
    service_epoch: int = 0


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
