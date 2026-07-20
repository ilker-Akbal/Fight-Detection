from __future__ import annotations

import argparse
import time

import cv2

from HizTespiti.motion.src.bg_subtractor import BackgroundMotionDetector
from HizTespiti.motion.src.camera_reader import CameraReader
from HizTespiti.motion.src.motion_gate import MotionGate
from HizTespiti.motion.src.roi_mask import RoiMask

from HizTespiti.yolo.src.simple_tracker import SimpleIoUTracker
from HizTespiti.yolo.src.utils import ensure_dir, now_stamp, resize_keep_aspect
from HizTespiti.yolo.src.vehicle_detector import VehicleDetector
from HizTespiti.yolo.src.visualizer import YoloVisualizer
from HizTespiti.yolo.src.yolo_config import load_config
from HizTespiti.yolo.src.yolo_event import YoloEventWriter


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--config",
        default="HizTespiti/yolo/configs/yolo.yaml",
        help="YOLO config yaml path",
    )
    p.add_argument("--source", default=None, help="Video/RTSP/Webcam source override")
    p.add_argument("--camera-id", default=None, help="Camera ID override")
    p.add_argument("--weights", default=None, help="YOLO weights override")
    p.add_argument("--show", action="store_true", help="Force show window")
    p.add_argument("--no-show", action="store_true", help="Disable show window")
    p.add_argument("--no-motion", action="store_true", help="Disable motion gate, always run YOLO by stride")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config(args.config)

    if args.source is not None:
        cfg.camera.source = args.source

    if args.camera_id is not None:
        cfg.camera.camera_id = args.camera_id

    if args.weights is not None:
        cfg.yolo.weights = args.weights

    if args.show:
        cfg.runtime.show = True

    if args.no_show:
        cfg.runtime.show = False

    if args.no_motion:
        cfg.motion.enabled = False

    out_dir = ensure_dir(cfg.runtime.output_dir)

    reader = CameraReader(cfg.camera.source)
    fps = reader.fps()

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

    visualizer = YoloVisualizer()
    event_writer = YoloEventWriter(out_dir, cfg.camera.camera_id)

    writer = None
    debug_video_path = None

    frame_idx = 0
    last_frame_time = 0.0
    last_tracks = []

    print("=" * 90)
    print("HIZ TESPITI - YOLO VEHICLE DETECTION")
    print(f"camera_id       : {cfg.camera.camera_id}")
    print(f"source          : {cfg.camera.source}")
    print(f"weights         : {cfg.yolo.weights}")
    print(f"motion_enabled  : {cfg.motion.enabled}")
    print(f"yolo_stride     : {cfg.yolo.stride}")
    print(f"output_dir      : {out_dir}")
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

            timestamp_sec = frame_idx / max(fps, 1.0)

            if cfg.events.write_jsonl:
                for tr in last_tracks:
                    if tr.age >= cfg.events.min_track_age:
                        event_writer.write_track(
                            frame_idx=frame_idx,
                            timestamp_sec=timestamp_sec,
                            track=tr,
                        )

        else:
            if motion_active:
                last_tracks = tracker.active_tracks()
            else:
                last_tracks = []

        vis = visualizer.draw(
            frame=frame,
            detections_count=len(detections),
            tracks=last_tracks,
            motion_gate=gate_result,
            frame_idx=frame_idx,
            camera_id=cfg.camera.camera_id,
            yolo_ran=yolo_ran,
        )

        vis = roi.draw(vis)

        if cfg.runtime.save_debug_video:
            if writer is None:
                h, w = vis.shape[:2]
                debug_video_path = out_dir / f"{cfg.camera.camera_id}_yolo_debug_{now_stamp()}.mp4"
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(str(debug_video_path), fourcc, fps, (w, h))

            writer.write(vis)

        if cfg.runtime.show:
            cv2.imshow("HizTespiti YOLO Vehicle Detection", vis)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("q"):
                break

    reader.close()

    if writer is not None:
        writer.release()
        print(f"Debug video kaydedildi: {debug_video_path}")

    cv2.destroyAllWindows()
    print("YOLO vehicle detection kapandı.")


if __name__ == "__main__":
    main()