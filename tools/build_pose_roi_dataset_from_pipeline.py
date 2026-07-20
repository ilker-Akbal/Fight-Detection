from __future__ import annotations
import sys

"""
build_pose_roi_dataset_from_pipeline.py

Full video klasörlerinden, mevcut Fight-Detection pipeline'ına en yakın şekilde
Stage3 eğitim dataseti üretir.

Desteklenen input yapıları:

1) Splitli yapı:
  input-root/
    train/Fight/*.mp4
    train/NonFight/*.mp4
    val/Fight/*.mp4
    val/NonFight/*.mp4

2) Split olmayan Real Life Violence Dataset yapısı:
  input-root/
    Violence/*.mp4
    NonViolence/*.mp4

Bu script 2. yapıda otomatik train/val split üretir.

Output her zaman:
  output-root/
    train/Fight/*.mp4
    train/NonFight/*.mp4
    val/Fight/*.mp4
    val/NonFight/*.mp4
    manifest.csv
    summary.json
    debug/*.jpg
"""

import argparse
import csv
import json
import math
import random
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Deque, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import cv2
import numpy as np
import yaml
from tqdm.auto import tqdm

from fight.motion.src.core.config import load_config as load_motion_config
from fight.motion.src.service.motion_service import MotionRunner
from fight.pipeline.adapters import YoloAdapter
from fight.pipeline.clip_buffer import save_clip_mp4
from fight.pipeline.pair_selector import LivePairRoiController
from fight.pipeline.person_stabilizer import TemporalPersonStabilizer
from fight.pipeline.utils import crop_from_box, sanitize_box
from fight.pose.src.pose_adapter import PoseAdapter
from fight.pose.src.pose_gate import PoseGate


VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
CLASS_NAMES = ("Fight", "NonFight")

CLASS_ALIASES = {
    "fight": "Fight",
    "fights": "Fight",
    "violence": "Fight",
    "violent": "Fight",
    "kavga": "Fight",
    "nonfight": "NonFight",
    "nonfights": "NonFight",
    "nonviolence": "NonFight",
    "nonviolent": "NonFight",
    "normal": "NonFight",
    "nofight": "NonFight",
}


@dataclass
class ActiveEvent:
    frames: list
    pose_scores: list[float]
    positive_hits: int
    start_idx: int
    end_idx: int
    start_sec: float
    end_sec: float
    close_reason: str = ""

    @property
    def pose_mean(self) -> float:
        return float(sum(self.pose_scores) / max(1, len(self.pose_scores)))

    @property
    def pose_max(self) -> float:
        return float(max(self.pose_scores)) if self.pose_scores else 0.0


def seed_everything(seed: int) -> None:
    random.seed(int(seed))
    np.random.seed(int(seed))


def _norm_name(name: str) -> str:
    return (
        str(name)
        .strip()
        .lower()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )


def canonical_class_name(name: str) -> Optional[str]:
    return CLASS_ALIASES.get(_norm_name(name))


def list_class_videos(class_dir: Path) -> list[Path]:
    if not class_dir.exists():
        return []
    out = []
    for p in class_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            out.append(p)
    out.sort(key=lambda x: str(x))
    return out


def find_class_dirs(root: Path) -> dict[str, Path]:
    """
    root altında class klasörlerini bulur.
    Örn:
      root/Violence -> Fight
      root/NonViolence -> NonFight
      root/Fight -> Fight
      root/NonFight -> NonFight
    """
    found: dict[str, Path] = {}

    if not root.exists():
        return found

    for p in root.iterdir():
        if not p.is_dir():
            continue

        cls = canonical_class_name(p.name)
        if cls in CLASS_NAMES and cls not in found:
            found[cls] = p

    return found


def iter_videos_split_layout(input_root: Path) -> list[tuple[Path, str, str]]:
    items: list[tuple[Path, str, str]] = []

    for split in ("train", "val"):
        split_dir = input_root / split
        found = find_class_dirs(split_dir)

        for cls in CLASS_NAMES:
            d = found.get(cls)
            if d is None:
                print(f"[WARN] Klasör yok: {split_dir / cls}")
                continue

            for p in list_class_videos(d):
                items.append((p, split, cls))

    items.sort(key=lambda x: str(x[0]))
    return items


