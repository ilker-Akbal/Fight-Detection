from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
import yaml
from ultralytics import YOLO

from fight.pipeline.roi_utils import make_square_pair_roi
from fight.pipeline.utils import resize_with_padding
from fight.pose.src.pose_utils import (
    compute_interaction_score,
    compute_pair_features,
    count_valid_kpts,
    select_best_pair_indices,
    top2_person_indices_by_area,
)


@dataclass
class PoseResult:
    ok: bool
    score: float
    num_persons: int
    hist_positive: int
    valid_a: int
    valid_b: int
    kp_conf_a: float
    kp_conf_b: float
    center_dist_norm: float
    wrist_dist_norm: float
    upper_body_dist_norm: float
    torso_dist_norm: float
    wrist_to_head_norm: float
    wrist_to_upper_torso_norm: float
    wrist_to_torso_norm: float
    arm_extension_max: float
    arm_alignment_max: float
    strike_like: bool
    grapple_like: bool
    debug_frame: Optional[np.ndarray]


class PoseAdapter:
    def __init__(self, cfg_path: str):
        cfgp = Path(cfg_path)
        with open(cfgp, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f) or {}

        mcfg = self.cfg.get("model", {})
        icfg = self.cfg.get("input", {})
        fcfg = self.cfg.get("filter", {})
        xcfg = self.cfg.get("interaction", {})
        dcfg = self.cfg.get("debug", {})

        self.weights = str(mcfg.get("weights", "yolo11s-pose.pt"))
        self.device = mcfg.get("device", 0)
        self.imgsz = int(mcfg.get("imgsz", 416))
        self.conf = float(mcfg.get("conf", 0.18))
        self.verbose = bool(mcfg.get("verbose", False))

        self.roi_size = int(icfg.get("roi_size", 320))
        self.pad_value = int(icfg.get("pad_value", 114))

        self.min_persons = int(fcfg.get("min_persons", 2))
        self.min_kpt_conf = float(fcfg.get("min_kpt_conf", 0.30))
        self.min_valid_kpts = int(fcfg.get("min_valid_kpts", 7))

        self.max_center_dist_norm = float(xcfg.get("max_center_dist_norm", 0.40))
        self.wrist_dist_norm_thr = float(xcfg.get("wrist_dist_norm", 0.18))
        self.upper_body_dist_norm_thr = float(xcfg.get("upper_body_dist_norm", 0.22))
        self.torso_dist_norm_thr = float(xcfg.get("torso_dist_norm", 0.24))

        self.wrist_to_head_thr = float(xcfg.get("wrist_to_head_norm", 0.16))
        self.wrist_to_upper_torso_thr = float(xcfg.get("wrist_to_upper_torso_norm", 0.18))
        self.wrist_to_torso_thr = float(xcfg.get("wrist_to_torso_norm", 0.20))

        self.min_arm_extension = float(xcfg.get("min_arm_extension", 0.58))
        self.min_arm_alignment = float(xcfg.get("min_arm_alignment", 0.50))
        self.min_interaction_score = float(xcfg.get("min_interaction_score", 0.34))
        self.require_contact_like = bool(xcfg.get("require_contact_like", True))

        self.strike_torso_dist_norm_thr = float(xcfg.get("strike_torso_dist_norm", 0.36))
        self.grapple_torso_dist_norm_thr = float(xcfg.get("grapple_torso_dist_norm", 0.20))
        self.grapple_upper_body_dist_norm_thr = float(xcfg.get("grapple_upper_body_dist_norm", 0.18))
        self.grapple_wrist_dist_norm_thr = float(xcfg.get("grapple_wrist_dist_norm", 0.16))

        self.draw_debug = bool(dcfg.get("draw", True))

        self.model = YOLO(self.weights)

    def _empty(self, debug_frame: Optional[np.ndarray] = None) -> PoseResult:
        return PoseResult(
            ok=False,
            score=0.0,
            num_persons=0,
            hist_positive=0,
            valid_a=0,
            valid_b=0,
            kp_conf_a=0.0,
            kp_conf_b=0.0,
            center_dist_norm=1e9,
            wrist_dist_norm=1e9,
            upper_body_dist_norm=1e9,
            torso_dist_norm=1e9,
            wrist_to_head_norm=1e9,
            wrist_to_upper_torso_norm=1e9,
            wrist_to_torso_norm=1e9,
            arm_extension_max=0.0,
            arm_alignment_max=0.0,
            strike_like=False,
            grapple_like=False,
            debug_frame=debug_frame,
        )

    def _prepare_roi(self, roi_bgr: np.ndarray) -> np.ndarray:
        return resize_with_padding(
            roi_bgr,
            out_size=self.roi_size,
            pad_value=self.pad_value,
            interpolation=cv2.INTER_LINEAR,
        )

    def _crop_xyxy(
        self,
        frame_bgr: np.ndarray,
        roi_xyxy: Tuple[int, int, int, int],
    ) -> Optional[np.ndarray]:
        if frame_bgr is None or frame_bgr.size == 0:
            return None

        H, W = frame_bgr.shape[:2]
        x1, y1, x2, y2 = roi_xyxy

        x1 = max(0, min(W - 1, int(x1)))
        y1 = max(0, min(H - 1, int(y1)))
        x2 = max(0, min(W, int(x2)))
        y2 = max(0, min(H, int(y2)))

        if x2 <= x1 or y2 <= y1:
            return None

        crop = frame_bgr[y1:y2, x1:x2]
        if crop is None or crop.size == 0:
            return None
        return crop

    def infer_roi(self, roi_bgr: np.ndarray) -> Dict[str, Any]:
        roi_resized = self._prepare_roi(roi_bgr)
        roi_rgb = cv2.cvtColor(roi_resized, cv2.COLOR_BGR2RGB)

        res = self.model.predict(
            source=roi_rgb,
            imgsz=self.imgsz,
            conf=self.conf,
            device=self.device,
            verbose=self.verbose,
        )[0]

        return {
            "resized_bgr": roi_resized,
            "result": res,
        }

    def evaluate_xyxy(
        self,
        frame_bgr: np.ndarray,
        roi_xyxy: Tuple[int, int, int, int],
        hist_positive: int = 0,
    ) -> PoseResult:
        roi_bgr = self._crop_xyxy(frame_bgr, roi_xyxy)
        if roi_bgr is None:
            return self._empty(debug_frame=None)
        return self.evaluate(roi_bgr, hist_positive=hist_positive)

    def evaluate_pair(
        self,
        frame_bgr: np.ndarray,
        box_a: np.ndarray,
        box_b: np.ndarray,
        hist_positive: int = 0,
        margin: float = 0.15,
    ) -> PoseResult:
        pair_roi = make_square_pair_roi(
            frame=frame_bgr,
            box_a=box_a,
            box_b=box_b,
            out_size=self.roi_size,
            margin=margin,
            pad_value=self.pad_value,
        )
        roi_bgr = pair_roi.get("roi_bgr", None)
        if roi_bgr is None or roi_bgr.size == 0:
            return self._empty(debug_frame=None)
        return self.evaluate(roi_bgr, hist_positive=hist_positive)

    def evaluate(self, roi_bgr: np.ndarray, hist_positive: int = 0) -> PoseResult:
        if roi_bgr is None or roi_bgr.size == 0:
            return self._empty(debug_frame=None)

        out = self.infer_roi(roi_bgr)
        roi_vis = out["resized_bgr"].copy()
        res = out["result"]

        if getattr(res, "boxes", None) is None or len(res.boxes) == 0:
            return self._empty(debug_frame=roi_vis)

        if getattr(res, "keypoints", None) is None:
            return self._empty(debug_frame=roi_vis)

        boxes_xyxy = res.boxes.xyxy.detach().cpu().numpy()
        kp_xy = res.keypoints.xy.detach().cpu().numpy()
        kp_conf = res.keypoints.conf.detach().cpu().numpy()

        h, w = roi_vis.shape[:2]
        idx2 = select_best_pair_indices(
            boxes_xyxy=boxes_xyxy,
            kp_xy=kp_xy,
            kp_conf=kp_conf,
            roi_w=w,
            roi_h=h,
            min_conf=self.min_kpt_conf,
            min_valid_kpts_select=max(4, self.min_valid_kpts // 2),
        )

        if len(idx2) < self.min_persons:
            idx2 = top2_person_indices_by_area(boxes_xyxy)

        if len(idx2) < self.min_persons:
            return self._empty(debug_frame=roi_vis)

        i0, i1 = idx2[0], idx2[1]
        a_xy = kp_xy[i0]
        b_xy = kp_xy[i1]
        a_conf = kp_conf[i0]
        b_conf = kp_conf[i1]

        valid_a = count_valid_kpts(a_conf, self.min_kpt_conf)
        valid_b = count_valid_kpts(b_conf, self.min_kpt_conf)
        kp_conf_a = float(np.mean(a_conf)) if len(a_conf) else 0.0
        kp_conf_b = float(np.mean(b_conf)) if len(b_conf) else 0.0

        if valid_a < self.min_valid_kpts or valid_b < self.min_valid_kpts:
            return PoseResult(
                ok=False,
                score=0.0,
                num_persons=len(boxes_xyxy),
                hist_positive=hist_positive,
                valid_a=valid_a,
                valid_b=valid_b,
                kp_conf_a=kp_conf_a,
                kp_conf_b=kp_conf_b,
                center_dist_norm=1e9,
                wrist_dist_norm=1e9,
                upper_body_dist_norm=1e9,
                torso_dist_norm=1e9,
                wrist_to_head_norm=1e9,
                wrist_to_upper_torso_norm=1e9,
                wrist_to_torso_norm=1e9,
                arm_extension_max=0.0,
                arm_alignment_max=0.0,
                strike_like=False,
                grapple_like=False,
                debug_frame=roi_vis,
            )

        feats = compute_pair_features(
            kpts_a_xy=a_xy,
            kpts_a_conf=a_conf,
            kpts_b_xy=b_xy,
            kpts_b_conf=b_conf,
            roi_w=w,
            roi_h=h,
            min_conf=self.min_kpt_conf,
        )

        score = compute_interaction_score(
            center_dist_norm=feats["center_dist_norm"],
            wrist_dist_norm=feats["wrist_dist_norm"],
            upper_body_dist_norm=feats["upper_body_dist_norm"],
            torso_dist_norm=feats["torso_dist_norm"],
            wrist_to_head_norm=feats["wrist_to_head_norm"],
            wrist_to_upper_torso_norm=feats["wrist_to_upper_torso_norm"],
            wrist_to_torso_norm=feats["wrist_to_torso_norm"],
            arm_extension_max=feats["arm_extension_max"],
            arm_alignment_max=feats["arm_alignment_max"],
            max_center_dist_norm=self.max_center_dist_norm,
            wrist_thr=self.wrist_dist_norm_thr,
            upper_thr=self.upper_body_dist_norm_thr,
            torso_thr=self.torso_dist_norm_thr,
            wrist_to_head_thr=self.wrist_to_head_thr,
            wrist_to_upper_torso_thr=self.wrist_to_upper_torso_thr,
            wrist_to_torso_thr=self.wrist_to_torso_thr,
            min_arm_extension=self.min_arm_extension,
            min_arm_alignment=self.min_arm_alignment,
        )

        strike_like = (
            (
                feats["wrist_to_head_norm"] <= self.wrist_to_head_thr
                or feats["wrist_to_upper_torso_norm"] <= self.wrist_to_upper_torso_thr
            )
            and feats["arm_extension_max"] >= self.min_arm_extension
            and feats["arm_alignment_max"] >= self.min_arm_alignment
            and feats["torso_dist_norm"] <= self.strike_torso_dist_norm_thr
        )

        grapple_like = (
            feats["torso_dist_norm"] <= self.grapple_torso_dist_norm_thr
            and feats["upper_body_dist_norm"] <= self.grapple_upper_body_dist_norm_thr
            and (
                feats["wrist_dist_norm"] <= self.grapple_wrist_dist_norm_thr
                or feats["center_dist_norm"] <= self.max_center_dist_norm * 0.75
                or feats["wrist_to_torso_norm"] <= self.wrist_to_torso_thr
            )
        )

        contact_like = strike_like or grapple_like

        if self.require_contact_like:
            ok = (score >= self.min_interaction_score) and contact_like
        else:
            ok = score >= self.min_interaction_score

        if self.draw_debug:
            txt1 = (
                f"pose={score:.3f} ok={int(ok)} "
                f"strike={int(strike_like)} grapple={int(grapple_like)} hist={hist_positive}"
            )
            txt2 = (
                f"va={valid_a} vb={valid_b} "
                f"td={feats['torso_dist_norm']:.2f} ud={feats['upper_body_dist_norm']:.2f} "
                f"wh={feats['wrist_to_head_norm']:.2f} wu={feats['wrist_to_upper_torso_norm']:.2f}"
            )
            txt3 = (
                f"wd={feats['wrist_dist_norm']:.2f} "
                f"arm={feats['arm_extension_max']:.2f} "
                f"align={feats['arm_alignment_max']:.2f}"
            )

            cv2.putText(
                roi_vis,
                txt1,
                (10, 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )
            cv2.putText(
                roi_vis,
                txt2,
                (10, 42),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.43,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )
            cv2.putText(
                roi_vis,
                txt3,
                (10, 62),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.43,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )

        return PoseResult(
            ok=bool(ok),
            score=float(score),
            num_persons=len(boxes_xyxy),
            hist_positive=hist_positive,
            valid_a=valid_a,
            valid_b=valid_b,
            kp_conf_a=kp_conf_a,
            kp_conf_b=kp_conf_b,
            center_dist_norm=float(feats["center_dist_norm"]),
            wrist_dist_norm=float(feats["wrist_dist_norm"]),
            upper_body_dist_norm=float(feats["upper_body_dist_norm"]),
            torso_dist_norm=float(feats["torso_dist_norm"]),
            wrist_to_head_norm=float(feats["wrist_to_head_norm"]),
            wrist_to_upper_torso_norm=float(feats["wrist_to_upper_torso_norm"]),
            wrist_to_torso_norm=float(feats["wrist_to_torso_norm"]),
            arm_extension_max=float(feats["arm_extension_max"]),
            arm_alignment_max=float(feats["arm_alignment_max"]),
            strike_like=bool(strike_like),
            grapple_like=bool(grapple_like),
            debug_frame=roi_vis,
        )