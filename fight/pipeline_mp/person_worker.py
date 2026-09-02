from __future__ import annotations

import queue
import time
from collections import deque
from typing import Callable

from fight.pipeline.adapters import YoloAdapter
from fight.pipeline_mp.batching import collect_request_batch
from fight.pipeline_mp.common import configure_process_runtime, now_str
from fight.pipeline_mp.messages import (
    PersonInferenceRequest,
    PersonInferenceResult,
    ReportMessage,
)
from fight.pipeline_mp.performance import (
    BoundedMetricCollector,
    best_effort_queue_depth,
    compute_inference_timings,
    make_timing_collectors,
    metrics_runtime_config,
    metrics_warmup_requests,
    payload_metadata,
)


class PersonInferenceError(RuntimeError):
    pass


class PersonInferenceTimeout(PersonInferenceError):
    pass


def should_request_person_inference(motion_ok: bool, frame_idx: int, yolo_stride: int) -> bool:
    return bool(motion_ok) and int(frame_idx) % max(1, int(yolo_stride)) == 0


def _report(report_queue, row: dict) -> None:
    if report_queue is None:
        return
    try:
        report_queue.put(ReportMessage(kind="status", row=row), timeout=0.5)
    except Exception:
        pass


class PersonResultValidator:
    """Validates result identity without keeping any detection/tracking state."""

    def __init__(self, camera_id: str, generation: int, history_size: int = 128):
        self.camera_id = str(camera_id)
        self.generation = int(generation)
        self._completed_order = deque(maxlen=max(1, int(history_size)))
        self._completed = set()

    def validate(
        self,
        result: PersonInferenceResult,
        *,
        expected_frame_idx: int,
        expected_request_id: int,
    ) -> tuple[bool, str]:
        if not isinstance(result, PersonInferenceResult):
            return False, "invalid_message_type"
        if str(result.camera_id) != self.camera_id:
            return False, "wrong_camera_id"
        if int(result.generation) != self.generation:
            return False, "stale_generation"

        request_id = int(result.request_id)
        if request_id in self._completed:
            return False, "duplicate_result"
        if request_id != int(expected_request_id):
            return False, "unexpected_request_id"
        if int(result.frame_idx) != int(expected_frame_idx):
            return False, "unexpected_frame_idx"
        return True, "accepted"

    def mark_completed(self, request_id: int) -> None:
        request_id = int(request_id)
        if request_id in self._completed:
            return
        if len(self._completed_order) == self._completed_order.maxlen:
            expired = self._completed_order.popleft()
            self._completed.discard(expired)
        self._completed_order.append(request_id)
        self._completed.add(request_id)


