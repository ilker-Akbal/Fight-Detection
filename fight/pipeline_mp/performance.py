from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path
from typing import Iterable


def _non_negative_ms(seconds: float) -> float:
    return max(0.0, float(seconds) * 1000.0)


def compute_inference_timings(
    *,
    request_created: float,
    request_put_started: float,
    request_put_done: float,
    worker_received: float,
    inference_started: float,
    inference_ended: float,
    result_received: float,
) -> dict[str, float]:
    return {
        "enqueue_ms": _non_negative_ms(request_put_done - request_put_started),
        "queue_wait_ms": _non_negative_ms(worker_received - request_put_done),
        "inference_ms": _non_negative_ms(inference_ended - inference_started),
        "result_delivery_ms": _non_negative_ms(result_received - inference_ended),
        "round_trip_ms": _non_negative_ms(result_received - request_created),
    }


def percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]

    q = min(1.0, max(0.0, float(quantile)))
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def summarize_values(values: Iterable[float]) -> dict[str, float | int]:
    vals = [max(0.0, float(v)) for v in values]
    if not vals:
        return {
            "samples": 0,
            "mean": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "max": 0.0,
        }
    return {
        "samples": len(vals),
        "mean": round(sum(vals) / len(vals), 6),
        "p50": round(percentile(vals, 0.50), 6),
        "p95": round(percentile(vals, 0.95), 6),
        "max": round(max(vals), 6),
    }


class BoundedMetricCollector:
    def __init__(self, *, enabled: bool, sample_every: int = 1, max_samples: int = 2048):
        self.enabled = bool(enabled)
        self.sample_every = max(1, int(sample_every))
        self.max_samples = max(1, int(max_samples))
        self.observations = 0
        self._values = deque(maxlen=self.max_samples)

    def observe(self, value: float) -> None:
        if not self.enabled:
            return
        self.observations += 1
        if (self.observations - 1) % self.sample_every != 0:
            return
        self._values.append(max(0.0, float(value)))

    @property
    def sample_count(self) -> int:
        return len(self._values)

    def values(self) -> list[float]:
        return list(self._values)

    def summary(self) -> dict[str, float | int]:
        out = summarize_values(self._values)
        out["observations"] = self.observations
        out["bounded_capacity"] = self.max_samples
        return out


def payload_metadata(value) -> dict[str, int]:
    shape = tuple(getattr(value, "shape", ()) or ())
    height = int(shape[0]) if len(shape) >= 1 else 0
    width = int(shape[1]) if len(shape) >= 2 else 0
    channels = int(shape[2]) if len(shape) >= 3 else (1 if len(shape) == 2 else 0)
    return {
        "width": width,
        "height": height,
        "channels": channels,
        "bytes": max(0, int(getattr(value, "nbytes", 0) or 0)),
    }


def best_effort_queue_depth(q) -> int:
    try:
        return max(0, int(q.qsize()))
    except Exception:
        return -1


def metrics_runtime_config(runtime: dict) -> tuple[bool, int, int]:
    return (
        bool(runtime.get("performance_metrics_enabled", False)),
        max(1, int(runtime.get("performance_metrics_sample_every", 1))),
        max(1, int(runtime.get("performance_metrics_max_samples", 2048))),
    )


def metrics_warmup_requests(runtime: dict) -> int:
    return max(0, int(runtime.get("performance_metrics_warmup_requests", 2)))


def make_timing_collectors(runtime: dict, names: Iterable[str]) -> dict[str, BoundedMetricCollector]:
    enabled, sample_every, max_samples = metrics_runtime_config(runtime)
    return {
        name: BoundedMetricCollector(
            enabled=enabled,
            sample_every=sample_every,
            max_samples=max_samples,
        )
        for name in names
    }


def _last_summary(rows: list[dict], stage: str) -> dict:
    matches = [row for row in rows if row.get("stage") == stage and row.get("detail") == "summary"]
    return dict(matches[-1]) if matches else {}


def _merge_client_samples(
    camera_rows: list[dict],
    section: str,
    metric: str,
    sample_field: str = "_samples",
) -> list[float]:
    merged = []
    for row in camera_rows:
        client = row.get(section, {}) or {}
        samples = client.get(sample_field, {}) or {}
        merged.extend(float(v) for v in samples.get(metric, []) or [])
    return merged


def _client_totals(camera_rows: list[dict], section: str) -> dict[str, int]:
    fields = ("requests", "results", "timeouts", "queue_full", "result_drops")
    return {
        field: sum(int((row.get(section, {}) or {}).get(field, 0)) for row in camera_rows)
        for field in fields
    }


def _clean_camera_row(row: dict) -> dict:
    clean = dict(row)
    for section in ("person", "pose"):
        if isinstance(clean.get(section), dict):
            clean[section] = dict(clean[section])
            clean[section].pop("_samples", None)
            clean[section].pop("_steady_state_samples", None)
    return clean


