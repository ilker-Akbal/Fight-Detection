from __future__ import annotations

import argparse
import time

import cv2

from HizTespiti.motion.src.bg_subtractor import BackgroundMotionDetector
from HizTespiti.motion.src.camera_reader import CameraReader
from HizTespiti.motion.src.motion_gate import MotionGate
from HizTespiti.motion.src.roi_mask import RoiMask

from HizTespiti.yolo.src.simple_tracker import SimpleIoUTracker
from HizTespiti.yolo.src.vehicle_detector import VehicleDetector

from HizTespiti.speed.src.calibration_loader import load_calibration
from HizTespiti.speed.src.evidence_writer import EvidenceWriter, FrameBuffer
from HizTespiti.speed.src.speed_config import load_config
from HizTespiti.speed.src.speed_estimator import SpeedEstimator
from HizTespiti.speed.src.utils import ensure_dir, now_stamp, resize_keep_aspect
from HizTespiti.speed.src.violation_decider import ViolationDecider
from HizTespiti.speed.src.visualizer import SpeedVisualizer


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--config",
        default="HizTespiti/speed/configs/speed.yaml",
    )
    p.add_argument("--source", default=None)
    p.add_argument("--camera-id", default=None)
    p.add_argument("--weights", default=None)
    p.add_argument("--calibration", default=None)
    p.add_argument("--show", action="store_true")
    p.add_argument("--no-show", action="store_true")
    p.add_argument("--no-motion", action="store_true")
    return p.parse_args()


def _draw_calibration_overlay(frame, calibration):
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


def main():
    args = parse_args()
    cfg = load_config(args.config)

    if args.source is not None:
        cfg.camera.source = args.source

    if args.camera_id is not None:
        cfg.camera.camera_id = args.camera_id

    if args.weights is not None:
        cfg.yolo.weights = args.weights

    if args.calibration is not None:
        cfg.calibration.path = args.calibration

    if args.show:
        cfg.runtime.show = True

    if args.no_show:
        cfg.runtime.show = False

    if args.no_motion:
        cfg.motion.enabled = False

    out_dir = ensure_dir(cfg.runtime.output_dir)

    calibration = load_calibration(cfg.calibration.path)

    if not calibration.ready:
        raise RuntimeError(
            f"Kalibrasyon tamamlanmamış: {calibration.ready_reason}. "
            f"Dashboard üzerinden ROI + iki ölçüm çizgisi + gerçek mesafe gir."
        )

    reader = CameraReader(cfg.camera.source)
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
        output_dir=out_dir,
        camera_id=cfg.camera.camera_id,
        cfg=cfg.evidence,
        fps=fps,
    )

    visualizer = SpeedVisualizer()

    writer = None
    debug_video_path = None

    frame_idx = 0
    last_frame_time = 0.0
    last_tracks = []

    print("=" * 90)
    print("HIZ TESPITI - SPEED PIPELINE")
    print(f"camera_id        : {cfg.camera.camera_id}")
    print(f"source           : {cfg.camera.source}")
    print(f"weights          : {cfg.yolo.weights}")
    print(f"calibration      : {cfg.calibration.path}")
    print(f"calibration_mode : {calibration.measurement_mode}")
    print(f"direction        : {calibration.direction}")
    print(f"distance_m       : {calibration.distance_m}")
    print(f"meter_per_pixel  : {calibration.meter_per_pixel}")
    print(f"scale_confidence : {calibration.scale_confidence:.3f}")
    print(f"speed_limit      : {calibration.speed_limit_kmh}")
    print(f"tolerance        : {calibration.tolerance_kmh}")
    print(f"motion_enabled   : {cfg.motion.enabled}")
    print(f"roi_enabled      : {calibration.road_roi_enabled or cfg.roi.enabled}")
    print(f"output_dir       : {out_dir}")
    print("=" * 90)

    while True:
        if cfg.runtime.max_fps > 0:
            min_dt = 1.0 / cfg.runtime.max_fps
            now = time.time()
            dt = now - last_frame_time

            if dt < min_dt:
                time.sleep(min_dt - dt)

            last_frame_time = time.time()

        ok, frame = reader.read()

        if not ok or frame is None:
            if not str(cfg.camera.source).lower().startswith(("rtsp://", "http://", "https://")):
                break

            continue

        frame_idx += 1
        frame = resize_keep_aspect(frame, cfg.runtime.resize_width)
        frame_buffer.add(frame_idx, frame)

        gate_result = None

        if cfg.motion.enabled and motion_detector is not None and motion_gate is not None:
            mask_roi = roi.get_mask(frame.shape)
            motion = motion_detector.detect(frame, mask_roi)

            gate_result = motion_gate.update(
                frame_idx=frame_idx,
                motion_score=motion["motion_score"],
                boxes_count=len(motion["boxes"]),
            )

            motion_active = gate_result.active
        else:
            motion_active = True

        yolo_ran = False
        detections = []

        should_run_yolo = (
            motion_active
            and frame_idx % cfg.yolo.stride == 0
        )

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

        for tr in last_tracks:
            speed_result = speed_estimator.estimate(tr)
            decision = decider.update(speed_result)

            speed_results[tr.track_id] = speed_result
            decisions[tr.track_id] = decision

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

        for tr in last_tracks:
            decision = decisions.get(tr.track_id)

            if decision and decision.should_report and decision.speed_kmh is not None:
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

                print(
                    f"[SPEED VIOLATION] "
                    f"camera={event.camera_id} "
                    f"track={event.track_id} "
                    f"speed={event.speed_kmh:.1f} "
                    f"limit={event.speed_limit_kmh:.1f} "
                    f"snapshot={event.snapshot_path}"
                )

        if cfg.runtime.save_debug_video:
            if writer is None:
                h, w = vis.shape[:2]
                debug_video_path = out_dir / f"{cfg.camera.camera_id}_speed_debug_{now_stamp()}.mp4"
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(str(debug_video_path), fourcc, fps, (w, h))

            writer.write(vis)

        if cfg.runtime.show:
            cv2.imshow("HizTespiti Speed Detection", vis)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("q"):
                break

    reader.close()

    if writer is not None:
        writer.release()
        print(f"Debug video kaydedildi: {debug_video_path}")

    cv2.destroyAllWindows()
    print("Speed pipeline kapandı.")


if __name__ == "__main__":
    main()