from __future__ import annotations

import queue
import time
from typing import Callable

import cv2

from fight.pipeline.utils import open_source
from fight.pipeline_mp.common import (
    configure_process_runtime,
    is_file_source,
    now_str,
    redact_source,
)
from fight.pipeline_mp.messages import CameraFrame, CameraIngestSignal, ReportMessage


def _report(report_queue, camera_id: str, detail: str, **extra) -> None:
    if report_queue is None:
        return
    row = {
        "ts": now_str(),
        "camera_id": str(camera_id),
        "stage": "camera_ingest",
        "detail": str(detail),
    }
    row.update(extra)
    try:
        report_queue.put(ReportMessage(kind="status", row=row), timeout=0.5)
    except Exception:
        pass


def _safe_capture_value(capture, prop: int, default: float = 0.0) -> float:
    try:
        value = float(capture.get(prop) or 0.0)
        return value if value > 0.0 else float(default)
    except Exception:
        return float(default)


def _safe_error(exc: Exception, source: str) -> str:
    raw = str(source or "")
    text = f"{type(exc).__name__}: {exc}"
    return text.replace(raw, redact_source(raw)) if raw else text


def publish_latest(channel, item) -> tuple[bool, int]:
    """Latest-frame-wins publication for a bounded consumer-specific queue."""
    try:
        channel.put_nowait(item)
        return True, 0
    except queue.Full:
        pass
    except Exception:
        return False, 0

    dropped = 0
    try:
        channel.get_nowait()
        dropped = 1
    except Exception:
        pass
    try:
        channel.put_nowait(item)
        return True, dropped
    except Exception:
        return False, dropped


def publish_ordered(channel, item, stop_event, timeout_sec: float) -> bool:
    """Bounded-wait ordered publication used for deterministic file cameras."""
    timeout = max(0.01, float(timeout_sec))
    while stop_event is None or not stop_event.is_set():
        try:
            channel.put(item, timeout=timeout)
            return True
        except queue.Full:
            continue
        except Exception:
            return False
    return False


def _publish_signal(
    channel,
    signal: CameraIngestSignal,
    *,
    ordered: bool,
    stop_event,
    timeout: float,
    preserve_pending: bool = False,
) -> None:
    if channel is None:
        return
    if ordered:
        publish_ordered(channel, signal, stop_event, timeout)
    elif preserve_pending:
        try:
            channel.put(signal, timeout=max(0.01, float(timeout)))
        except Exception:
            # A pending preview frame is more valuable than an EOF marker. The
            # supervisor's stop event still gives this consumer a clean exit.
            pass
    else:
        publish_latest(channel, signal)


