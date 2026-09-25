from __future__ import annotations

import queue
import time
from pathlib import Path

import cv2

from fight.pipeline_mp.common import MpPaths, configure_process_runtime, now_str
from fight.pipeline_mp.messages import CameraFrame, CameraIngestSignal, ReportMessage
from fight.pipeline_mp.health import HealthEmitter


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
    health_queue=None,
    slot_id: int = -1,
    live_channel=None,
) -> None:
    runtime = config.get("runtime", {})
    from fight.pipeline_mp.attribution import AttributionMetrics
    telemetry = AttributionMetrics(runtime, ("frame_delivery_age_ms",), ("frames_received",))
    camera_id = str(camera["camera_id"])
    preview_path = MpPaths.from_output_dir(config["output_dir"]).previews_dir / f"{camera_id}.jpg"
    quality = int(runtime.get("preview_jpeg_quality", 75))
    if live_channel is not None:
        # Browser preview is observational and lossy by design. Encoding every
        # source frame scales CPU roughly per camera without improving AI
        # correctness, so cap the live JPEG publish rate by default.
        live_fps = max(0.0, float(runtime.get("preview_live_max_fps", 10.0)))
        interval = 0.0 if live_fps <= 0.0 else 1.0 / live_fps
    else:
        interval = max(0.0, float(runtime.get("preview_write_interval_sec", 0.25)))
    if live_channel is not None and hasattr(live_channel, "cancel_join_thread"):
        live_channel.cancel_join_thread()  # Lossy preview must never delay child exit.
    last_write = 0.0
    frames_received = 0
    frames_written = 0
    last_frame_seq = 0
    health = HealthEmitter(
        health_queue,
        component="camera_preview",
        component_type="camera",
        camera_id=camera_id,
        slot_id=slot_id,
        generation=generation,
        interval_sec=float(runtime.get("health_heartbeat_interval_sec", 1.0)),
    )
    health.emit("process_started", force=True)

    _report(report_queue, camera_id, "started", generation=int(generation))
    while stop_event is None or not stop_event.is_set():
        try:
            message = preview_channel.get(timeout=0.25)
        except queue.Empty:
            health.heartbeat(progress=frames_received, secondary_progress=frames_written)
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
        telemetry.count("frames_received")
        if telemetry.enabled:
            telemetry.observe("frame_delivery_age_ms", (time.perf_counter() - message.captured_monotonic) * 1000)
            telemetry.publish(report_queue, "preview", camera_id=camera_id, generation=generation)
        last_frame_seq = int(message.frame_seq)
        now = time.monotonic()
        if now - last_write < interval:
            continue
        if live_channel is None:
            published = write_preview_atomic(preview_path, message.frame, quality)
        else:
            from fight.pipeline_mp.preview_gateway import MAX_JPEG
            ok, encoded = cv2.imencode(".jpg", message.frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            published = False
            if ok and encoded.nbytes <= MAX_JPEG:
                try:
                    from fight.pipeline_mp.camera_ingest import publish_latest
                    published, _ = publish_latest(live_channel, (
                        camera_id, slot_id, generation, message.frame_seq,
                        message.captured_monotonic, encoded.tobytes()))
                except (queue.Full, OSError, ValueError):
                    pass
        if published:
            frames_written += 1
            last_write = now
            health.emit(
                "preview_published",
                progress=frames_received,
                secondary_progress=frames_written,
            )

    telemetry.publish(report_queue, "preview", force=True, camera_id=camera_id, generation=generation)
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
    health.emit(
        "process_stopping",
        force=True,
        progress=frames_received,
        secondary_progress=frames_written,
    )


def camera_preview_process_main(
    config: dict,
    camera: dict,
    preview_channel,
    report_queue,
    stop_event,
    generation: int,
    health_queue=None,
    slot_id: int = -1,
    live_channel=None,
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
        health_queue,
        slot_id,
        live_channel,
    )
