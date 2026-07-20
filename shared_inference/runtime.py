from __future__ import annotations

import logging
import queue
import time
from typing import Any, Callable

from .protocol import InferenceJob, InferenceResult, RouteCommand

LOG = logging.getLogger(__name__)


def result_router_main(result_queue, command_queue, stop_event, result_channels=None) -> None:
    routes: dict[tuple[str, str], tuple[str, str, Any]] = {}
    result_channels = result_channels or {}

    def drain_commands() -> bool:
        """Apply lifecycle changes before routing any already queued result."""
        while True:
            try:
                cmd = command_queue.get_nowait()
            except queue.Empty:
                return True
            if cmd is None:
                return False
            if not isinstance(cmd, RouteCommand):
                LOG.warning("invalid router command dropped")
                continue
            key = (cmd.pipeline, cmd.camera_id)
            if cmd.action == "register":
                channel = result_channels.get(key)
                if channel is not None:
                    routes[key] = (cmd.session_id, cmd.generation_id, channel)
            elif cmd.action == "unregister":
                current = routes.get(key)
                if current and current[:2] == (cmd.session_id, cmd.generation_id):
                    routes.pop(key, None)

    while not stop_event.is_set():
        if not drain_commands():
            return
        try:
            result = result_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if result is None:
            return
        if not drain_commands():
            return
        if not isinstance(result, InferenceResult) or not result.valid():
            LOG.warning("invalid inference result dropped")
            continue
        route = routes.get((result.pipeline, result.camera_id))
        if not route or route[:2] != (result.session_id, result.generation_id):
            continue
        try:
            route[2].put(result, block=False)
        except queue.Full:
            LOG.warning("camera result channel full: %s/%s", result.pipeline, result.camera_id)


def inference_worker_main(*, stage: str, job_queue, result_queue, stop_event,
                          build_handler: Callable[[], Callable[[Any], Any]],
                          max_batch_size: int = 4, max_batch_wait_ms: float = 5.0,
                          ready_queue=None) -> None:
    started = time.monotonic()
    try:
        handler = build_handler()
    except Exception:
        if ready_queue is not None:
            ready_queue.put({"stage": stage, "ready": False, "error_code": "MODEL_LOAD_FAILED"})
        raise
    if ready_queue is not None:
        ready_queue.put({"stage": stage, "ready": True,
                         "model_load_ms": round((time.monotonic()-started)*1000, 3)})
    max_batch_size = max(1, int(max_batch_size))
    wait_sec = max(0.0, float(max_batch_wait_ms) / 1000.0)
    while not stop_event.is_set():
        try:
            first = job_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if first is None:
            return
        jobs = [first]
        deadline = time.monotonic() + wait_sec
        while len(jobs) < max_batch_size and time.monotonic() < deadline:
            try:
                item = job_queue.get_nowait()
            except queue.Empty:
                time.sleep(min(0.001, max(0.0, deadline-time.monotonic())))
                continue
            if item is None:
                break
            jobs.append(item)
        for job in jobs:
            if not isinstance(job, InferenceJob) or not job.valid() or job.stage != stage:
                continue
            infer_started = time.monotonic_ns()
            try:
                payload = handler(job.image)
                result = InferenceResult.from_job(
                    job, success=True, payload=payload,
                    queue_wait_ms=(infer_started-job.created_ns)/1e6,
                    inference_ms=(time.monotonic_ns()-infer_started)/1e6,
                )
            except Exception:
                result = InferenceResult.from_job(job, success=False, error_code="INFERENCE_FAILED")
            try:
                result_queue.put(result, timeout=0.2)
            except queue.Full:
                pass