def iter_videos_flat_layout(input_root: Path, val_ratio: float, seed: int) -> list[tuple[Path, str, str]]:
    """
    Real Life Violence Dataset gibi:
      input-root/Violence
      input-root/NonViolence

    -> deterministic train/val split.
    """
    found = find_class_dirs(input_root)
    rng = random.Random(int(seed))

    items: list[tuple[Path, str, str]] = []

    for cls in CLASS_NAMES:
        d = found.get(cls)
        if d is None:
            print(f"[WARN] Flat class klasörü yok: {input_root / cls}")
            continue

        vids = list_class_videos(d)
        rng.shuffle(vids)

        n = len(vids)
        if n == 0:
            continue

        val_count = int(round(n * float(val_ratio)))
        val_count = max(1, val_count) if n >= 5 else max(0, val_count)

        val_set = set(vids[:val_count])

        for p in vids:
            split = "val" if p in val_set else "train"
            items.append((p, split, cls))

    items.sort(key=lambda x: (x[1], x[2], str(x[0])))
    return items


def detect_input_layout(input_root: Path) -> str:
    has_train_val = (input_root / "train").exists() or (input_root / "val").exists()
    if has_train_val:
        return "split"

    found = find_class_dirs(input_root)
    if "Fight" in found or "NonFight" in found:
        return "flat"

    return "unknown"


def iter_videos(input_root: Path, layout: str, val_ratio: float, seed: int) -> list[tuple[Path, str, str]]:
    if layout == "auto":
        layout = detect_input_layout(input_root)

    if layout == "split":
        return iter_videos_split_layout(input_root)

    if layout == "flat":
        return iter_videos_flat_layout(input_root, val_ratio=val_ratio, seed=seed)

    raise RuntimeError(
        f"Input layout anlaşılamadı: {input_root}\n"
        "Desteklenen yapılar:\n"
        "  split: train/Fight, train/NonFight, val/Fight, val/NonFight\n"
        "  flat : Violence, NonViolence"
    )


