from __future__ import annotations

import queue
import time
from pathlib import Path

import cv2

from fight.pipeline_mp.common import MpPaths, configure_process_runtime, now_str
from fight.pipeline_mp.messages import CameraFrame, CameraIngestSignal, ReportMessage


def _report(report_queue, camera_id: str, detail: str, **extra) -> None:
    if report_queue is None:
        return
    row = {
        "ts": now_str(),
        "camera_id": str(camera_id),
        "stage": "camera_preview",
        "detail": str(detail),
    }
    row.update(extra)
    try:
        report_queue.put(ReportMessage(kind="status", row=row), timeout=0.5)
    except Exception:
        pass


def write_preview_atomic(path: Path, frame, jpeg_quality: int) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ok, buffer = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
        )
        if not ok:
            return False
        temporary = path.with_name(path.name + ".ingest.tmp.jpg")
        temporary.write_bytes(buffer.tobytes())
        temporary.replace(path)
        return True
    except Exception:
        return False


def run_preview_consumer_loop(
    config: dict,
    camera: dict,
    preview_channel,
    report_queue,
    stop_event,
    generation: int,
) -> None:
    runtime = config.get("runtime", {})
    camera_id = str(camera["camera_id"])
    preview_path = MpPaths.from_output_dir(config["output_dir"]).previews_dir / f"{camera_id}.jpg"
    quality = int(runtime.get("preview_jpeg_quality", 75))
    interval = max(0.0, float(runtime.get("preview_write_interval_sec", 0.25)))
    last_write = 0.0
    frames_received = 0
    frames_written = 0
    last_frame_seq = 0

    _report(report_queue, camera_id, "started", generation=int(generation))
    while stop_event is None or not stop_event.is_set():
        try:
            message = preview_channel.get(timeout=0.25)
        except queue.Empty:
            continue

        if isinstance(message, CameraIngestSignal):
            if message.detail in {"eof", "stopping", "fatal_error"}:
                break
            continue
        if not isinstance(message, CameraFrame):
            continue
        if message.camera_id != camera_id or int(message.generation) != int(generation):
            continue

        frames_received += 1
        last_frame_seq = int(message.frame_seq)
        now = time.monotonic()
        if now - last_write < interval:
            continue
        if write_preview_atomic(preview_path, message.frame, quality):
            frames_written += 1
            last_write = now

    _report(
        report_queue,
        camera_id,
        "summary",
        generation=int(generation),
        frames_received=frames_received,
        frames_written=frames_written,
        last_frame_seq=last_frame_seq,
    )
    _report(report_queue, camera_id, "stopped", generation=int(generation))


def camera_preview_process_main(
    config: dict,
    camera: dict,
    preview_channel,
    report_queue,
    stop_event,
    generation: int,
) -> None:
    runtime = config.get("runtime", {})
    configure_process_runtime(
        cv2_threads=int(
            runtime.get("camera_preview_cv2_threads", runtime.get("cv2_threads", 1))
        ),
        enable_cuda_tuning=False,
    )
    run_preview_consumer_loop(
        config,
        camera,
        preview_channel,
        report_queue,
        stop_event,
        generation,
    )