class PersonInferenceClient:
    """Synchronous, one-outstanding-request client used by one camera process."""

    def __init__(
        self,
        *,
        camera_id: str,
        generation: int,
        request_queue,
        result_queue,
        report_queue,
        stop_event,
        inference_timeout_sec: float,
        enqueue_timeout_sec: float,
        performance_metrics_enabled: bool = False,
        performance_metrics_sample_every: int = 1,
        performance_metrics_max_samples: int = 2048,
        performance_metrics_warmup_requests: int = 0,
    ):
        self.camera_id = str(camera_id)
        self.generation = int(generation)
        self.request_queue = request_queue
        self.result_queue = result_queue
        self.report_queue = report_queue
        self.stop_event = stop_event
        self.inference_timeout_sec = max(0.01, float(inference_timeout_sec))
        self.enqueue_timeout_sec = max(0.01, float(enqueue_timeout_sec))
        self.validator = PersonResultValidator(self.camera_id, self.generation)
        self.next_request_id = 1
        self.metrics_enabled = bool(performance_metrics_enabled)
        self.metrics_warmup_requests = max(0, int(performance_metrics_warmup_requests))
        self.stats = {
            "requests": 0,
            "results": 0,
            "timeouts": 0,
            "queue_full": 0,
            "result_drops": 0,
        }
        self.queue_depth_high_water = -1
        self.timing = {
            name: BoundedMetricCollector(
                enabled=self.metrics_enabled,
                sample_every=performance_metrics_sample_every,
                max_samples=performance_metrics_max_samples,
            )
            for name in ("enqueue_ms", "queue_wait_ms", "inference_ms", "result_delivery_ms", "round_trip_ms")
        }
        self.steady_timing = {
            name: BoundedMetricCollector(
                enabled=self.metrics_enabled,
                sample_every=performance_metrics_sample_every,
                max_samples=performance_metrics_max_samples,
            )
            for name in ("enqueue_ms", "queue_wait_ms", "inference_ms", "result_delivery_ms", "round_trip_ms")
        }

    def _status(self, detail: str, **extra) -> None:
        row = {
            "ts": now_str(),
            "camera_id": self.camera_id,
            "stage": "person_inference",
            "detail": detail,
            "generation": self.generation,
        }
        row.update(extra)
        _report(self.report_queue, row)

    def infer(self, frame, frame_idx: int):
        request_created = time.perf_counter()
        payload = payload_metadata(frame) if self.metrics_enabled else {}
        request_id = self.next_request_id
        self.next_request_id += 1
        request = PersonInferenceRequest(
            camera_id=self.camera_id,
            generation=self.generation,
            frame_idx=int(frame_idx),
            request_id=request_id,
            frame=frame,
            created_monotonic=request_created,
            payload_width=int(payload.get("width", 0)),
            payload_height=int(payload.get("height", 0)),
            payload_channels=int(payload.get("channels", 0)),
            payload_bytes=int(payload.get("bytes", 0)),
        )
        self.stats["requests"] += 1

        put_started = time.perf_counter()
        request.put_started_monotonic = put_started
        try:
            self.request_queue.put(request, timeout=self.enqueue_timeout_sec)
        except Exception as exc:
            self.stats["queue_full"] += 1
            self._status(
                "request_queue_full",
                frame_idx=int(frame_idx),
                request_id=request_id,
                error=str(exc),
            )
            raise PersonInferenceTimeout("Person inference request queue is full") from exc
        put_done = time.perf_counter()
        if self.metrics_enabled:
            self.timing["enqueue_ms"].observe((put_done - put_started) * 1000.0)
            depth = best_effort_queue_depth(self.request_queue)
            if depth >= 0:
                self.queue_depth_high_water = max(self.queue_depth_high_water, depth)

        deadline = time.monotonic() + self.inference_timeout_sec
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                raise PersonInferenceError("Pipeline is stopping")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stats["timeouts"] += 1
                self._status(
                    "timeout",
                    frame_idx=int(frame_idx),
                    request_id=request_id,
                    timeout_sec=self.inference_timeout_sec,
                )
                raise PersonInferenceTimeout(
                    f"Person inference timed out after {self.inference_timeout_sec:.3f}s"
                )

            try:
                result = self.result_queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            result_received = time.perf_counter()

            accepted, reason = self.validator.validate(
                result,
                expected_frame_idx=int(frame_idx),
                expected_request_id=request_id,
            )
            if not accepted:
                self.stats["result_drops"] += 1
                self._status(
                    "result_dropped",
                    drop_reason=reason,
                    expected_frame_idx=int(frame_idx),
                    expected_request_id=request_id,
                    result_camera_id=getattr(result, "camera_id", None),
                    result_generation=getattr(result, "generation", None),
                    result_frame_idx=getattr(result, "frame_idx", None),
                    result_request_id=getattr(result, "request_id", None),
                )
                continue

            self.validator.mark_completed(request_id)
            self.stats["results"] += 1
            if self.metrics_enabled:
                timings = compute_inference_timings(
                    request_created=request_created,
                    request_put_started=put_started,
                    request_put_done=put_done,
                    worker_received=float(result.worker_received_monotonic),
                    inference_started=float(result.inference_started_monotonic),
                    inference_ended=float(result.inference_ended_monotonic),
                    result_received=result_received,
                )
                for name, value in timings.items():
                    if name != "enqueue_ms":
                        self.timing[name].observe(value)
                if int(getattr(result, "worker_request_index", 0)) > self.metrics_warmup_requests:
                    self.steady_timing["enqueue_ms"].observe(
                        (put_done - put_started) * 1000.0
                    )
                    for name, value in timings.items():
                        if name != "enqueue_ms":
                            self.steady_timing[name].observe(value)
            if result.error:
                self._status(
                    "worker_error",
                    frame_idx=int(frame_idx),
                    request_id=request_id,
                    error=result.error,
                )
                raise PersonInferenceError(result.error)
            return result.detections

    def performance_summary(self) -> dict:
        out = dict(self.stats)
        out["metrics_enabled"] = self.metrics_enabled
        out["warmup_requests"] = self.metrics_warmup_requests
        out["queue_depth_high_water_best_effort"] = self.queue_depth_high_water
        out["timings"] = {name: collector.summary() for name, collector in self.timing.items()}
        out["_samples"] = {name: collector.values() for name, collector in self.timing.items()}
        out["steady_state_timings"] = {
            name: collector.summary() for name, collector in self.steady_timing.items()
        }
        out["_steady_state_samples"] = {
            name: collector.values() for name, collector in self.steady_timing.items()
        }
        return out