def run_camera_ingest_loop(
    config: dict,
    camera: dict,
    fight_channel,
    preview_channel,
    report_queue,
    stop_event,
    generation: int,
    *,
    capture_factory: Callable[[str], object] = open_source,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    runtime = config.get("runtime", {})
    camera_id = str(camera["camera_id"])
    source = str(camera["source"])
    safe_source = redact_source(source)
    source_is_file = is_file_source(source)
    reconnect_enabled = bool(runtime.get("camera_reconnect_enabled", True))
    reconnect_initial = max(
        0.01,
        float(runtime.get("camera_reconnect_initial_delay_sec", 0.5)),
    )
    reconnect_max = max(
        reconnect_initial,
        float(runtime.get("camera_reconnect_max_delay_sec", 8.0)),
    )
    publish_timeout = max(
        0.01,
        float(runtime.get("camera_ingest_publish_timeout_sec", 0.2)),
    )
    loop_file_sources = bool(runtime.get("loop_file_sources", False))
    file_fight_ordered = source_is_file and str(
        runtime.get("camera_ingest_file_fight_policy", "ordered")
    ).lower() == "ordered"

    started = time.perf_counter()
    capture = None
    frame_seq = 0
    frames_decoded = 0
    frames_published_fight = 0
    frames_dropped_fight = 0
    frames_published_preview = 0
    frames_dropped_preview = 0
    reconnect_count = 0
    reconnect_delay = reconnect_initial
    source_fps = 0.0
    source_frame_count = 0
    stopping_detail = "stopping"

    _report(
        report_queue,
        camera_id,
        "starting",
        generation=int(generation),
        source=safe_source,
        source_is_file=int(source_is_file),
    )

    try:
        while stop_event is None or not stop_event.is_set():
            try:
                capture = capture_factory(source)
                if capture is None or not capture.isOpened():
                    raise RuntimeError("camera source could not be opened")

                source_fps = _safe_capture_value(capture, cv2.CAP_PROP_FPS, 0.0)
                source_frame_count = int(
                    _safe_capture_value(capture, cv2.CAP_PROP_FRAME_COUNT, 0.0)
                )
                _report(
                    report_queue,
                    camera_id,
                    "source_opened",
                    generation=int(generation),
                    source=safe_source,
                    source_is_file=int(source_is_file),
                    source_fps=round(source_fps, 6),
                    source_frame_count=source_frame_count,
                )
                if reconnect_count > 0:
                    _report(
                        report_queue,
                        camera_id,
                        "reconnected",
                        generation=int(generation),
                        reconnect_count=reconnect_count,
                    )
                reconnect_delay = reconnect_initial
                flow_started = False

                while stop_event is None or not stop_event.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        if source_is_file:
                            _report(
                                report_queue,
                                camera_id,
                                "eof",
                                generation=int(generation),
                                last_frame_seq=frame_seq,
                            )
                            if loop_file_sources:
                                break
                            eof_signal = CameraIngestSignal(
                                camera_id=camera_id,
                                generation=int(generation),
                                detail="eof",
                                frame_seq=frame_seq,
                            )
                            _publish_signal(
                                fight_channel,
                                eof_signal,
                                ordered=file_fight_ordered,
                                stop_event=stop_event,
                                timeout=publish_timeout,
                            )
                            _publish_signal(
                                preview_channel,
                                eof_signal,
                                ordered=False,
                                stop_event=stop_event,
                                timeout=publish_timeout,
                                preserve_pending=True,
                            )
                            stopping_detail = "eof"
                            return

                        _report(
                            report_queue,
                            camera_id,
                            "read_failed",
                            generation=int(generation),
                            last_frame_seq=frame_seq,
                        )
                        break

                    captured_monotonic = time.perf_counter()
                    captured_wall_time = time.time()
                    frame_seq += 1
                    frames_decoded += 1
                    height, width = frame.shape[:2]
                    envelope = CameraFrame(
                        camera_id=camera_id,
                        generation=int(generation),
                        frame_seq=frame_seq,
                        captured_monotonic=captured_monotonic,
                        captured_wall_time=captured_wall_time,
                        frame=frame,
                        source_fps=source_fps,
                        source_width=int(width),
                        source_height=int(height),
                        source_frame_count=source_frame_count,
                    )

                    if file_fight_ordered:
                        published_fight = publish_ordered(
                            fight_channel,
                            envelope,
                            stop_event,
                            publish_timeout,
                        )
                        dropped_fight = 0
                    else:
                        published_fight, dropped_fight = publish_latest(
                            fight_channel,
                            envelope,
                        )
                    frames_published_fight += int(published_fight)
                    frames_dropped_fight += int(dropped_fight or not published_fight)

                    published_preview, dropped_preview = publish_latest(
                        preview_channel,
                        envelope,
                    )
                    frames_published_preview += int(published_preview)
                    frames_dropped_preview += int(
                        dropped_preview or not published_preview
                    )

                    if not flow_started:
                        flow_started = True
                        _report(
                            report_queue,
                            camera_id,
                            "frame_flow_started",
                            generation=int(generation),
                            frame_seq=frame_seq,
                            width=int(width),
                            height=int(height),
                        )

                if stop_event is not None and stop_event.is_set():
                    break

                if source_is_file and loop_file_sources:
                    reconnect_count += 1
                    continue
                if not reconnect_enabled:
                    failure = CameraIngestSignal(
                        camera_id=camera_id,
                        generation=int(generation),
                        detail="fatal_error",
                        frame_seq=frame_seq,
                        error="live source read failed and reconnect is disabled",
                    )
                    _publish_signal(
                        fight_channel,
                        failure,
                        ordered=False,
                        stop_event=stop_event,
                        timeout=publish_timeout,
                    )
                    stopping_detail = "fatal_error"
                    return

            except Exception as exc:
                detail = "source_open_failed" if frame_seq == 0 else "fatal_error"
                _report(
                    report_queue,
                    camera_id,
                    detail,
                    generation=int(generation),
                    source=safe_source,
                    error=_safe_error(exc, source),
                )
                if source_is_file or not reconnect_enabled:
                    failure = CameraIngestSignal(
                        camera_id=camera_id,
                        generation=int(generation),
                        detail="fatal_error",
                        frame_seq=frame_seq,
                        error=_safe_error(exc, source),
                    )
                    _publish_signal(
                        fight_channel,
                        failure,
                        ordered=file_fight_ordered,
                        stop_event=stop_event,
                        timeout=publish_timeout,
                    )
                    _publish_signal(
                        preview_channel,
                        failure,
                        ordered=False,
                        stop_event=stop_event,
                        timeout=publish_timeout,
                        preserve_pending=True,
                    )
                    stopping_detail = "fatal_error"
                    return
            finally:
                if capture is not None:
                    try:
                        capture.release()
                    except Exception:
                        pass
                    capture = None

            reconnect_count += 1
            _report(
                report_queue,
                camera_id,
                "reconnecting",
                generation=int(generation),
                reconnect_count=reconnect_count,
                delay_sec=round(reconnect_delay, 3),
            )
            sleep_fn(reconnect_delay)
            reconnect_delay = min(reconnect_max, reconnect_delay * 2.0)
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass
        elapsed = max(0.0, time.perf_counter() - started)
        if stopping_detail == "stopping":
            signal = CameraIngestSignal(
                camera_id=camera_id,
                generation=int(generation),
                detail="stopping",
                frame_seq=frame_seq,
            )
            _publish_signal(
                fight_channel,
                signal,
                ordered=False,
                stop_event=None,
                timeout=publish_timeout,
            )
            _publish_signal(
                preview_channel,
                signal,
                ordered=False,
                stop_event=None,
                timeout=publish_timeout,
            )
        _report(
            report_queue,
            camera_id,
            "stopping",
            generation=int(generation),
            reason=stopping_detail,
        )
        _report(
            report_queue,
            camera_id,
            "summary",
            generation=int(generation),
            frames_decoded=frames_decoded,
            frames_published_fight=frames_published_fight,
            frames_dropped_fight=frames_dropped_fight,
            frames_published_preview=frames_published_preview,
            frames_dropped_preview=frames_dropped_preview,
            reconnect_count=reconnect_count,
            last_frame_seq=frame_seq,
            elapsed_sec=round(elapsed, 6),
            ingest_decode_fps=round(frames_decoded / elapsed, 6) if elapsed > 0 else 0.0,
        )
        _report(
            report_queue,
            camera_id,
            "stopped",
            generation=int(generation),
            reason=stopping_detail,
        )


def camera_ingest_process_main(
    config: dict,
    camera: dict,
    fight_channel,
    preview_channel,
    report_queue,
    stop_event,
    generation: int,
) -> None:
    runtime = config.get("runtime", {})
    configure_process_runtime(
        cv2_threads=int(
            runtime.get("camera_ingest_cv2_threads", runtime.get("cv2_threads", 1))
        ),
        enable_cuda_tuning=False,
    )
    try:
        run_camera_ingest_loop(
            config,
            camera,
            fight_channel,
            preview_channel,
            report_queue,
            stop_event,
            generation,
        )
    except Exception as exc:
        _report(
            report_queue,
            str(camera.get("camera_id", "__system__")),
            "fatal_error",
            generation=int(generation),
            error=_safe_error(exc, str(camera.get("source", ""))),
        )
        raise