def build_performance_summary(
    config: dict,
    status_rows: list[dict],
    wall_processing_sec: float,
) -> dict:
    wall_sec = max(0.0, float(wall_processing_sec))
    camera_rows = [
        row
        for row in status_rows
        if row.get("stage") == "camera_summary" and row.get("detail") == "completed"
    ]
    person_worker = _last_summary(status_rows, "person_inference")
    pose_worker = _last_summary(status_rows, "pose_inference")
    stage3_worker = _last_summary(status_rows, "stage3")
    ingest_rows = [
        dict(row)
        for row in status_rows
        if row.get("stage") == "camera_ingest" and row.get("detail") == "summary"
    ]

    total_frames = sum(int(row.get("frames_read", 0)) for row in camera_rows)
    camera_source_durations: dict[str, float] = {}
    unique_sources: dict[str, float] = {}
    for row in camera_rows:
        camera_id = str(row.get("camera_id", ""))
        source = str(row.get("source", ""))
        duration = float(row.get("source_duration_sec", 0.0))
        camera_source_durations[camera_id] = max(
            camera_source_durations.get(camera_id, 0.0),
            duration,
        )
        unique_sources[source] = max(
            unique_sources.get(source, 0.0),
            duration,
        )
    aggregate_source_duration = sum(camera_source_durations.values())
    unique_source_duration = sum(unique_sources.values())

    person = _client_totals(camera_rows, "person")
    pose = _client_totals(camera_rows, "pose")
    for target, section, worker in (
        (person, "person", person_worker),
        (pose, "pose", pose_worker),
    ):
        def latency_view(sample_field: str, worker_view: dict) -> dict:
            queue_wait_samples = _merge_client_samples(
                camera_rows,
                section,
                "queue_wait_ms",
                sample_field,
            )
            return {
                "enqueue_ms": summarize_values(
                    _merge_client_samples(camera_rows, section, "enqueue_ms", sample_field)
                ),
                "queue_wait_ms": (
                    summarize_values(queue_wait_samples)
                    if queue_wait_samples
                    else worker_view.get("queue_wait_ms", summarize_values([]))
                ),
                "inference_ms": worker_view.get("inference_ms", summarize_values([])),
                "result_delivery_ms": summarize_values(
                    _merge_client_samples(
                        camera_rows,
                        section,
                        "result_delivery_ms",
                        sample_field,
                    )
                ),
                "round_trip_ms": summarize_values(
                    _merge_client_samples(
                        camera_rows,
                        section,
                        "round_trip_ms",
                        sample_field,
                    )
                ),
            }

        all_requests = latency_view("_samples", worker.get("all_requests", worker))
        steady_worker = worker.get("steady_state", {}) or {}
        steady_state = latency_view("_steady_state_samples", steady_worker)
        steady_state["requests_excluded"] = int(
            steady_worker.get("requests_excluded", 0)
        )
        steady_state["requests_included"] = int(
            steady_worker.get("requests_included", 0)
        )
        target.update(all_requests)
        target["all_requests"] = all_requests
        target["steady_state"] = steady_state
        target["warmup_requests"] = int(worker.get("warmup_requests", 0))
        target["batch"] = worker.get("batch", {}) or {}
        target["worker_queue_wait_inclusive_ms"] = worker.get(
            "queue_wait_ms",
            summarize_values([]),
        )
        target["payload_bytes_mean"] = float(worker.get("payload_bytes_mean", 0.0))
        target["payload_bytes_max"] = int(worker.get("payload_bytes_max", 0))
        target["payload_mb_total"] = round(float(worker.get("payload_bytes_total", 0)) / (1024.0 * 1024.0), 6)
        target["payload_width_max"] = int(worker.get("payload_width_max", 0))
        target["payload_height_max"] = int(worker.get("payload_height_max", 0))
        target["payload_channels_max"] = int(worker.get("payload_channels_max", 0))
        target["worker_errors"] = int(worker.get("errors", 0))
        target["queue_depth_high_water_best_effort"] = max(
            [int((row.get(section, {}) or {}).get("queue_depth_high_water_best_effort", -1)) for row in camera_rows]
            or [-1]
        )

    error_details = (
        "result_dropped",
        "timeout",
        "request_queue_full",
        "result_queue_full",
        "fatal_error",
        "person_inference_process_dead",
        "person_result_router_process_dead",
        "pose_inference_process_dead",
        "pose_result_router_process_dead",
        "failed",
    )
    errors = {detail: 0 for detail in error_details}
    for row in status_rows:
        detail = row.get("detail")
        if detail in errors:
            errors[detail] += 1

    stage3 = {
        "jobs_received": int(stage3_worker.get("jobs_received", 0)),
        "jobs_completed": int(stage3_worker.get("jobs_completed", 0)),
        "errors": int(stage3_worker.get("errors", 0)),
        "queue_wait_ms": stage3_worker.get("queue_wait_ms", summarize_values([])),
        "inference_ms": stage3_worker.get("inference_ms", summarize_values([])),
    }
    ingest_elapsed = sum(float(row.get("elapsed_sec", 0.0)) for row in ingest_rows)
    ingest_frames = sum(int(row.get("frames_decoded", 0)) for row in ingest_rows)
    camera_ingest = {
        "mode": str(config.get("runtime", {}).get("camera_ingest_mode", "legacy")),
        "frames_decoded": ingest_frames,
        "frames_published_fight": sum(
            int(row.get("frames_published_fight", 0)) for row in ingest_rows
        ),
        "frames_dropped_fight": sum(
            int(row.get("frames_dropped_fight", 0)) for row in ingest_rows
        ),
        "frames_published_preview": sum(
            int(row.get("frames_published_preview", 0)) for row in ingest_rows
        ),
        "frames_dropped_preview": sum(
            int(row.get("frames_dropped_preview", 0)) for row in ingest_rows
        ),
        "reconnect_count": sum(int(row.get("reconnect_count", 0)) for row in ingest_rows),
        "elapsed_sec_total": round(ingest_elapsed, 6),
        "ingest_decode_fps": round(ingest_frames / ingest_elapsed, 6)
        if ingest_elapsed > 0.0
        else 0.0,
        "cameras": ingest_rows,
    }

    camera_realtime_factors = []
    for row in camera_rows:
        if "camera_source_to_processing_ratio" in row:
            ratio = float(row.get("camera_source_to_processing_ratio", 0.0))
        else:
            elapsed = float(row.get("camera_elapsed_sec", 0.0))
            ratio = (
                float(row.get("source_duration_sec", 0.0)) / elapsed
                if elapsed > 0.0
                else 0.0
            )
        camera_realtime_factors.append(max(0.0, ratio))
    camera_count = len(config.get("cameras", []))
    real_time_factor = aggregate_source_duration / wall_sec if wall_sec > 0 else 0.0

    return {
        "schema_version": 1,
        "run_id": str(config.get("run_id") or config.get("runtime", {}).get("run_id") or ""),
        "run_name": config.get("run_name", ""),
        "camera_count": camera_count,
        "performance_metrics_enabled": bool(
            config.get("runtime", {}).get("performance_metrics_enabled", False)
        ),
        "performance_metrics_warmup_requests": int(
            config.get("runtime", {}).get("performance_metrics_warmup_requests", 2)
        ),
        "wall_processing_sec": round(wall_sec, 6),
        "total_frames_read": total_frames,
        "aggregate_processing_fps": round(total_frames / wall_sec, 6) if wall_sec > 0 else 0.0,
        "source_duration_sec": round(unique_source_duration, 6),
        "unique_source_duration_sec": round(unique_source_duration, 6),
        "aggregate_camera_source_duration_sec": round(aggregate_source_duration, 6),
        "real_time_factor": round(real_time_factor, 6),
        "per_camera_realtime_factor": round(real_time_factor / camera_count, 6)
        if camera_count > 0
        else 0.0,
        "mean_camera_realtime_factor": round(
            sum(camera_realtime_factors) / len(camera_realtime_factors),
            6,
        )
        if camera_realtime_factors
        else 0.0,
        "min_camera_realtime_factor": round(min(camera_realtime_factors), 6)
        if camera_realtime_factors
        else 0.0,
        "person": person,
        "pose": pose,
        "stage3": stage3,
        "camera_ingest": camera_ingest,
        "metric_definitions": {
            "enqueue_ms": "camera request_queue.put done - put start",
            "queue_wait_ms": "worker receive - camera request_queue.put done",
            "worker_queue_wait_inclusive_ms": "worker receive - camera request_queue.put start; includes enqueue blocking",
            "inference_ms": "model inference end - model inference start",
            "batched_request_inference_ms": "wall duration of the one model call containing that request",
            "result_delivery_ms": "camera result receive - model inference end",
            "round_trip_ms": "camera result receive - request creation",
            "aggregate_processing_fps": "total_frames_read / wall_processing_sec",
            "ingest_decode_fps": "camera ingest frames_decoded / summed ingest elapsed_sec",
            "real_time_factor": "aggregate_camera_source_duration_sec / wall_processing_sec",
            "per_camera_realtime_factor": "real_time_factor / configured camera_count",
            "camera_source_to_processing_ratio": "camera source_duration_sec / camera_elapsed_sec",
            "steady_state": "requests after the first performance_metrics_warmup_requests at each model worker",
        },
        "cameras": [_clean_camera_row(row) for row in camera_rows],
        "error_drop_counters": errors,
    }


def load_status_rows(path: str | Path, start_offset: int = 0) -> list[dict]:
    rows = []
    p = Path(path)
    if not p.exists():
        return rows
    with open(p, "rb") as fh:
        fh.seek(max(0, int(start_offset)))
        for raw_line in fh:
            try:
                rows.append(json.loads(raw_line.decode("utf-8")))
            except Exception:
                continue
    return rows