def route_person_result(result: PersonInferenceResult, result_channels: dict, timeout_sec: float) -> tuple[bool, str]:
    channel = result_channels.get(str(getattr(result, "camera_id", "")))
    if channel is None:
        return False, "unknown_camera_id"
    try:
        channel.put(result, timeout=max(0.01, float(timeout_sec)))
        return True, "routed"
    except Exception:
        return False, "camera_result_queue_full"


def person_result_router_main(config: dict, shared_result_queue, result_channels: dict, report_queue, stop_event) -> None:
    runtime = config.get("runtime", {})
    route_timeout_sec = float(runtime.get("person_route_timeout_sec", 0.5))

    _report(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "person_result_router",
            "detail": "started",
            "camera_channel_count": len(result_channels),
        },
    )

    while not stop_event.is_set() or not shared_result_queue.empty():
        try:
            result = shared_result_queue.get(timeout=0.25)
        except queue.Empty:
            continue

        try:
            if result is None:
                break
            routed, reason = route_person_result(result, result_channels, route_timeout_sec)
            if not routed:
                _report(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": getattr(result, "camera_id", "__system__"),
                        "stage": "person_result_router",
                        "detail": "result_dropped",
                        "drop_reason": reason,
                        "generation": getattr(result, "generation", None),
                        "frame_idx": getattr(result, "frame_idx", None),
                        "request_id": getattr(result, "request_id", None),
                    },
                )
        finally:
            try:
                shared_result_queue.task_done()
            except Exception:
                pass

    _report(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "person_result_router",
            "detail": "stopped",
        },
    )


