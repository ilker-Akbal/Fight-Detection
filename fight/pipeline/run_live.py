from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

import cv2

from fight.pipeline.adapters import MotionAdapter, PoseLiveAdapter, Stage3Adapter, YoloAdapter
from fight.pipeline.clip_buffer import save_clip_mp4
from fight.pipeline.debug_view import build_debug_lines, compose_debug_view
from fight.pipeline.pair_selector import LivePairRoiController
from fight.pipeline.person_stabilizer import TemporalPersonStabilizer
from fight.pipeline.utils import crop_from_box, open_source, sanitize_box, box_iou


def pair_debug_enabled() -> bool:
    v = os.getenv("PAIR_DEBUG", "0").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _count_persons_in_roi(persons, roi_box, min_iou=0.08, min_center_inside=True):
    if roi_box is None:
        return 0

    rx1, ry1, rx2, ry2 = roi_box
    cnt = 0

    for _, box in persons:
        bx1, by1, bx2, by2 = box
        cx = 0.5 * (bx1 + bx2)
        cy = 0.5 * (by1 + by2)

        iou = box_iou(box, roi_box)
        center_inside = (rx1 <= cx <= rx2) and (ry1 <= cy <= ry2)

        if iou >= min_iou or (min_center_inside and center_inside):
            cnt += 1

    return cnt


