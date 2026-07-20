from __future__ import annotations

import queue
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


VALID_PIPELINES = frozenset({"fight", "speed"})
VALID_STAGES = frozenset({"person_detection", "pose", "vehicle_detection"})


@dataclass(slots=True)
class InferenceJob:
    pipeline: str
    stage: str
    camera_id: str
    session_id: str
    generation_id: str
    frame_id: int
    timestamp_ns: int
    request_id: str
    image: Any
    created_ns: int = field(default_factory=time.monotonic_ns)

    @classmethod
    def create(cls, *, pipeline: str, stage: str, camera_id: str,
               session_id: str, generation_id: str, frame_id: int,
               timestamp_ns: int, image: Any) -> "InferenceJob":
        return cls(pipeline, stage, str(camera_id), session_id, generation_id,
                   int(frame_id), int(timestamp_ns), uuid.uuid4().hex, image)

    def valid(self) -> bool:
        return (
            self.pipeline in VALID_PIPELINES and self.stage in VALID_STAGES
            and bool(self.camera_id and self.session_id and self.generation_id and self.request_id)
            and self.frame_id >= 0 and self.timestamp_ns > 0
        )


@dataclass(slots=True)
class InferenceResult:
    pipeline: str
    stage: str
    camera_id: str
    session_id: str
    generation_id: str
    frame_id: int
    timestamp_ns: int
    request_id: str
    success: bool
    payload: Any = None
    error_code: str = ""
    queue_wait_ms: float = 0.0
    inference_ms: float = 0.0

    @classmethod
    def from_job(cls, job: InferenceJob, *, success: bool, payload: Any = None,
                 error_code: str = "", queue_wait_ms: float = 0.0,
                 inference_ms: float = 0.0) -> "InferenceResult":
        return cls(job.pipeline, job.stage, job.camera_id, job.session_id,
                   job.generation_id, job.frame_id, job.timestamp_ns,
                   job.request_id, success, payload, error_code,
                   queue_wait_ms, inference_ms)

    def valid(self) -> bool:
        return (
            self.pipeline in VALID_PIPELINES and self.stage in VALID_STAGES
            and bool(self.camera_id and self.session_id and self.generation_id and self.request_id)
            and self.frame_id >= 0 and self.timestamp_ns > 0
        )


@dataclass(slots=True)
class RouteCommand:
    action: str
    pipeline: str
    camera_id: str
    session_id: str
    generation_id: str
    result_channel: Any = None


class InferenceClient:
    """Synchronous camera-side facade with bounded pending and strict validation."""

    def __init__(self, *, pipeline: str, camera_id: str, session_id: str,
                 generation_id: str, result_channel, job_queues: dict[str, Any],
                 timeout_sec: float = 2.0, max_inflight: int = 2):
        self.pipeline = pipeline
        self.camera_id = str(camera_id)
        self.session_id = session_id
        self.generation_id = generation_id
        self.result_channel = result_channel
        self.job_queues = job_queues
        self.timeout_sec = max(0.05, float(timeout_sec))
        self.max_inflight = max(1, int(max_inflight))
        self.pending: dict[str, tuple[str, int, int, float]] = {}
        self.last_accepted_frame: dict[str, int] = {}
        self.metrics = {k: 0 for k in (
            "submitted", "completed", "dropped", "timeouts", "stale_results",
            "session_mismatch", "unknown_request",
        )}
        self.total_inference_ms = 0.0
        self.total_queue_wait_ms = 0.0
        self.last_success_ns = 0

    def infer(self, stage: str, frame_id: int, timestamp_ns: int, image: Any):
        self.expire()
        stage_pending = sum(1 for v in self.pending.values() if v[0] == stage)
        if stage_pending >= self.max_inflight:
            self.metrics["dropped"] += 1
            return None
        job = InferenceJob.create(
            pipeline=self.pipeline, stage=stage, camera_id=self.camera_id,
            session_id=self.session_id, generation_id=self.generation_id,
            frame_id=frame_id, timestamp_ns=timestamp_ns, image=image,
        )
        try:
            self.job_queues[stage].put(job, block=False)
        except queue.Full:
            self.metrics["dropped"] += 1
            return None
        self.pending[job.request_id] = (stage, frame_id, timestamp_ns, time.monotonic())
        self.metrics["submitted"] += 1
        deadline = time.monotonic() + self.timeout_sec
        while time.monotonic() < deadline:
            try:
                result = self.result_channel.get(timeout=min(0.05, max(0.001, deadline-time.monotonic())))
            except queue.Empty:
                continue
            accepted = self._accept(result, expected_stage=stage)
            if accepted is not None:
                return accepted
        self.pending.pop(job.request_id, None)
        self.metrics["timeouts"] += 1
        return None

    def _accept(self, result: Any, *, expected_stage: str):
        if not isinstance(result, InferenceResult) or not result.valid():
            self.metrics["unknown_request"] += 1
            return None
        if (result.pipeline != self.pipeline or result.camera_id != self.camera_id
                or result.session_id != self.session_id
                or result.generation_id != self.generation_id):
            self.metrics["session_mismatch"] += 1
            return None
        pending = self.pending.get(result.request_id)
        if pending is None or result.stage != expected_stage or pending[0] != result.stage:
            self.metrics["unknown_request"] += 1
            return None
        if result.frame_id != pending[1] or result.timestamp_ns != pending[2]:
            self.pending.pop(result.request_id, None)
            self.metrics["stale_results"] += 1
            return None
        if result.frame_id <= self.last_accepted_frame.get(result.stage, -1):
            self.pending.pop(result.request_id, None)
            self.metrics["stale_results"] += 1
            return None
        self.pending.pop(result.request_id, None)
        self.last_accepted_frame[result.stage] = result.frame_id
        if not result.success:
            return None
        self.metrics["completed"] += 1
        self.total_inference_ms += result.inference_ms
        self.total_queue_wait_ms += result.queue_wait_ms
        self.last_success_ns = time.time_ns()
        return result.payload

    def expire(self) -> None:
        cutoff = time.monotonic() - self.timeout_sec
        expired = [rid for rid, value in self.pending.items() if value[3] < cutoff]
        for rid in expired:
            self.pending.pop(rid, None)
        self.metrics["timeouts"] += len(expired)

    def close(self) -> None:
        self.pending.clear()

    def status(self) -> dict[str, Any]:
        completed = max(1, self.metrics["completed"])
        return {
            **self.metrics, "pending": len(self.pending),
            "avg_inference_ms": round(self.total_inference_ms / completed, 3),
            "avg_queue_wait_ms": round(self.total_queue_wait_ms / completed, 3),
            "last_success_ns": self.last_success_ns,
        }