def make_loose_pose_config(
    src_pose_config: str,
    dst_dir: Path,
    pose_weights: str = "",
    device: str = "0",
    pose_conf: float = 0.12,
    min_kpt_conf: float = 0.20,
    min_valid_kpts: int = 5,
    min_interaction_score: float = 0.20,
    require_contact_like: bool = False,
    temporal_window: int = 5,
    temporal_need_positive: int = 1,
    temporal_min_mean_score: float = 0.18,
    temporal_peak_score_thr: float = 0.35,
    temporal_min_consecutive: int = 1,
) -> Path:
    with open(src_pose_config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    cfg.setdefault("model", {})
    cfg.setdefault("filter", {})
    cfg.setdefault("interaction", {})
    cfg.setdefault("temporal", {})
    cfg.setdefault("debug", {})

    if pose_weights:
        cfg["model"]["weights"] = str(pose_weights)
    cfg["model"]["device"] = int(device) if str(device).isdigit() else device
    cfg["model"]["conf"] = float(pose_conf)
    cfg["model"]["verbose"] = False

    cfg["filter"]["min_persons"] = 2
    cfg["filter"]["min_kpt_conf"] = float(min_kpt_conf)
    cfg["filter"]["min_valid_kpts"] = int(min_valid_kpts)

    x = cfg["interaction"]
    x["max_center_dist_norm"] = float(x.get("max_center_dist_norm", 0.40)) * 1.30
    x["wrist_dist_norm"] = max(float(x.get("wrist_dist_norm", 0.18)), 0.30)
    x["upper_body_dist_norm"] = max(float(x.get("upper_body_dist_norm", 0.22)), 0.34)
    x["torso_dist_norm"] = max(float(x.get("torso_dist_norm", 0.24)), 0.34)
    x["wrist_to_head_norm"] = max(float(x.get("wrist_to_head_norm", 0.16)), 0.26)
    x["wrist_to_upper_torso_norm"] = max(float(x.get("wrist_to_upper_torso_norm", 0.18)), 0.28)
    x["wrist_to_torso_norm"] = max(float(x.get("wrist_to_torso_norm", 0.20)), 0.30)
    x["min_arm_extension"] = min(float(x.get("min_arm_extension", 0.58)), 0.45)
    x["min_arm_alignment"] = min(float(x.get("min_arm_alignment", 0.50)), 0.38)
    x["strike_torso_dist_norm"] = max(float(x.get("strike_torso_dist_norm", 0.36)), 0.44)
    x["grapple_torso_dist_norm"] = max(float(x.get("grapple_torso_dist_norm", 0.20)), 0.30)
    x["grapple_upper_body_dist_norm"] = max(float(x.get("grapple_upper_body_dist_norm", 0.18)), 0.28)
    x["grapple_wrist_dist_norm"] = max(float(x.get("grapple_wrist_dist_norm", 0.16)), 0.26)
    x["min_interaction_score"] = float(min_interaction_score)
    x["require_contact_like"] = bool(require_contact_like)

    cfg["temporal"]["window_size"] = int(temporal_window)
    cfg["temporal"]["need_positive"] = int(temporal_need_positive)
    cfg["temporal"]["min_mean_score"] = float(temporal_min_mean_score)
    cfg["temporal"]["peak_score_thr"] = float(temporal_peak_score_thr)
    cfg["temporal"]["min_consecutive"] = int(temporal_min_consecutive)

    cfg["debug"]["draw"] = False

    dst_dir.mkdir(parents=True, exist_ok=True)
    out = dst_dir / "pose_loose_for_dataset.yaml"
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)

    return out


def frame_time_sec(frame_idx_sampled: int, sample_fps: float) -> float:
    return float(frame_idx_sampled) / max(1e-6, float(sample_fps))


def pad_or_split_event(ev: ActiveEvent, clip_frames: int, stride_frames: int, min_clip_frames: int) -> list[ActiveEvent]:
    frames = ev.frames
    n = len(frames)
    clip_frames = int(clip_frames)
    stride_frames = max(1, int(stride_frames))
    min_clip_frames = int(min_clip_frames)

    if n < min_clip_frames:
        return []

    out: list[ActiveEvent] = []

    if n <= clip_frames:
        new_frames = list(frames)
        while len(new_frames) < clip_frames:
            new_frames.append(new_frames[-1].copy())

        out.append(
            ActiveEvent(
                frames=new_frames,
                pose_scores=list(ev.pose_scores),
                positive_hits=int(ev.positive_hits),
                start_idx=ev.start_idx,
                end_idx=ev.end_idx,
                start_sec=ev.start_sec,
                end_sec=ev.end_sec,
                close_reason=ev.close_reason,
            )
        )
        return out

    starts = list(range(0, n - clip_frames + 1, stride_frames))
    if starts and starts[-1] != n - clip_frames:
        starts.append(n - clip_frames)

    for s in starts:
        e = s + clip_frames
        sub_scores = ev.pose_scores[s:e] if len(ev.pose_scores) >= e else ev.pose_scores
        sub_pos = sum(1 for x in sub_scores if x > 0.0)

        out.append(
            ActiveEvent(
                frames=[f.copy() for f in frames[s:e]],
                pose_scores=list(sub_scores),
                positive_hits=int(sub_pos),
                start_idx=ev.start_idx + s,
                end_idx=ev.start_idx + e - 1,
                start_sec=ev.start_sec,
                end_sec=ev.end_sec,
                close_reason=ev.close_reason,
            )
        )

    return out


