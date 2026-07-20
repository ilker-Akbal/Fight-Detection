from __future__ import annotations

import argparse
import time
from pathlib import Path

import cv2

from HizTespiti.motion.src.bg_subtractor import BackgroundMotionDetector
from HizTespiti.motion.src.camera_reader import CameraReader
from HizTespiti.motion.src.motion_config import load_config
from HizTespiti.motion.src.motion_event import MotionEventWriter
from HizTespiti.motion.src.motion_gate import MotionGate
from HizTespiti.motion.src.roi_mask import RoiMask
from HizTespiti.motion.src.utils import ensure_dir, now_stamp, resize_keep_aspect
from HizTespiti.motion.src.visualizer import MotionVisualizer


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--config",
        default="HizTespiti/motion/configs/motion.yaml",
        help="Motion config yaml path",
    )
    p.add_argument("--source", default=None, help="Video/RTSP/Webcam source override")
    p.add_argument("--camera-id", default=None, help="Camera ID override")
    p.add_argument("--show", action="store_true", help="Force show window")
    p.add_argument("--no-show", action="store_true", help="Disable show window")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config(args.config)

    if args.source is not None:
        cfg.camera.source = args.source

    if args.camera_id is not None:
        cfg.camera.camera_id = args.camera_id

    if args.show:
        cfg.runtime.show = True

    if args.no_show:
        cfg.runtime.show = False

    out_dir = ensure_dir(cfg.runtime.output_dir)

    reader = CameraReader(cfg.camera.source)
    fps = reader.fps()

    roi = RoiMask(cfg.roi.enabled, cfg.roi.polygon)
    detector = BackgroundMotionDetector(cfg.motion)
    gate = MotionGate(cfg.motion)
    visualizer = MotionVisualizer()
    event_writer = MotionEventWriter(out_dir, cfg.camera.camera_id)

    writer = None
    debug_video_path = None

    frame_idx = 0
    last_frame_time = 0.0

    print("=" * 90)
    print("HIZ TESPITI - MOTION GATE")
    print(f"camera_id     : {cfg.camera.camera_id}")
    print(f"source        : {cfg.camera.source}")
    print(f"output_dir    : {out_dir}")
    print(f"show          : {cfg.runtime.show}")
    print(f"save_debug    : {cfg.runtime.save_debug_video}")
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

        mask_roi = roi.get_mask(frame.shape)
        motion = detector.detect(frame, mask_roi)

        boxes = motion["boxes"]
        motion_score = motion["motion_score"]

        gate_result = gate.update(
            frame_idx=frame_idx,
            motion_score=motion_score,
            boxes_count=len(boxes),
        )

        timestamp_sec = frame_idx / max(fps, 1.0)

        if gate_result.opened:
            event_writer.write(
                event_type="motion_opened",
                frame_idx=frame_idx,
                timestamp_sec=timestamp_sec,
                motion_score=motion_score,
                boxes_count=len(boxes),
            )
            print(f"[MOTION OPENED] frame={frame_idx} score={motion_score:.5f} boxes={len(boxes)}")

        if gate_result.closed:
            event_writer.write(
                event_type="motion_closed",
                frame_idx=frame_idx,
                timestamp_sec=timestamp_sec,
                motion_score=motion_score,
                boxes_count=len(boxes),
            )
            print(f"[MOTION CLOSED] frame={frame_idx} score={motion_score:.5f} boxes={len(boxes)}")

        vis = visualizer.draw(
            frame=frame,
            boxes=boxes,
            motion_score=motion_score,
            gate=gate_result,
            frame_idx=frame_idx,
            camera_id=cfg.camera.camera_id,
        )
        vis = roi.draw(vis)

        if cfg.runtime.save_debug_video:
            if writer is None:
                h, w = vis.shape[:2]
                debug_video_path = out_dir / f"{cfg.camera.camera_id}_motion_debug_{now_stamp()}.mp4"
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(str(debug_video_path), fourcc, fps, (w, h))

            writer.write(vis)

        if cfg.runtime.show:
            cv2.imshow("HizTespiti Motion Gate", vis)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("q"):
                break

    reader.close()

    if writer is not None:
        writer.release()
        print(f"Debug video kaydedildi: {debug_video_path}")

    cv2.destroyAllWindows()
    print("Motion gate kapandı.")


if __name__ == "__main__":
    main()