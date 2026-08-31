from __future__ import annotations

import os
import signal
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import cv2

from HizTespiti.motion.src.bg_subtractor import BackgroundMotionDetector
from HizTespiti.motion.src.camera_reader import CameraReader
from HizTespiti.motion.src.motion_gate import MotionGate
from HizTespiti.motion.src.roi_mask import RoiMask

from HizTespiti.yolo.src.simple_tracker import SimpleIoUTracker
from HizTespiti.yolo.src.vehicle_detector import VehicleDetector

from HizTespiti.speed.src.calibration_loader import LoadedCalibration, load_calibration
from HizTespiti.speed.src.evidence_writer import EvidenceWriter, FrameBuffer
from HizTespiti.speed.src.speed_config import AppConfig, load_config
from HizTespiti.speed.src.speed_estimator import SpeedEstimator
from HizTespiti.speed.src.utils import ensure_dir, resize_keep_aspect
from HizTespiti.speed.src.violation_decider import ViolationDecider
from HizTespiti.speed.src.visualizer import SpeedVisualizer

from .config import SpeedMpCamera, SpeedMpConfig
from .messages import speed_event_message, status_message


_STOP = False


def _handle_stop(signum, frame):
    global _STOP
    _STOP = True


def _install_signal_handlers() -> None:
    try:
        signal.signal(signal.SIGTERM, _handle_stop)
    except Exception:
        pass

    try:
        signal.signal(signal.SIGINT, _handle_stop)
    except Exception:
        pass

    if os.name == "nt":
        try:
            signal.signal(signal.SIGBREAK, _handle_stop)
        except Exception:
            pass


def _put_report(report_queue, msg: dict[str, Any]) -> None:
    try:
        report_queue.put_nowait(msg)
    except Exception:
        try:
            report_queue.put(msg, timeout=1.0)
        except Exception:
            pass


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _patch_cfg(base_cfg: AppConfig, mp_cfg: SpeedMpConfig, cam: SpeedMpCamera, camera_out_dir: Path) -> AppConfig:
    cfg = base_cfg

    cfg.camera.camera_id = cam.camera_id
    cfg.camera.source = cam.source

    cfg.runtime.output_dir = str(camera_out_dir)
    cfg.runtime.show = False
    cfg.runtime.save_debug_video = bool(mp_cfg.runtime.get("save_debug_video", False))
    cfg.runtime.resize_width = _safe_int(mp_cfg.runtime.get("resize_width"), cfg.runtime.resize_width)
    cfg.runtime.max_fps = _safe_float(mp_cfg.runtime.get("max_fps"), cfg.runtime.max_fps)

    cfg.yolo.weights = str(mp_cfg.yolo_weights or cfg.yolo.weights)
    cfg.yolo.stride = max(1, _safe_int(mp_cfg.yolo.get("stride"), cfg.yolo.stride))
    cfg.yolo.conf = _safe_float(mp_cfg.yolo.get("conf"), cfg.yolo.conf)
    cfg.yolo.iou = _safe_float(mp_cfg.yolo.get("iou"), cfg.yolo.iou)
    cfg.yolo.imgsz = _safe_int(mp_cfg.yolo.get("imgsz"), cfg.yolo.imgsz)
    cfg.yolo.device = str(mp_cfg.yolo.get("device", cfg.yolo.device))

    vehicle_classes = mp_cfg.yolo.get("vehicle_classes")
    if isinstance(vehicle_classes, list) and vehicle_classes:
        cfg.yolo.vehicle_classes = [str(x) for x in vehicle_classes]

    cfg.motion.enabled = bool(mp_cfg.motion.get("enabled", cfg.motion.enabled))

    cfg.tracker.iou_threshold = _safe_float(
        mp_cfg.tracker.get("iou_threshold"),
        cfg.tracker.iou_threshold,
    )
    cfg.tracker.max_age = _safe_int(mp_cfg.tracker.get("max_age"), cfg.tracker.max_age)
    cfg.tracker.min_hits = _safe_int(mp_cfg.tracker.get("min_hits"), cfg.tracker.min_hits)

    cfg.speed.min_track_points = _safe_int(
        mp_cfg.speed.get("min_track_points"),
        cfg.speed.min_track_points,
    )
    cfg.speed.min_time_delta_sec = _safe_float(
        mp_cfg.speed.get("min_time_delta_sec"),
        cfg.speed.min_time_delta_sec,
    )
    cfg.speed.max_time_delta_sec = _safe_float(
        mp_cfg.speed.get("max_time_delta_sec"),
        cfg.speed.max_time_delta_sec,
    )
    cfg.speed.smooth_window = _safe_int(
        mp_cfg.speed.get("smooth_window"),
        cfg.speed.smooth_window,
    )
    cfg.speed.min_valid_speed_kmh = _safe_float(
        mp_cfg.speed.get("min_valid_speed_kmh"),
        cfg.speed.min_valid_speed_kmh,
    )
    cfg.speed.max_valid_speed_kmh = _safe_float(
        mp_cfg.speed.get("max_valid_speed_kmh"),
        cfg.speed.max_valid_speed_kmh,
    )
    cfg.speed.confirm_frames = _safe_int(
        mp_cfg.speed.get("confirm_frames"),
        cfg.speed.confirm_frames,
    )
    cfg.speed.cooldown_sec = _safe_float(
        mp_cfg.speed.get("cooldown_sec"),
        cfg.speed.cooldown_sec,
    )

    cfg.evidence.save_snapshot = bool(cam.save_snapshot)
    cfg.evidence.save_clip = bool(cam.save_clip)
    cfg.evidence.clip_pre_sec = _safe_float(
        mp_cfg.evidence.get("clip_pre_sec"),
        cfg.evidence.clip_pre_sec,
    )
    cfg.evidence.clip_post_sec = _safe_float(
        mp_cfg.evidence.get("clip_post_sec"),
        cfg.evidence.clip_post_sec,
    )
    cfg.evidence.jpeg_quality = _safe_int(
        mp_cfg.evidence.get("jpeg_quality"),
        cfg.evidence.jpeg_quality,
    )

    cfg.roi.enabled = bool(cam.roi_enabled)
    cfg.roi.polygon = cam.roi_polygon or []

    if cam.calibration_path:
        cfg.calibration.path = cam.calibration_path
    else:
        cfg.calibration.path = f"HizTespiti/calibration/out_calibration/{cam.camera_id}_calibration.json"

    return cfg