def save_debug_jpg(frames: list, path: Path, max_frames: int = 12) -> None:
    if not frames:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    pick = frames[:max_frames]

    thumbs = []
    for f in pick:
        try:
            thumbs.append(cv2.resize(f, (160, 160)))
        except Exception:
            pass

    if not thumbs:
        return

    cols = 4
    rows = int(math.ceil(len(thumbs) / cols))
    canvas = np.full((rows * 160, cols * 160, 3), 20, dtype=np.uint8)

    for i, t in enumerate(thumbs):
        r = i // cols
        c = i % cols
        canvas[r * 160:(r + 1) * 160, c * 160:(c + 1) * 160] = t

    cv2.imwrite(str(path), canvas)


class PipelineLikeExtractor:
    def __init__(self, args):
        self.args = args

        self.yolo = YoloAdapter(args.yolo_config, args.yolo_weights)
        self.pose_cfg_path = make_loose_pose_config(
            src_pose_config=args.pose_config,
            dst_dir=Path(args.output_root) / "_extractor_cfg",
            pose_weights=args.pose_weights,
            device=args.device,
            pose_conf=args.pose_conf,
            min_kpt_conf=args.pose_min_kpt_conf,
            min_valid_kpts=args.pose_min_valid_kpts,
            min_interaction_score=args.pose_min_interaction_score,
            require_contact_like=args.pose_require_contact_like,
            temporal_window=args.pose_temporal_window,
            temporal_need_positive=args.pose_temporal_need_positive,
            temporal_min_mean_score=args.pose_temporal_min_mean_score,
            temporal_peak_score_thr=args.pose_temporal_peak_score_thr,
            temporal_min_consecutive=args.pose_temporal_min_consecutive,
        )
        self.pose = PoseAdapter(str(self.pose_cfg_path))

        self.motion_runner = None
        if bool(args.use_motion):
            self.motion_runner = MotionRunner(load_motion_config(Path(args.motion_config)))

    def new_stabilizer(self) -> TemporalPersonStabilizer:
        return TemporalPersonStabilizer(
            max_age=8,
            min_hits=1,
            iou_match_thr=0.22,
            conf_alpha=0.65,
            max_tracks=12,
        )

    def new_pair_roi(self) -> LivePairRoiController:
        return LivePairRoiController(
            enter_score=0.50,
            keep_score=0.34,
            keep_frames=18,
            min_hits_to_activate=1,
            candidate_confirm_frames=1,
            pair_identity_iou_thr=0.30,
            switch_margin=0.04,
            roi_expand_x=1.25,
            roi_expand_y=1.18,
            debug=False,
        )

    def new_pose_gate(self) -> PoseGate:
        return PoseGate(
            window_size=int(self.args.pose_temporal_window),
            need_positive=int(self.args.pose_temporal_need_positive),
            min_mean_score=float(self.args.pose_temporal_min_mean_score),
            peak_score_thr=float(self.args.pose_temporal_peak_score_thr),
            min_consecutive=int(self.args.pose_temporal_min_consecutive),
        )

    def process_video(self, video_path: Path, split: str, cls: str, out_root: Path, manifest_rows: list[dict]) -> int:
        cap = cv2.VideoCapture(str(video_path), cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"Video açılamadı: {video_path}")

        src_fps = cap.get(cv2.CAP_PROP_FPS)
        if not src_fps or src_fps <= 1e-3:
            src_fps = 30.0

        sample_step = max(1, int(round(float(src_fps) / float(self.args.sample_fps))))

        stabilizer = self.new_stabilizer()
        pair_roi = self.new_pair_roi()
        pose_gate = self.new_pose_gate()

        active: Optional[ActiveEvent] = None
        prebuffer: Deque = deque(maxlen=int(self.args.prebuffer_frames))
        events: list[ActiveEvent] = []

        frame_idx = -1
        sampled_idx = -1
        yolo_ctr = 0
        pose_ctr = 0

        last_pose_score = 0.0
        last_pose_positive = False
        last_pose_hit_sampled_idx = -10_000
        recent_persons = []

        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break

            frame_idx += 1
            if frame_idx % sample_step != 0:
                continue

            sampled_idx += 1
            now_sec = frame_time_sec(sampled_idx, self.args.sample_fps)

            if self.motion_runner is not None:
                try:
                    out = self.motion_runner.step(now_sec, frame)
                    if out is not None and not bool(out.pass_frame):
                        if active is not None:
                            active.close_reason = "motion_off"
                            events.append(active)
                            active = None
                        stabilizer.reset()
                        pair_roi.reset()
                        pose_gate.reset()
                        prebuffer.clear()
                        last_pose_score = 0.0
                        last_pose_positive = False
                        last_pose_hit_sampled_idx = -10_000
                        continue
                    base_frame = frame if out is None or out.frame_resized is None else out.frame_resized
                except Exception:
                    base_frame = frame
            else:
                base_frame = frame

            yolo_ctr += 1
            if yolo_ctr % max(1, int(self.args.yolo_stride)) == 0:
                dets = self.yolo.detect_persons(base_frame)
                dets = [(c, box) for c, box in dets if float(c) >= float(self.args.person_conf)]
                recent_persons = stabilizer.update(dets)
            else:
                recent_persons = stabilizer.predict_only()

            if len(recent_persons) < int(self.args.min_persons_for_pose):
                if active is not None:
                    gap = sampled_idx - active.end_idx
                    if gap > int(self.args.event_close_grace_frames):
                        active.close_reason = "no_persons_for_pose"
                        events.append(active)
                        active = None
                continue

            pair_res = pair_roi.update(recent_persons, base_frame.shape)
            roi_box = pair_res.get("roi_box") if pair_res.get("roi_ok") else None
            roi_box = sanitize_box(roi_box, base_frame.shape) if roi_box is not None else None

            if roi_box is None:
                if active is not None:
                    gap = sampled_idx - active.end_idx
                    if gap > int(self.args.event_close_grace_frames):
                        active.close_reason = "pair_roi_missing"
                        events.append(active)
                        active = None
                continue

            roi_bgr = crop_from_box(base_frame, roi_box, out_size=int(self.args.roi_size))
            if roi_bgr is None:
                if active is not None:
                    gap = sampled_idx - active.end_idx
                    if gap > int(self.args.event_close_grace_frames):
                        active.close_reason = "roi_crop_failed"
                        events.append(active)
                        active = None
                continue

            prebuffer.append(roi_bgr.copy())

            pose_ctr += 1
            if pose_ctr % max(1, int(self.args.pose_stride)) == 0:
                pres = self.pose.evaluate(roi_bgr)
                gate = pose_gate.update(float(pres.score), bool(pres.ok))
                last_pose_score = float(pres.score)
                last_pose_positive = bool(gate.pose_ok)

                if last_pose_positive:
                    last_pose_hit_sampled_idx = sampled_idx

            pose_positive = last_pose_positive or (
                (sampled_idx - last_pose_hit_sampled_idx) <= int(self.args.pose_hold_frames)
            )
            pose_score = float(last_pose_score)

            if pose_positive:
                if active is None:
                    seed_frames = [f.copy() for f in list(prebuffer)[-int(self.args.prebuffer_frames):]]
                    active = ActiveEvent(
                        frames=seed_frames,
                        pose_scores=[pose_score] * max(1, len(seed_frames)),
                        positive_hits=1,
                        start_idx=max(0, sampled_idx - len(seed_frames) + 1),
                        end_idx=sampled_idx,
                        start_sec=frame_time_sec(max(0, sampled_idx - len(seed_frames) + 1), self.args.sample_fps),
                        end_sec=now_sec,
                    )

                active.frames.append(roi_bgr.copy())
                active.pose_scores.append(pose_score)
                active.positive_hits += 1
                active.end_idx = sampled_idx
                active.end_sec = now_sec

                if len(active.frames) >= int(self.args.max_event_frames):
                    active.close_reason = "max_event_frames"
                    events.append(active)
                    active = None
                    pose_gate.reset()
                    last_pose_positive = False
            else:
                if active is not None:
                    gap = sampled_idx - active.end_idx
                    if gap <= int(self.args.event_close_grace_frames):
                        active.frames.append(roi_bgr.copy())
                        active.pose_scores.append(pose_score)
                        active.end_idx = sampled_idx
                        active.end_sec = now_sec
                    else:
                        active.close_reason = "pose_timeout"
                        events.append(active)
                        active = None

        cap.release()

        if active is not None:
            active.close_reason = "eof"
            events.append(active)

        made = self._save_events(video_path, split, cls, events, out_root, manifest_rows)

        if made == 0 and bool(self.args.fallback_pair_roi):
            made = self._fallback_pair_roi_video(video_path, split, cls, out_root, manifest_rows)

        return made

    def _save_events(self, video_path: Path, split: str, cls: str, events: list[ActiveEvent], out_root: Path, manifest_rows: list[dict]) -> int:
        out_dir = out_root / split / cls
        out_dir.mkdir(parents=True, exist_ok=True)

        made = 0
        base = video_path.stem

        for ev_i, ev in enumerate(events):
            if len(ev.frames) < int(self.args.min_event_frames):
                continue
            if ev.positive_hits < int(self.args.min_positive_hits):
                continue
            if ev.pose_max < float(self.args.min_pose_max):
                continue
            if ev.pose_mean < float(self.args.min_pose_mean):
                continue

            sub_events = pad_or_split_event(
                ev,
                clip_frames=int(self.args.clip_frames),
                stride_frames=int(self.args.stride_frames),
                min_clip_frames=int(self.args.min_event_frames),
            )

            for sub_i, sub in enumerate(sub_events):
                if int(self.args.max_clips_per_video) > 0 and made >= int(self.args.max_clips_per_video):
                    break

                out_name = f"{base}__pose_roi_e{ev_i:03d}_{sub_i:03d}.mp4"
                out_path = out_dir / out_name

                if out_path.exists() and not bool(self.args.overwrite):
                    made += 1
                    continue

                ok = save_clip_mp4(sub.frames, str(out_path), fps=float(self.args.sample_fps))
                if not ok and not out_path.exists():
                    continue

                if made < int(self.args.debug_images_per_video):
                    dbg_path = out_root / "debug" / split / cls / f"{base}__pose_roi_e{ev_i:03d}_{sub_i:03d}.jpg"
                    save_debug_jpg(sub.frames, dbg_path)

                manifest_rows.append(
                    {
                        "split": split,
                        "class_name": cls,
                        "label": 1 if cls == "Fight" else 0,
                        "src_video": str(video_path),
                        "out_clip": str(out_path),
                        "clip_idx": made,
                        "frames": len(sub.frames),
                        "src_start_sec": round(float(sub.start_sec), 3),
                        "src_end_sec": round(float(sub.end_sec), 3),
                        "pose_score_mean": round(float(sub.pose_mean), 6),
                        "pose_score_max": round(float(sub.pose_max), 6),
                        "positive_hits": int(sub.positive_hits),
                        "close_reason": sub.close_reason,
                        "extractor": "pose_roi_pipeline_like",
                    }
                )

                made += 1

        return made

    def _fallback_pair_roi_video(self, video_path: Path, split: str, cls: str, out_root: Path, manifest_rows: list[dict]) -> int:
        if cls == "NonFight" and not bool(self.args.fallback_for_nonfight):
            return 0

        cap = cv2.VideoCapture(str(video_path), cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return 0

        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 1e-3:
            fps = 30.0
        step = max(1, int(round(float(fps) / float(self.args.sample_fps))))

        stabilizer = self.new_stabilizer()
        pair_roi = self.new_pair_roi()
        frames: list = []

        frame_idx = -1
        yolo_ctr = 0
        recent_persons = []

        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break

            frame_idx += 1
            if frame_idx % step != 0:
                continue

            yolo_ctr += 1
            if yolo_ctr % max(1, int(self.args.yolo_stride)) == 0:
                dets = self.yolo.detect_persons(frame)
                dets = [(c, box) for c, box in dets if float(c) >= float(self.args.person_conf)]
                recent_persons = stabilizer.update(dets)
            else:
                recent_persons = stabilizer.predict_only()

            if len(recent_persons) < int(self.args.min_persons_for_pose):
                continue

            pair_res = pair_roi.update(recent_persons, frame.shape)
            roi_box = pair_res.get("roi_box") if pair_res.get("roi_ok") else None
            roi_box = sanitize_box(roi_box, frame.shape) if roi_box is not None else None
            if roi_box is None:
                continue

            roi = crop_from_box(frame, roi_box, out_size=int(self.args.roi_size))
            if roi is not None:
                frames.append(roi.copy())

        cap.release()

        if len(frames) < int(self.args.min_event_frames):
            return 0

        ev = ActiveEvent(
            frames=frames,
            pose_scores=[0.0] * len(frames),
            positive_hits=0,
            start_idx=0,
            end_idx=len(frames) - 1,
            start_sec=0.0,
            end_sec=float(len(frames)) / max(1e-6, float(self.args.sample_fps)),
            close_reason="fallback_pair_roi",
        )

        old_min_pose_mean = self.args.min_pose_mean
        old_min_pose_max = self.args.min_pose_max
        old_min_positive_hits = self.args.min_positive_hits
        try:
            self.args.min_pose_mean = 0.0
            self.args.min_pose_max = 0.0
            self.args.min_positive_hits = 0
            return self._save_events(video_path, split, cls, [ev], out_root, manifest_rows)
        finally:
            self.args.min_pose_mean = old_min_pose_mean
            self.args.min_pose_max = old_min_pose_max
            self.args.min_positive_hits = old_min_positive_hits


def write_manifest(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "split",
        "class_name",
        "label",
        "src_video",
        "out_clip",
        "clip_idx",
        "frames",
        "src_start_sec",
        "src_end_sec",
        "pose_score_mean",
        "pose_score_max",
        "positive_hits",
        "close_reason",
        "extractor",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def parse_args():
    ap = argparse.ArgumentParser()

    ap.add_argument("--input-root", type=str, required=True)
    ap.add_argument("--output-root", type=str, required=True)

    ap.add_argument("--input-layout", type=str, default="auto", choices=["auto", "split", "flat"])
    ap.add_argument("--val-ratio", type=float, default=0.20)

    ap.add_argument("--motion-config", type=str, default="fight/motion/configs/motion.yaml")
    ap.add_argument("--yolo-config", type=str, default="fight/yolo/configs/yolo.yaml")
    ap.add_argument("--yolo-weights", type=str, default="fight/yolo11n.pt")
    ap.add_argument("--pose-config", type=str, default="fight/pose/configs/pose.yaml")
    ap.add_argument("--pose-weights", type=str, default="")

    ap.add_argument("--device", type=str, default="0")
    ap.add_argument("--use-motion", action="store_true")

    ap.add_argument("--sample-fps", type=float, default=16.0)
    ap.add_argument("--person-conf", type=float, default=0.22)
    ap.add_argument("--yolo-stride", type=int, default=2)
    ap.add_argument("--pose-stride", type=int, default=2)
    ap.add_argument("--roi-size", type=int, default=320)
    ap.add_argument("--min-persons-for-pose", type=int, default=2)

    ap.add_argument("--prebuffer-frames", type=int, default=24)
    ap.add_argument("--pose-hold-frames", type=int, default=10)
    ap.add_argument("--event-close-grace-frames", type=int, default=14)
    ap.add_argument("--max-event-frames", type=int, default=128)

    ap.add_argument("--clip-frames", type=int, default=64)
    ap.add_argument("--stride-frames", type=int, default=32)
    ap.add_argument("--min-event-frames", type=int, default=20)
    ap.add_argument("--min-positive-hits", type=int, default=3)
    ap.add_argument("--min-pose-mean", type=float, default=0.08)
    ap.add_argument("--min-pose-max", type=float, default=0.18)

    ap.add_argument("--pose-conf", type=float, default=0.12)
    ap.add_argument("--pose-min-kpt-conf", type=float, default=0.20)
    ap.add_argument("--pose-min-valid-kpts", type=int, default=5)
    ap.add_argument("--pose-min-interaction-score", type=float, default=0.20)
    ap.add_argument("--pose-require-contact-like", action="store_true", default=False)
    ap.add_argument("--pose-temporal-window", type=int, default=5)
    ap.add_argument("--pose-temporal-need-positive", type=int, default=1)
    ap.add_argument("--pose-temporal-min-mean-score", type=float, default=0.18)
    ap.add_argument("--pose-temporal-peak-score-thr", type=float, default=0.35)
    ap.add_argument("--pose-temporal-min-consecutive", type=int, default=1)

    ap.add_argument("--max-clips-per-video", type=int, default=0)
    ap.add_argument("--debug-images-per-video", type=int, default=1)

    ap.add_argument("--fallback-pair-roi", action="store_true", default=True)
    ap.add_argument("--no-fallback-pair-roi", dest="fallback_pair_roi", action="store_false")
    ap.add_argument("--fallback-for-nonfight", action="store_true", default=False)

    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--seed", type=int, default=42)

    return ap.parse_args()


def main():
    args = parse_args()
    seed_everything(args.seed)

    input_root = Path(args.input_root).resolve()
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    layout = args.input_layout
    if layout == "auto":
        layout = detect_input_layout(input_root)

    print("=" * 100)
    print("PIPELINE-LIKE POSE ROI DATASET EXTRACTOR")
    print("input :", input_root)
    print("output:", output_root)
    print("layout:", layout)
    print("val_ratio:", args.val_ratio)
    print("pose cfg loose generated under:", output_root / "_extractor_cfg")
    print("=" * 100)

    items = iter_videos(
        input_root,
        layout=args.input_layout,
        val_ratio=float(args.val_ratio),
        seed=int(args.seed),
    )
    if not items:
        raise RuntimeError(f"Video bulunamadı: {input_root}")

    print("Input video sayıları:")
    counts = {}
    for _, split, cls in items:
        counts[(split, cls)] = counts.get((split, cls), 0) + 1
    for split in ("train", "val"):
        for cls in CLASS_NAMES:
            print(f"  {split}/{cls}: {counts.get((split, cls), 0)}")

    extractor = PipelineLikeExtractor(args)
    manifest_rows: list[dict] = []

    total = 0
    by_class = {"Fight": 0, "NonFight": 0}

    pbar = tqdm(items, desc=f"extract {input_root.name}")
    for video_path, split, cls in pbar:
        try:
            made = extractor.process_video(video_path, split, cls, output_root, manifest_rows)
            total += int(made)
            by_class[cls] += int(made)
            pbar.set_postfix(made=made, total=total, cls=cls, split=split)
        except Exception as exc:
            print(f"[WARN] failed {video_path}: {exc}")

    manifest_name = "manifest.csv"
    # Real Life Violence Dataset gibi datasetlerde çıktı isimlerini ayırmak faydalı.
    if input_root.name.lower().replace(" ", "_") not in {"data", "rwf-2000"}:
        manifest_name = f"manifest_{input_root.name.lower().replace(' ', '_')}.csv"

    write_manifest(output_root / manifest_name, manifest_rows)

    summary = {
        "input_root": str(input_root),
        "output_root": str(output_root),
        "layout": layout,
        "val_ratio": float(args.val_ratio),
        "input_video_counts": {f"{k[0]}/{k[1]}": v for k, v in counts.items()},
        "total_clips": total,
        "fight_clips": by_class["Fight"],
        "nonfight_clips": by_class["NonFight"],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "args": vars(args),
    }
    with open(output_root / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print("=" * 100)
    print("DONE")
    print("total clips:", total)
    print("Fight:", by_class["Fight"])
    print("NonFight:", by_class["NonFight"])
    print("manifest:", output_root / manifest_name)
    print("summary:", output_root / "summary.json")
    print("=" * 100)


if __name__ == "__main__":
    main()