def main():
    if pair_debug_enabled():
        logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=str, default="0")
    ap.add_argument("--motion-config", type=str, default="fight/motion/configs/motion.yaml")
    ap.add_argument("--yolo-config", type=str, default="fight/yolo/configs/yolo.yaml")
    ap.add_argument("--yolo-weights", type=str, default="fight/yolo11n.pt")
    ap.add_argument("--yolo-stride", type=int, default=6)
    ap.add_argument("--person-conf", type=float, default=0.25)
    ap.add_argument("--use-pose", action="store_true")
    ap.add_argument("--pose-config", type=str, default="fight/pose/configs/pose.yaml")
    ap.add_argument("--pose-stride", type=int, default=2)
    ap.add_argument("--use-stage3", action="store_true")
    ap.add_argument("--stage3-config", type=str, default="fight/3D_CNN/configs/stage3.yaml")
    ap.add_argument("--stage3-stride", type=int, default=10)
    ap.add_argument("--min-2p-frames", type=int, default=8)
    ap.add_argument("--fight-thr", type=float, default=0.6)
    ap.add_argument("--show", action="store_true", default=True)
    ap.add_argument("--reconnect-sec", type=float, default=1.0)
    args = ap.parse_args()

    cap = open_source(args.source)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open source: {args.source}")

    motion = MotionAdapter(args.motion_config)
    yolo = YoloAdapter(args.yolo_config, args.yolo_weights)
    pose = PoseLiveAdapter(args.pose_config) if args.use_pose else None
    stage3 = Stage3Adapter(args.stage3_config) if args.use_stage3 else None

    person_stabilizer = TemporalPersonStabilizer(
        max_age=8,
        min_hits=1,
        iou_match_thr=0.22,
        conf_alpha=0.65,
        max_tracks=12,
    )

    pair_roi = LivePairRoiController(
        enter_score=0.58,
        keep_score=0.42,
        keep_frames=12,
        min_hits_to_activate=2,
        candidate_confirm_frames=2,
        pair_identity_iou_thr=0.35,
        switch_margin=0.06,
        roi_expand_x=1.20,
        roi_expand_y=1.14,
        debug=pair_debug_enabled(),
    )

    if stage3 is not None:
        print(
            f"[INIT] stage3_clip_len={stage3.clip_len} "
            f"stage3_stride={args.stage3_stride} "
            f"use_pose={int(pose is not None)}"
        )

    clip_debug_dir = Path("fight/clip_debug")
    clip_debug_dir.mkdir(parents=True, exist_ok=True)
    clip_save_idx = 0

    clip = []
    yolo_ctr = 0
    pose_ctr = 0
    stage3_cooldown = 0

    two_p_ctr = 0
    two_p_miss_ctr = 0
    two_p_grace_frames = 20

    stable_roi_box = None
    roi_miss_frames = 0
    clip_reset_frames = 40
    clip_soft_hold_frames = 10
    last_valid_roi_frame = None
    last_crop_live = False

    last_direct_roi_box = None
    last_direct_roi_ts = 0.0
    direct_roi_hold_sec = 1.0

    last_fight_prob = 0.0
    last_pose_score = 0.0
    last_pose_ok = False
    last_pose_vis = None

    last_pair_idx = None
    last_pair_score = 0.0
    pair_found = False

    last_pair_ok_ts = 0.0
    pair_hold_sec = 1.0

    last_pose_ok_ts = 0.0
    pose_missing_since = None
    pose_hold_sec = 1.0
    pose_reset_sec = 2.5

    last_pose_trigger_ts = 0.0
    pose_trigger_hold_sec = 2.4

    roi_invalid_ctr = 0
    roi_invalid_drop_frames = 4
    roi_person_min_count = 2

    fight_state = False
    fight_on_need = 3
    fight_off_need = 5
    fight_on_ctr = 0
    fight_off_ctr = 0

    recent_live_pair_ctr = 0
    recent_live_pair_hold = 18

    last_t = time.time()
    fps = 0.0
    debug_ctr = 0

    if args.show:
        cv2.namedWindow("fight_live", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("fight_live", 1700, 1000)

        if pose is not None:
            cv2.namedWindow("pose_roi", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("pose_roi", 700, 700)

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                if Path(args.source).suffix.lower() in [".mp4", ".avi", ".mov", ".mkv"]:
                    print("video ended.")
                    break

                print("cap.read() failed, reconnecting...")
                try:
                    cap.release()
                except Exception:
                    pass
                time.sleep(args.reconnect_sec)
                cap = open_source(args.source)
                continue

            should_append_clip = False
            raw_pose_ready = False
            effective_pose_ready = False
            pose_capture_active = False
            can_classify = False
            roi_available = False
            roi_person_count = 0

            now = time.time()
            dt = now - last_t
            if dt > 0:
                fps = 0.9 * fps + 0.1 * (1.0 / dt) if fps > 0 else (1.0 / dt)
            last_t = now

            score, active, frame_resized, motion_vis = motion.step(now, frame)
            view = frame if frame_resized is None else frame_resized.copy()
            base_frame = frame if frame_resized is None else frame_resized

            yolo_ctr += 1
            yolo_ran = False
            det_persons = []

            if active and (yolo_ctr % max(1, args.yolo_stride) == 0):
                dets = yolo.detect_persons(view)
                det_persons = [(c, box) for (c, box) in dets if c >= args.person_conf]
                det_persons.sort(key=lambda x: x[0], reverse=True)
                persons = person_stabilizer.update(det_persons)
                yolo_ran = True
            elif active:
                persons = person_stabilizer.predict_only()
            else:
                person_stabilizer.reset()
                persons = []

            conf_list = [round(c, 2) for c, _ in persons]

            for idx, (c, (x1, y1, x2, y2)) in enumerate(persons):
                cv2.rectangle(view, (x1, y1), (x2, y2), (255, 0, 0), 2)
                cv2.putText(
                    view,
                    f"{idx}:{c:.2f}",
                    (x1, max(0, y1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (255, 0, 0),
                    2,
                )

            if active:
                pair_res = pair_roi.update(persons, base_frame.shape)
            else:
                pair_roi.reset()
                pair_res = {
                    "pair_ok": 0,
                    "pair_idx": None,
                    "pair_score": 0.0,
                    "roi_ok": 0,
                    "roi_box": None,
                    "pair_boxes": None,
                    "miss_count": 0,
                }

            pair_found = bool(pair_res["pair_ok"])
            last_pair_idx = pair_res["pair_idx"]
            last_pair_score = float(pair_res["pair_score"])
            roi_hint_box = pair_res["roi_box"] if pair_res["roi_ok"] else None

            if active and pair_res["pair_ok"]:
                two_p_ctr += 1
                two_p_miss_ctr = 0
                recent_live_pair_ctr = recent_live_pair_hold
            else:
                if recent_live_pair_ctr > 0:
                    recent_live_pair_ctr -= 1

                if two_p_ctr > 0:
                    two_p_miss_ctr += 1
                    if two_p_miss_ctr >= two_p_grace_frames:
                        two_p_ctr = 0
                        two_p_miss_ctr = 0
                else:
                    two_p_ctr = 0
                    two_p_miss_ctr = 0

            raw_pair_ok = bool(pair_res["pair_ok"])
            if raw_pair_ok:
                last_pair_ok_ts = now

            effective_pair_ok = raw_pair_ok
            if (not effective_pair_ok) and last_pair_ok_ts > 0 and (now - last_pair_ok_ts) <= pair_hold_sec:
                effective_pair_ok = True

            display_live_pair = effective_pair_ok
            classify_live_pair = effective_pair_ok
            effective_live_pair = effective_pair_ok

            fight_active = False

            if stage3 is not None:
                if stage3_cooldown > 0:
                    stage3_cooldown -= 1

                if roi_hint_box is not None:
                    stable_roi_box = sanitize_box(roi_hint_box, base_frame.shape)
                    last_direct_roi_box = stable_roi_box
                    last_direct_roi_ts = now
                else:
                    if (
                        last_direct_roi_box is not None
                        and (now - last_direct_roi_ts) <= direct_roi_hold_sec
                    ):
                        stable_roi_box = last_direct_roi_box
                    else:
                        stable_roi_box = None
                        last_direct_roi_box = None

                roi_person_count = _count_persons_in_roi(
                    persons,
                    stable_roi_box,
                    min_iou=0.08,
                    min_center_inside=True,
                )

                if stable_roi_box is not None:
                    if raw_pair_ok or roi_person_count >= roi_person_min_count:
                        roi_invalid_ctr = 0
                    else:
                        roi_invalid_ctr += 1
                else:
                    roi_invalid_ctr += 1

                if roi_invalid_ctr >= roi_invalid_drop_frames:
                    stable_roi_box = None
                    last_direct_roi_box = None

                if stable_roi_box is not None:
                    roi_miss_frames = 0
                else:
                    roi_miss_frames += 1

                display_live_pair = effective_pair_ok
                classify_live_pair = effective_pair_ok
                effective_live_pair = effective_pair_ok

                view_s3 = None
                last_crop_live = False

                if stable_roi_box is not None:
                    stable_roi_box = sanitize_box(stable_roi_box, base_frame.shape)
                    view_s3 = crop_from_box(base_frame, stable_roi_box, out_size=320)
                    if view_s3 is not None:
                        last_valid_roi_frame = view_s3.copy()
                        last_crop_live = effective_live_pair

                allow_stale_crop = (
                    view_s3 is None
                    and last_valid_roi_frame is not None
                    and roi_miss_frames <= clip_soft_hold_frames
                    and roi_invalid_ctr < roi_invalid_drop_frames
                    and (effective_pair_ok or roi_person_count >= 1)
                )

                if allow_stale_crop:
                    view_s3 = last_valid_roi_frame.copy()
                    last_crop_live = effective_live_pair

                if pose is not None:
                    if active and (view_s3 is not None):
                        pose_ctr += 1
                        if pose_ctr % max(1, args.pose_stride) == 0:
                            pres = pose.check(view_s3)
                            last_pose_score = pres.score
                            last_pose_ok = pres.ok
                            last_pose_vis = pres.debug_frame
                        raw_pose_ready = bool(last_pose_ok)
                    else:
                        raw_pose_ready = False
                        last_pose_ok = False
                        last_pose_score = 0.0

                    if raw_pose_ready:
                        last_pose_ok_ts = now
                        last_pose_trigger_ts = now
                        pose_missing_since = None
                    else:
                        if pose_missing_since is None:
                            pose_missing_since = now

                    effective_pose_ready = raw_pose_ready
                    if not effective_pose_ready and last_pose_ok_ts > 0 and (now - last_pose_ok_ts) <= pose_hold_sec:
                        effective_pose_ready = True
                else:
                    effective_pose_ready = True
                    raw_pose_ready = True

                roi_available = (stable_roi_box is not None) and (view_s3 is not None)

                pose_capture_active = False
                if pose is None:
                    pose_capture_active = True
                elif last_pose_trigger_ts > 0 and (now - last_pose_trigger_ts) <= pose_trigger_hold_sec:
                    pose_capture_active = True

                hard_reset = False
                if not active:
                    hard_reset = True
                if roi_miss_frames > clip_reset_frames:
                    hard_reset = True
                if roi_invalid_ctr >= roi_invalid_drop_frames + 3:
                    hard_reset = True
                if pose is not None and pose_missing_since is not None and (now - pose_missing_since) > pose_reset_sec:
                    hard_reset = True

                if hard_reset:
                    clip = []
                    fight_on_ctr = 0
                    fight_off_ctr += 1
                    last_fight_prob = 0.0
                    last_pose_trigger_ts = 0.0

                    if not active or roi_invalid_ctr >= roi_invalid_drop_frames + 3:
                        stable_roi_box = None
                        pair_roi.reset()
                        last_direct_roi_box = None
                        last_direct_roi_ts = 0.0
                        last_valid_roi_frame = None
                        roi_miss_frames = 0
                        roi_invalid_ctr = 0
                        two_p_ctr = 0
                        two_p_miss_ctr = 0
                        recent_live_pair_ctr = 0
                        last_crop_live = False
                        last_pair_ok_ts = 0.0

                    if pose is not None and not active:
                        pose.reset()
                        pose_missing_since = None
                        last_pose_ok_ts = 0.0
                        last_pose_ok = False
                        last_pose_score = 0.0

                    if fight_state and fight_off_ctr >= fight_off_need:
                        fight_state = False
                else:
                    should_append_clip = (
                        roi_available
                        and pose_capture_active
                        and effective_live_pair
                        and (roi_invalid_ctr < roi_invalid_drop_frames)
                    )

                    if should_append_clip:
                        clip.append(view_s3)
                    elif roi_available and (not pose_capture_active):
                        if len(clip) > 0:
                            clip = clip[-max(0, stage3.clip_len // 2):]

                    if len(clip) > stage3.clip_len:
                        clip = clip[-stage3.clip_len:]

                can_classify = (
                    active
                    and classify_live_pair
                    and roi_available
                    and pose_capture_active
                    and (two_p_ctr >= args.min_2p_frames)
                    and (len(clip) >= stage3.clip_len)
                    and (stage3_cooldown == 0)
                    and (roi_invalid_ctr < roi_invalid_drop_frames)
                )

                if can_classify:
                    clip_path = clip_debug_dir / f"clip_{clip_save_idx:04d}.mp4"
                    save_clip_mp4(clip, str(clip_path), fps=16.0)

                    last_fight_prob = stage3.infer(clip)

                    print(
                        f"[STAGE3] clip={clip_path.name} "
                        f"fight_prob={last_fight_prob:.4f} "
                        f"thr={args.fight_thr:.2f} "
                        f"two_p_ctr={two_p_ctr} "
                        f"clip_len={len(clip)} "
                        f"pair_idx={last_pair_idx} "
                        f"pair_score={last_pair_score:.4f} "
                        f"roi_persons={roi_person_count} "
                        f"roi_invalid_ctr={roi_invalid_ctr} "
                        f"pose_raw={int(raw_pose_ready) if pose is not None else -1} "
                        f"pose_eff={int(effective_pose_ready) if pose is not None else -1} "
                        f"pose_capture={int(pose_capture_active) if pose is not None else -1} "
                        f"pose_score={last_pose_score:.3f}"
                    )

                    clip_save_idx += 1
                    stage3_cooldown = max(1, args.stage3_stride)

                    if last_fight_prob >= args.fight_thr:
                        fight_on_ctr += 1
                        fight_off_ctr = 0
                    else:
                        fight_off_ctr += 1
                        fight_on_ctr = 0

                    if (not fight_state) and fight_on_ctr >= fight_on_need:
                        fight_state = True

                    if fight_state and fight_off_ctr >= fight_off_need:
                        fight_state = False

                fight_active = fight_state

            if last_pair_idx is not None and len(persons) >= 2:
                i, j = last_pair_idx
                if i < len(persons) and j < len(persons):
                    _, (ax1, ay1, ax2, ay2) = persons[i]
                    _, (bx1, by1, bx2, by2) = persons[j]
                    cv2.rectangle(view, (ax1, ay1), (ax2, ay2), (0, 0, 255), 2)
                    cv2.rectangle(view, (bx1, by1), (bx2, by2), (0, 0, 255), 2)

            if stable_roi_box is not None:
                ux1, uy1, ux2, uy2 = stable_roi_box
                roi_color = (0, 255, 255) if effective_live_pair else (0, 165, 255)
                cv2.rectangle(view, (ux1, uy1), (ux2, uy2), roi_color, 2)

            pose_miss_sec = 0.0 if pose_missing_since is None else (now - pose_missing_since)
            pose_eff = last_pose_ok
            if pose is not None and not last_pose_ok and last_pose_ok_ts > 0 and (now - last_pose_ok_ts) <= pose_hold_sec:
                pose_eff = True

            pose_vis_to_show = None
            if pose is not None and last_pose_vis is not None:
                pose_vis_to_show = last_pose_vis.copy()
                if last_crop_live:
                    tag = "LIVE"
                    tag_color = (0, 255, 0)
                    cv2.putText(
                        pose_vis_to_show,
                        tag,
                        (10, 28),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.8,
                        tag_color,
                        2,
                    )

            debug_ctr += 1
            if debug_ctr % 15 == 0:
                print(
                    f"[PIPE] motion_active={int(active)} "
                    f"yolo_ran={int(yolo_ran)} "
                    f"persons_det={len(det_persons)} "
                    f"persons={len(persons)} "
                    f"conf={conf_list} "
                    f"pair_ok={int(pair_found)} "
                    f"pair_idx={last_pair_idx} "
                    f"pair_score={last_pair_score:.4f} "
                    f"pair_eff={int(effective_pair_ok)} "
                    f"two_p_ctr={two_p_ctr} "
                    f"roi_ok={int(stable_roi_box is not None)} "
                    f"roi_persons={roi_person_count} "
                    f"roi_invalid_ctr={roi_invalid_ctr} "
                    f"roi_miss_frames={roi_miss_frames} "
                    f"pose_ok={int(last_pose_ok) if pose is not None else -1} "
                    f"pose_eff={int(pose_eff) if pose is not None else -1} "
                    f"raw_pose_ready={int(raw_pose_ready) if pose is not None else -1} "
                    f"pose_capture={int(pose_capture_active) if pose is not None else -1} "
                    f"append_clip={int(should_append_clip) if stage3 is not None else -1} "
                    f"pose_score={last_pose_score:.3f} "
                    f"pose_miss_sec={pose_miss_sec:.2f} "
                    f"clip_len={len(clip)} "
                    f"cooldown={stage3_cooldown} "
                    f"stage3_ready={int(can_classify)} "
                    f"fight_prob={last_fight_prob:.4f} "
                    f"fight_state={int(fight_state)}"
                )

            lines = build_debug_lines(
                fps=fps,
                motion_score=score,
                persons_count=len(persons),
                pose_eff=pose_eff,
                pose_score=last_pose_score,
                last_fight_prob=last_fight_prob,
                fight_active=fight_active,
                clip_len=len(clip),
                pose_enabled=(pose is not None),
            )
            display_view = compose_debug_view(view, lines, panel_width=440)

            if args.show:
                cv2.imshow("fight_live", display_view)
                if pose is not None and pose_vis_to_show is not None:
                    cv2.imshow("pose_roi", pose_vis_to_show)

                k = cv2.waitKey(1) & 0xFF
                if k == 27 or k == ord("q"):
                    break
            else:
                time.sleep(0.001)

    finally:
        motion.close()
        cap.release()
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass


if __name__ == "__main__":
    main()