def _override_calibration(calibration: LoadedCalibration, cam: SpeedMpCamera) -> LoadedCalibration:
    """
    DB'deki hız limiti/tolerans değerleri kamera bazlı override edilir.
    Yeni LoadedCalibration şeması korunur; sadece limit/tolerans güncellenir.
    """
    raw = dict(calibration.raw or {})
    raw["speed_limit_kmh"] = float(cam.speed_limit_kmh)
    raw["tolerance_kmh"] = float(cam.tolerance_kmh)

    return LoadedCalibration(
        camera_id=calibration.camera_id,

        speed_limit_kmh=float(cam.speed_limit_kmh),
        tolerance_kmh=float(cam.tolerance_kmh),

        measurement_mode=calibration.measurement_mode,
        direction=calibration.direction,
        line_a=calibration.line_a,
        line_b=calibration.line_b,
        distance_m=calibration.distance_m,

        road_roi_enabled=calibration.road_roi_enabled,
        road_roi_polygon=calibration.road_roi_polygon,

        meter_per_pixel=calibration.meter_per_pixel,
        scale_confidence=calibration.scale_confidence,
        user_corrected=calibration.user_corrected,

        ready=calibration.ready,
        ready_reason=calibration.ready_reason,

        raw=raw,
    )


def _write_preview(path: Path, frame_bgr, jpeg_quality: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        ok, buf = cv2.imencode(
            ".jpg",
            frame_bgr,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
        )

        if not ok:
            return

        tmp_path = str(path) + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(buf.tobytes())

        os.replace(tmp_path, str(path))

    except Exception:
        pass


def _event_to_dict(event) -> dict[str, Any]:
    if hasattr(event, "__dataclass_fields__"):
        return asdict(event)

    if isinstance(event, dict):
        return dict(event)

    return dict(getattr(event, "__dict__", {}) or {})


def _draw_calibration_overlay(frame, calibration: LoadedCalibration):
    if str(calibration.measurement_mode or "").lower() != "two_line_time_gate":
        return frame

    try:
        if len(calibration.line_a) == 2:
            a1, a2 = calibration.line_a
            cv2.line(frame, tuple(a1), tuple(a2), (0, 255, 255), 3)
            cv2.putText(
                frame,
                "START",
                tuple(a1),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 255),
                2,
                cv2.LINE_AA,
            )

        if len(calibration.line_b) == 2:
            b1, b2 = calibration.line_b
            cv2.line(frame, tuple(b1), tuple(b2), (255, 0, 255), 3)
            cv2.putText(
                frame,
                "END",
                tuple(b1),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 0, 255),
                2,
                cv2.LINE_AA,
            )

        if calibration.distance_m is not None:
            cv2.putText(
                frame,
                f"Distance: {float(calibration.distance_m):.1f} m",
                (18, 86),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

    except Exception:
        pass

    return frame


def camera_process_main(
    camera_dict: dict[str, Any],
    mp_config_dict: dict[str, Any],
    report_queue,
) -> None:
    global _STOP
    _STOP = False
    _install_signal_handlers()

    cam = SpeedMpCamera(**camera_dict)

    mp_cfg = SpeedMpConfig(
        run_name=str(mp_config_dict["run_name"]),
        output_dir=str(mp_config_dict["output_dir"]),
        base_config=str(mp_config_dict["base_config"]),
        yolo_weights=str(mp_config_dict["yolo_weights"]),
        runtime=dict(mp_config_dict.get("runtime") or {}),
        motion=dict(mp_config_dict.get("motion") or {}),
        yolo=dict(mp_config_dict.get("yolo") or {}),
        tracker=dict(mp_config_dict.get("tracker") or {}),
        speed=dict(mp_config_dict.get("speed") or {}),
        evidence=dict(mp_config_dict.get("evidence") or {}),
        cameras=[],
    )

    output_dir = Path(mp_cfg.output_dir)
    camera_out_dir = output_dir
    previews_dir = output_dir / "previews"
    preview_path = previews_dir / f"{cam.camera_id}.jpg"

    preview_every_frames = max(1, _safe_int(mp_cfg.runtime.get("preview_every_frames"), 3))
    preview_jpeg_quality = _safe_int(mp_cfg.runtime.get("preview_jpeg_quality"), 80)
    status_every_frames = max(1, _safe_int(mp_cfg.runtime.get("status_every_frames"), 15))
    reconnect_sec = _safe_float(mp_cfg.runtime.get("reconnect_sec"), 1.0)

    reader = None
    writer = None

    try:
        _put_report(
            report_queue,
            status_message(
                camera_id=cam.camera_id,
                stage="init",
                detail="starting",
            ),
        )

        cfg = load_config(mp_cfg.base_config)
        cfg = _patch_cfg(cfg, mp_cfg, cam, camera_out_dir)

        calibration = load_calibration(cfg.calibration.path)
        calibration = _override_calibration(calibration, cam)

        if not calibration.ready:
            raise RuntimeError(
                f"{cam.camera_id} için hız kalibrasyonu tamamlanmamış. "
                f"Neden: {calibration.ready_reason}. "
                f"Dashboard üzerinden ROI + başlangıç çizgisi + bitiş çizgisi + gerçek mesafe gir."
            )

        reader = CameraReader(cfg.camera.source, reconnect_wait_sec=reconnect_sec)
        fps = reader.fps()

        frame_buffer_size = int((cfg.evidence.clip_pre_sec + cfg.evidence.clip_post_sec + 2) * fps)
        frame_buffer = FrameBuffer(max_frames=max(30, frame_buffer_size))

        if calibration.road_roi_enabled and calibration.road_roi_polygon:
            roi = RoiMask(True, calibration.road_roi_polygon)
        else:
            roi = RoiMask(cfg.roi.enabled, cfg.roi.polygon)

        motion_detector = None
        motion_gate = None

        if cfg.motion.enabled:
            motion_detector = BackgroundMotionDetector(cfg.motion)
            motion_gate = MotionGate(cfg.motion)

        detector = VehicleDetector(cfg.yolo)

        tracker = SimpleIoUTracker(
            iou_threshold=cfg.tracker.iou_threshold,
            max_age=cfg.tracker.max_age,
            min_hits=cfg.tracker.min_hits,
        )

        speed_estimator = SpeedEstimator(
            cfg=cfg.speed,
            calibration=calibration,
            fps=fps,
        )

        decider = ViolationDecider(
            speed_limit_kmh=calibration.speed_limit_kmh,
            tolerance_kmh=calibration.tolerance_kmh,
            confirm_frames=cfg.speed.confirm_frames,
            cooldown_sec=cfg.speed.cooldown_sec,
        )

        evidence_writer = EvidenceWriter(
            output_dir=ensure_dir(cfg.runtime.output_dir),
            camera_id=cfg.camera.camera_id,
            cfg=cfg.evidence,
            fps=fps,
        )

        visualizer = SpeedVisualizer()

        frame_idx = 0
        last_frame_time = 0.0
        last_tracks = []
        latest_speed_kmh = None
        latest_violation = False

        _put_report(
            report_queue,
            status_message(
                camera_id=cam.camera_id,
                stage="ready",
                detail="camera_started",
                fps=fps,
            ),
        )

        print("=" * 90, flush=True)
        print("HIZ TESPITI CAMERA PROCESS", flush=True)
        print(f"camera_id        : {cfg.camera.camera_id}", flush=True)
        print(f"source           : {cfg.camera.source}", flush=True)
        print(f"weights          : {cfg.yolo.weights}", flush=True)
        print(f"calibration      : {cfg.calibration.path}", flush=True)
        print(f"calibration_mode : {calibration.measurement_mode}", flush=True)
        print(f"direction        : {calibration.direction}", flush=True)
        print(f"distance_m       : {calibration.distance_m}", flush=True)
        print(f"meter_per_pixel  : {calibration.meter_per_pixel}", flush=True)
        print(f"speed_limit      : {calibration.speed_limit_kmh}", flush=True)
        print(f"tolerance        : {calibration.tolerance_kmh}", flush=True)
        print(f"motion_enabled   : {cfg.motion.enabled}", flush=True)
        print(f"preview_path     : {preview_path}", flush=True)
        print(f"output_dir       : {cfg.runtime.output_dir}", flush=True)
        print("=" * 90, flush=True)

        while not _STOP:
            if cfg.runtime.max_fps > 0:
                min_dt = 1.0 / cfg.runtime.max_fps
                now = time.time()
                dt = now - last_frame_time

                if dt < min_dt:
                    time.sleep(min_dt - dt)

                last_frame_time = time.time()

            ok, frame = reader.read()

            if not ok or frame is None:
                _put_report(
                    report_queue,
                    status_message(
                        camera_id=cam.camera_id,
                        stage="read",
                        detail="frame_read_failed",
                        frame_idx=frame_idx,
                        fps=fps,
                    ),
                )

                if not str(cfg.camera.source).lower().startswith(("rtsp://", "http://", "https://")):
                    break

                time.sleep(0.05)
                continue

            frame_idx += 1
            frame = resize_keep_aspect(frame, cfg.runtime.resize_width)

            gate_result = None

            if cfg.motion.enabled and motion_detector is not None and motion_gate is not None:
                mask_roi = roi.get_mask(frame.shape)
                motion = motion_detector.detect(frame, mask_roi)

                gate_result = motion_gate.update(
                    frame_idx=frame_idx,
                    motion_score=motion["motion_score"],
                    boxes_count=len(motion["boxes"]),
                )

                motion_active = bool(gate_result.active)
            else:
                motion_active = True

            yolo_ran = False
            detections = []

            should_run_yolo = motion_active and frame_idx % cfg.yolo.stride == 0

            if should_run_yolo:
                yolo_ran = True
                detections = detector.detect(frame)
                last_tracks = tracker.update(detections, frame_idx)
            else:
                if motion_active:
                    last_tracks = tracker.active_tracks()
                else:
                    last_tracks = []

            active_track_ids = {t.track_id for t in last_tracks}
            decider.cleanup(active_track_ids)
            speed_estimator.cleanup(active_track_ids)

            speed_results = {}
            decisions = {}

            timestamp_sec = frame_idx / max(fps, 1.0)
            latest_violation = False

            for tr in last_tracks:
                speed_result = speed_estimator.estimate(tr)
                decision = decider.update(speed_result)

                speed_results[tr.track_id] = speed_result
                decisions[tr.track_id] = decision

                if getattr(speed_result, "speed_kmh", None) is not None:
                    latest_speed_kmh = float(speed_result.speed_kmh)

            vis = visualizer.draw(
                frame=frame,
                tracks=last_tracks,
                speed_results=speed_results,
                decisions=decisions,
                motion_gate=gate_result,
                frame_idx=frame_idx,
                camera_id=cfg.camera.camera_id,
                yolo_ran=yolo_ran,
                speed_limit_kmh=calibration.speed_limit_kmh,
                threshold_kmh=calibration.speed_limit_kmh + calibration.tolerance_kmh,
                meter_per_pixel=calibration.meter_per_pixel,
            )

            vis = roi.draw(vis)
            vis = _draw_calibration_overlay(vis, calibration)

            # Kanıt clipleri ham görüntü değil, ROI/çizgi/box/hız overlayli işlenmiş frame akışından yazılır.
            frame_buffer.add(frame_idx, vis)

            for tr in last_tracks:
                decision = decisions.get(tr.track_id)

                if decision and decision.should_report and decision.speed_kmh is not None:
                    latest_violation = True

                    event = evidence_writer.save_event(
                        frame_idx=frame_idx,
                        timestamp_sec=timestamp_sec,
                        frame_vis=vis,
                        track=tr,
                        speed_kmh=decision.speed_kmh,
                        speed_limit_kmh=calibration.speed_limit_kmh,
                        tolerance_kmh=calibration.tolerance_kmh,
                        threshold_kmh=decision.threshold_kmh,
                        frame_buffer=frame_buffer,
                    )

                    event_dict = _event_to_dict(event)
                    event_dict["run_name"] = mp_cfg.run_name

                    _put_report(report_queue, speed_event_message(event_dict))

                    print(
                        f"[SPEED VIOLATION] "
                        f"camera={event.camera_id} "
                        f"track={event.track_id} "
                        f"speed={event.speed_kmh:.1f} "
                        f"limit={event.speed_limit_kmh:.1f} "
                        f"snapshot={event.snapshot_path}",
                        flush=True,
                    )

            if frame_idx % preview_every_frames == 0:
                # Üstteki ana kamera/preview temiz ham akış olsun; overlay sadece snapshot/clip kanıtlarında kalsın.
                _write_preview(preview_path, frame, preview_jpeg_quality)

            if frame_idx % status_every_frames == 0:
                _put_report(
                    report_queue,
                    status_message(
                        camera_id=cam.camera_id,
                        stage="running",
                        detail="ok",
                        frame_idx=frame_idx,
                        fps=fps,
                        motion_active=motion_active,
                        yolo_ran=yolo_ran,
                        detections=len(detections),
                        tracks=len(last_tracks),
                        latest_speed_kmh=latest_speed_kmh,
                        latest_violation=latest_violation,
                    ),
                )

        _put_report(
            report_queue,
            status_message(
                camera_id=cam.camera_id,
                stage="stopped",
                detail="camera_process_stopped",
                frame_idx=frame_idx,
                fps=fps,
            ),
        )

    except Exception as exc:
        _put_report(
            report_queue,
            status_message(
                camera_id=cam.camera_id,
                stage="error",
                detail="camera_process_failed",
                error=str(exc),
            ),
        )
        raise

    finally:
        try:
            if reader is not None:
                reader.close()
        except Exception:
            pass

        try:
            if writer is not None:
                writer.release()
        except Exception:
            pass

        try:
            cv2.destroyAllWindows()
        except Exception:
            pass