def run_person_inference_loop(
    config: dict,
    request_queue,
    result_queue,
    report_queue,
    stop_event,
    adapter_factory: Callable = YoloAdapter,
) -> None:
    runtime = config.get("runtime", {})
    models = config.get("models", {})
    result_timeout_sec = float(runtime.get("person_result_enqueue_timeout_sec", 1.0))
    metrics_enabled, _, _ = metrics_runtime_config(runtime)
    warmup_requests = metrics_warmup_requests(runtime)
    batch_enabled = bool(runtime.get("person_batch_enabled", False))
    batch_size_limit = max(1, int(runtime.get("person_batch_size", 1)))
    batch_max_wait_ms = max(0.0, float(runtime.get("person_batch_max_wait_ms", 0.0)))
    timing = make_timing_collectors(runtime, ("queue_wait_ms", "inference_ms", "result_enqueue_ms"))
    steady_timing = make_timing_collectors(
        runtime,
        ("queue_wait_ms", "inference_ms", "result_enqueue_ms"),
    )
    batch_timing = make_timing_collectors(
        runtime,
        ("batch_size", "batch_collect_wait_ms", "batch_inference_ms"),
    )
    steady_batch_timing = make_timing_collectors(
        runtime,
        ("batch_size", "batch_collect_wait_ms", "batch_inference_ms"),
    )
    payload_sizes = make_timing_collectors(runtime, ("payload_bytes",))["payload_bytes"]
    requests_processed = 0
    batches_processed = 0
    errors = 0
    payload_bytes_total = 0
    payload_bytes_max = 0
    payload_width_max = 0
    payload_height_max = 0
    payload_channels_max = 0

    _report(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "person_inference",
            "detail": "model_initializing",
            "model_instance_count": 0,
        },
    )

    adapter = adapter_factory(models["yolo_config"], models["yolo_weights"])

    _report(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "person_inference",
            "detail": "model_initialized",
            "model_instance_count": 1,
            "weights": models["yolo_weights"],
            "batch_enabled": batch_enabled,
            "batch_size_limit": batch_size_limit,
            "batch_max_wait_ms": batch_max_wait_ms,
        },
    )

    shutdown_after_batch = False
    while True:
        try:
            first_request = request_queue.get(timeout=0.25)
        except queue.Empty:
            continue

        batch = []
        sentinel_received = False
        try:
            if first_request is None:
                sentinel_received = True
                break

            first_received_monotonic = time.perf_counter()
            collected = collect_request_batch(
                request_queue,
                first_request,
                enabled=batch_enabled,
                batch_size=batch_size_limit,
                max_wait_ms=batch_max_wait_ms,
                first_received_monotonic=first_received_monotonic,
            )
            batch = collected.requests
            sentinel_received = collected.sentinel_received
            shutdown_after_batch = sentinel_received
            batches_processed += 1

            request_indices = list(
                range(requests_processed + 1, requests_processed + len(batch) + 1)
            )
            requests_processed += len(batch)
            started_at = time.time()
            for request, worker_received_monotonic, request_index in zip(
                batch,
                collected.received_monotonic,
                request_indices,
            ):
                payload_bytes = int(getattr(request, "payload_bytes", 0) or 0)
                if metrics_enabled:
                    if payload_bytes <= 0:
                        payload_bytes = payload_metadata(request.frame)["bytes"]
                    payload_sizes.observe(payload_bytes)
                    payload_bytes_total += payload_bytes
                    payload_bytes_max = max(payload_bytes_max, payload_bytes)
                    payload_width_max = max(
                        payload_width_max,
                        int(getattr(request, "payload_width", 0)),
                    )
                    payload_height_max = max(
                        payload_height_max,
                        int(getattr(request, "payload_height", 0)),
                    )
                    payload_channels_max = max(
                        payload_channels_max,
                        int(getattr(request, "payload_channels", 0)),
                    )
                    put_started = float(
                        getattr(request, "put_started_monotonic", 0.0) or 0.0
                    )
                    queue_wait_ms = (
                        max(0.0, (worker_received_monotonic - put_started) * 1000.0)
                        if put_started > 0
                        else 0.0
                    )
                    if put_started > 0:
                        timing["queue_wait_ms"].observe(queue_wait_ms)
                        if request_index > warmup_requests:
                            steady_timing["queue_wait_ms"].observe(queue_wait_ms)

            inference_started_monotonic = time.perf_counter()
            outputs: list[tuple[list, str | None]] | None = None
            if batch_enabled and len(batch) > 1 and hasattr(adapter, "detect_persons_batch"):
                try:
                    batch_outputs = list(
                        adapter.detect_persons_batch([request.frame for request in batch])
                    )
                    if len(batch_outputs) != len(batch):
                        raise RuntimeError(
                            f"Person batch output mismatch: requests={len(batch)} outputs={len(batch_outputs)}"
                        )
                    outputs = [(detections, None) for detections in batch_outputs]
                except Exception:
                    outputs = None
            if outputs is None:
                outputs = []
                for request in batch:
                    try:
                        outputs.append((adapter.detect_persons(request.frame), None))
                    except Exception as exc:
                        outputs.append(([], f"{type(exc).__name__}: {exc}"))
                        errors += 1

            inference_ended_monotonic = time.perf_counter()
            processed_at = time.time()
            inference_ms = (inference_ended_monotonic - inference_started_monotonic) * 1000.0
            if metrics_enabled:
                batch_timing["batch_size"].observe(len(batch))
                batch_timing["batch_collect_wait_ms"].observe(collected.collect_wait_ms)
                batch_timing["batch_inference_ms"].observe(inference_ms)
                all_steady = all(index > warmup_requests for index in request_indices)
                if all_steady:
                    steady_batch_timing["batch_size"].observe(len(batch))
                    steady_batch_timing["batch_collect_wait_ms"].observe(
                        collected.collect_wait_ms
                    )
                    steady_batch_timing["batch_inference_ms"].observe(inference_ms)
                for request_index in request_indices:
                    timing["inference_ms"].observe(inference_ms)
                    if request_index > warmup_requests:
                        steady_timing["inference_ms"].observe(inference_ms)

            for request, worker_received_monotonic, request_index, output in zip(
                batch,
                collected.received_monotonic,
                request_indices,
                outputs,
            ):
                detections, error = output
                result_put_started_monotonic = time.perf_counter()
                result = PersonInferenceResult(
                    camera_id=request.camera_id,
                    generation=request.generation,
                    frame_idx=request.frame_idx,
                    request_id=request.request_id,
                    detections=detections,
                    requested_at=request.created_at,
                    started_at=started_at,
                    processed_at=processed_at,
                    inference_ms=inference_ms,
                    error=error,
                    worker_received_monotonic=worker_received_monotonic,
                    inference_started_monotonic=inference_started_monotonic,
                    inference_ended_monotonic=inference_ended_monotonic,
                    result_put_started_monotonic=result_put_started_monotonic,
                    worker_request_index=request_index,
                )
                try:
                    result_queue.put(result, timeout=max(0.01, result_timeout_sec))
                    if metrics_enabled:
                        result_enqueue_ms = (
                            time.perf_counter() - result_put_started_monotonic
                        ) * 1000.0
                        timing["result_enqueue_ms"].observe(result_enqueue_ms)
                        if request_index > warmup_requests:
                            steady_timing["result_enqueue_ms"].observe(
                                result_enqueue_ms
                            )
                except Exception:
                    errors += 1
                    _report(
                        report_queue,
                        {
                            "ts": now_str(),
                            "camera_id": request.camera_id,
                            "stage": "person_inference",
                            "detail": "result_queue_full",
                            "generation": request.generation,
                            "frame_idx": request.frame_idx,
                            "request_id": request.request_id,
                        },
                    )
        finally:
            for _ in batch:
                try:
                    request_queue.task_done()
                except Exception:
                    pass
            if sentinel_received:
                try:
                    request_queue.task_done()
                except Exception:
                    pass

        if shutdown_after_batch:
            break

    batch_size_summary = batch_timing["batch_size"].summary()
    batch_summary = {
        "enabled": batch_enabled,
        "size_limit": batch_size_limit,
        "max_wait_ms": batch_max_wait_ms,
        "batches_processed": batches_processed,
        "batch_size": batch_size_summary,
        "requests_per_batch": batch_size_summary,
        "batch_collect_wait_ms": batch_timing["batch_collect_wait_ms"].summary(),
        "batch_inference_ms": batch_timing["batch_inference_ms"].summary(),
        "steady_state": {
            "batch_size": steady_batch_timing["batch_size"].summary(),
            "batch_collect_wait_ms": steady_batch_timing["batch_collect_wait_ms"].summary(),
            "batch_inference_ms": steady_batch_timing["batch_inference_ms"].summary(),
        },
    }

    _report(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "person_inference",
            "detail": "summary",
            "metrics_enabled": metrics_enabled,
            "warmup_requests": warmup_requests,
            "requests_processed": requests_processed,
            "batches_processed": batches_processed,
            "errors": errors,
            "queue_wait_ms": timing["queue_wait_ms"].summary(),
            "queue_wait_definition": "worker receive - camera request_queue.put start; includes enqueue blocking",
            "inference_ms": timing["inference_ms"].summary(),
            "result_enqueue_ms": timing["result_enqueue_ms"].summary(),
            "batch_size_mean": batch_size_summary["mean"],
            "batch_size_p50": batch_size_summary["p50"],
            "batch_size_p95": batch_size_summary["p95"],
            "batch_size_max": batch_size_summary["max"],
            "batch": batch_summary,
            "all_requests": {
                "queue_wait_ms": timing["queue_wait_ms"].summary(),
                "inference_ms": timing["inference_ms"].summary(),
                "result_enqueue_ms": timing["result_enqueue_ms"].summary(),
            },
            "steady_state": {
                "requests_excluded": min(warmup_requests, requests_processed),
                "requests_included": max(0, requests_processed - warmup_requests),
                "queue_wait_ms": steady_timing["queue_wait_ms"].summary(),
                "inference_ms": steady_timing["inference_ms"].summary(),
                "result_enqueue_ms": steady_timing["result_enqueue_ms"].summary(),
            },
            "payload_bytes_mean": payload_sizes.summary()["mean"],
            "payload_bytes_max": payload_bytes_max,
            "payload_bytes_total": payload_bytes_total,
            "payload_width_max": payload_width_max,
            "payload_height_max": payload_height_max,
            "payload_channels_max": payload_channels_max,
        },
    )

    _report(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "person_inference",
            "detail": "stopped",
            "model_instance_count": 1,
        },
    )


def person_inference_process_main(config: dict, request_queue, result_queue, report_queue, stop_event) -> None:
    runtime = config.get("runtime", {})
    configure_process_runtime(
        cv2_threads=int(runtime.get("person_worker_cv2_threads", runtime.get("cv2_threads", 1))),
        enable_cuda_tuning=True,
    )
    try:
        run_person_inference_loop(config, request_queue, result_queue, report_queue, stop_event)
    except Exception as exc:
        _report(
            report_queue,
            {
                "ts": now_str(),
                "camera_id": "__system__",
                "stage": "person_inference",
                "detail": "fatal_error",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise
