from __future__ import annotations

from itertools import combinations
from typing import Dict, List, Optional

import numpy as np


COCO_KPTS = {
    "nose": 0,
    "left_eye": 1,
    "right_eye": 2,
    "left_ear": 3,
    "right_ear": 4,
    "left_shoulder": 5,
    "right_shoulder": 6,
    "left_elbow": 7,
    "right_elbow": 8,
    "left_wrist": 9,
    "right_wrist": 10,
    "left_hip": 11,
    "right_hip": 12,
    "left_knee": 13,
    "right_knee": 14,
    "left_ankle": 15,
    "right_ankle": 16,
}


def safe_norm_dist(p1: Optional[np.ndarray], p2: Optional[np.ndarray], norm: float) -> float:
    if p1 is None or p2 is None or norm <= 1e-6:
        return 1e9
    return float(np.linalg.norm(p1 - p2) / norm)


def get_xy(kpts_xy: np.ndarray, kpts_conf: np.ndarray, idx: int, min_conf: float) -> Optional[np.ndarray]:
    if idx < 0 or idx >= len(kpts_xy):
        return None
    if float(kpts_conf[idx]) < float(min_conf):
        return None
    return np.asarray(kpts_xy[idx], dtype=np.float32)


def mean_points(points: List[Optional[np.ndarray]]) -> Optional[np.ndarray]:
    pts = [p for p in points if p is not None]
    if not pts:
        return None
    return np.stack(pts, axis=0).mean(axis=0)


def person_center_from_shoulders_hips(
    kpts_xy: np.ndarray, kpts_conf: np.ndarray, min_conf: float
) -> Optional[np.ndarray]:
    ids = [
        COCO_KPTS["left_shoulder"],
        COCO_KPTS["right_shoulder"],
        COCO_KPTS["left_hip"],
        COCO_KPTS["right_hip"],
    ]
    pts = [get_xy(kpts_xy, kpts_conf, idx, min_conf) for idx in ids]
    return mean_points(pts)


def head_center(kpts_xy: np.ndarray, kpts_conf: np.ndarray, min_conf: float) -> Optional[np.ndarray]:
    ids = [
        COCO_KPTS["nose"],
        COCO_KPTS["left_eye"],
        COCO_KPTS["right_eye"],
        COCO_KPTS["left_ear"],
        COCO_KPTS["right_ear"],
    ]
    pts = [get_xy(kpts_xy, kpts_conf, idx, min_conf) for idx in ids]
    return mean_points(pts)


def shoulder_center(kpts_xy: np.ndarray, kpts_conf: np.ndarray, min_conf: float) -> Optional[np.ndarray]:
    pts = [
        get_xy(kpts_xy, kpts_conf, COCO_KPTS["left_shoulder"], min_conf),
        get_xy(kpts_xy, kpts_conf, COCO_KPTS["right_shoulder"], min_conf),
    ]
    return mean_points(pts)


def hip_center(kpts_xy: np.ndarray, kpts_conf: np.ndarray, min_conf: float) -> Optional[np.ndarray]:
    pts = [
        get_xy(kpts_xy, kpts_conf, COCO_KPTS["left_hip"], min_conf),
        get_xy(kpts_xy, kpts_conf, COCO_KPTS["right_hip"], min_conf),
    ]
    return mean_points(pts)


def upper_torso_center(kpts_xy: np.ndarray, kpts_conf: np.ndarray, min_conf: float) -> Optional[np.ndarray]:
    return mean_points(
        [
            shoulder_center(kpts_xy, kpts_conf, min_conf),
            get_xy(kpts_xy, kpts_conf, COCO_KPTS["left_shoulder"], min_conf),
            get_xy(kpts_xy, kpts_conf, COCO_KPTS["right_shoulder"], min_conf),
        ]
    )


def torso_center(kpts_xy: np.ndarray, kpts_conf: np.ndarray, min_conf: float) -> Optional[np.ndarray]:
    return mean_points(
        [
            shoulder_center(kpts_xy, kpts_conf, min_conf),
            hip_center(kpts_xy, kpts_conf, min_conf),
        ]
    )


def count_valid_kpts(kpts_conf: np.ndarray, min_conf: float) -> int:
    return int(np.sum(np.asarray(kpts_conf) >= float(min_conf)))


def bbox_area_xyxy(box: np.ndarray) -> float:
    x1, y1, x2, y2 = box[:4]
    return float(max(0.0, x2 - x1) * max(0.0, y2 - y1))


def top2_person_indices_by_area(boxes_xyxy: np.ndarray) -> List[int]:
    if boxes_xyxy is None or len(boxes_xyxy) == 0:
        return []
    areas = np.array([bbox_area_xyxy(b) for b in boxes_xyxy], dtype=np.float32)
    idx = np.argsort(-areas)
    return [int(i) for i in idx[:2]]


def _safe_cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 1e-6 or nb <= 1e-6:
        return 0.0
    v = float(np.dot(a, b) / (na * nb))
    return float(max(-1.0, min(1.0, v)))


def _closeness_score(value: float, thr: float) -> float:
    if value >= 1e9:
        return 0.0
    return float(max(0.0, 1.0 - value / max(thr, 1e-6)))


def min_dist_point_to_points(p: Optional[np.ndarray], pts: List[Optional[np.ndarray]], norm: float) -> float:
    if p is None or norm <= 1e-6:
        return 1e9

    vals = []
    for q in pts:
        if q is not None:
            vals.append(float(np.linalg.norm(p - q) / norm))
    return min(vals) if vals else 1e9


def arm_extension_ratio(kpts_xy: np.ndarray, kpts_conf: np.ndarray, side: str, min_conf: float) -> float:
    if side == "left":
        s = get_xy(kpts_xy, kpts_conf, COCO_KPTS["left_shoulder"], min_conf)
        e = get_xy(kpts_xy, kpts_conf, COCO_KPTS["left_elbow"], min_conf)
        w = get_xy(kpts_xy, kpts_conf, COCO_KPTS["left_wrist"], min_conf)
    else:
        s = get_xy(kpts_xy, kpts_conf, COCO_KPTS["right_shoulder"], min_conf)
        e = get_xy(kpts_xy, kpts_conf, COCO_KPTS["right_elbow"], min_conf)
        w = get_xy(kpts_xy, kpts_conf, COCO_KPTS["right_wrist"], min_conf)

    if s is None or e is None or w is None:
        return 0.0

    se = np.linalg.norm(e - s)
    ew = np.linalg.norm(w - e)
    sw = np.linalg.norm(w - s)

    denom = max(1e-6, se + ew)
    return float(max(0.0, min(1.0, sw / denom)))


def max_arm_extension(kpts_xy: np.ndarray, kpts_conf: np.ndarray, min_conf: float) -> float:
    return max(
        arm_extension_ratio(kpts_xy, kpts_conf, "left", min_conf),
        arm_extension_ratio(kpts_xy, kpts_conf, "right", min_conf),
    )


def _arm_direction_alignment(
    kpts_xy: np.ndarray,
    kpts_conf: np.ndarray,
    target_points: List[Optional[np.ndarray]],
    min_conf: float,
) -> float:
    valid_targets = [p for p in target_points if p is not None]
    if not valid_targets:
        return 0.0

    best = 0.0
    for side in ("left", "right"):
        if side == "left":
            shoulder = get_xy(kpts_xy, kpts_conf, COCO_KPTS["left_shoulder"], min_conf)
            wrist = get_xy(kpts_xy, kpts_conf, COCO_KPTS["left_wrist"], min_conf)
        else:
            shoulder = get_xy(kpts_xy, kpts_conf, COCO_KPTS["right_shoulder"], min_conf)
            wrist = get_xy(kpts_xy, kpts_conf, COCO_KPTS["right_wrist"], min_conf)

        if shoulder is None or wrist is None:
            continue

        arm_vec = wrist - shoulder
        for tgt in valid_targets:
            tgt_vec = tgt - shoulder
            score = max(0.0, _safe_cosine(arm_vec, tgt_vec))
            best = max(best, float(score))

    return float(best)


def compute_pair_features(
    kpts_a_xy: np.ndarray,
    kpts_a_conf: np.ndarray,
    kpts_b_xy: np.ndarray,
    kpts_b_conf: np.ndarray,
    roi_w: int,
    roi_h: int,
    min_conf: float,
) -> Dict[str, float]:
    norm = float(max(roi_w, roi_h, 1))

    center_a = person_center_from_shoulders_hips(kpts_a_xy, kpts_a_conf, min_conf)
    center_b = person_center_from_shoulders_hips(kpts_b_xy, kpts_b_conf, min_conf)
    center_dist = safe_norm_dist(center_a, center_b, norm)

    shoulder_a = shoulder_center(kpts_a_xy, kpts_a_conf, min_conf)
    shoulder_b = shoulder_center(kpts_b_xy, kpts_b_conf, min_conf)
    upper_body_dist = safe_norm_dist(shoulder_a, shoulder_b, norm)

    upper_torso_a = upper_torso_center(kpts_a_xy, kpts_a_conf, min_conf)
    upper_torso_b = upper_torso_center(kpts_b_xy, kpts_b_conf, min_conf)

    torso_a = torso_center(kpts_a_xy, kpts_a_conf, min_conf)
    torso_b = torso_center(kpts_b_xy, kpts_b_conf, min_conf)
    torso_dist = safe_norm_dist(torso_a, torso_b, norm)

    head_a = head_center(kpts_a_xy, kpts_a_conf, min_conf)
    head_b = head_center(kpts_b_xy, kpts_b_conf, min_conf)

    lw_a = get_xy(kpts_a_xy, kpts_a_conf, COCO_KPTS["left_wrist"], min_conf)
    rw_a = get_xy(kpts_a_xy, kpts_a_conf, COCO_KPTS["right_wrist"], min_conf)
    lw_b = get_xy(kpts_b_xy, kpts_b_conf, COCO_KPTS["left_wrist"], min_conf)
    rw_b = get_xy(kpts_b_xy, kpts_b_conf, COCO_KPTS["right_wrist"], min_conf)

    wrists_a = [lw_a, rw_a]
    wrists_b = [lw_b, rw_b]

    wrist_dists = []
    for pa in wrists_a:
        for pb in wrists_b:
            if pa is not None and pb is not None:
                wrist_dists.append(safe_norm_dist(pa, pb, norm))
    wrist_dist = min(wrist_dists) if wrist_dists else 1e9

    wrist_to_head = min(
        min_dist_point_to_points(lw_a, [head_b], norm),
        min_dist_point_to_points(rw_a, [head_b], norm),
        min_dist_point_to_points(lw_b, [head_a], norm),
        min_dist_point_to_points(rw_b, [head_a], norm),
    )

    wrist_to_upper_torso = min(
        min_dist_point_to_points(lw_a, [upper_torso_b, shoulder_b], norm),
        min_dist_point_to_points(rw_a, [upper_torso_b, shoulder_b], norm),
        min_dist_point_to_points(lw_b, [upper_torso_a, shoulder_a], norm),
        min_dist_point_to_points(rw_b, [upper_torso_a, shoulder_a], norm),
    )

    wrist_to_torso = min(
        min_dist_point_to_points(lw_a, [torso_b], norm),
        min_dist_point_to_points(rw_a, [torso_b], norm),
        min_dist_point_to_points(lw_b, [torso_a], norm),
        min_dist_point_to_points(rw_b, [torso_a], norm),
    )

    arm_ext_a = max_arm_extension(kpts_a_xy, kpts_a_conf, min_conf)
    arm_ext_b = max_arm_extension(kpts_b_xy, kpts_b_conf, min_conf)

    arm_align_a = _arm_direction_alignment(
        kpts_a_xy,
        kpts_a_conf,
        [head_b, upper_torso_b, torso_b],
        min_conf,
    )
    arm_align_b = _arm_direction_alignment(
        kpts_b_xy,
        kpts_b_conf,
        [head_a, upper_torso_a, torso_a],
        min_conf,
    )

    return {
        "center_dist_norm": float(center_dist),
        "wrist_dist_norm": float(wrist_dist),
        "upper_body_dist_norm": float(upper_body_dist),
        "torso_dist_norm": float(torso_dist),
        "wrist_to_head_norm": float(wrist_to_head),
        "wrist_to_upper_torso_norm": float(wrist_to_upper_torso),
        "wrist_to_torso_norm": float(wrist_to_torso),
        "arm_extension_a": float(arm_ext_a),
        "arm_extension_b": float(arm_ext_b),
        "arm_extension_max": float(max(arm_ext_a, arm_ext_b)),
        "arm_alignment_a": float(arm_align_a),
        "arm_alignment_b": float(arm_align_b),
        "arm_alignment_max": float(max(arm_align_a, arm_align_b)),
    }


def compute_interaction_score(
    center_dist_norm: float,
    wrist_dist_norm: float,
    upper_body_dist_norm: float,
    torso_dist_norm: float,
    wrist_to_head_norm: float,
    wrist_to_upper_torso_norm: float,
    wrist_to_torso_norm: float,
    arm_extension_max: float,
    arm_alignment_max: float,
    max_center_dist_norm: float,
    wrist_thr: float,
    upper_thr: float,
    torso_thr: float,
    wrist_to_head_thr: float,
    wrist_to_upper_torso_thr: float,
    wrist_to_torso_thr: float,
    min_arm_extension: float,
    min_arm_alignment: float,
) -> float:
    s_center = _closeness_score(center_dist_norm, max_center_dist_norm)
    s_wrist = _closeness_score(wrist_dist_norm, wrist_thr)
    s_upper = _closeness_score(upper_body_dist_norm, upper_thr)
    s_torso = _closeness_score(torso_dist_norm, torso_thr)

    s_head_touch = _closeness_score(wrist_to_head_norm, wrist_to_head_thr)
    s_upper_touch = _closeness_score(wrist_to_upper_torso_norm, wrist_to_upper_torso_thr)
    s_torso_touch = _closeness_score(wrist_to_torso_norm, wrist_to_torso_thr)

    s_arm = max(
        0.0,
        min(1.0, (arm_extension_max - min_arm_extension) / max(1e-6, 1.0 - min_arm_extension)),
    )
    s_align = max(
        0.0,
        min(1.0, (arm_alignment_max - min_arm_alignment) / max(1e-6, 1.0 - min_arm_alignment)),
    )

    score = (
        0.10 * s_center +
        0.08 * s_wrist +
        0.14 * s_upper +
        0.16 * s_torso +
        0.16 * s_head_touch +
        0.18 * s_upper_touch +
        0.06 * s_torso_touch +
        0.06 * s_arm +
        0.06 * s_align
    )
    return float(max(0.0, min(1.0, score)))


def select_best_pair_indices(
    boxes_xyxy: np.ndarray,
    kp_xy: np.ndarray,
    kp_conf: np.ndarray,
    roi_w: int,
    roi_h: int,
    min_conf: float,
    min_valid_kpts_select: int = 4,
) -> List[int]:
    if boxes_xyxy is None or len(boxes_xyxy) < 2:
        return []

    n = len(boxes_xyxy)
    best_pair: Optional[List[int]] = None
    best_score = -1.0

    for i, j in combinations(range(n), 2):
        valid_i = count_valid_kpts(kp_conf[i], min_conf)
        valid_j = count_valid_kpts(kp_conf[j], min_conf)
        if valid_i < min_valid_kpts_select or valid_j < min_valid_kpts_select:
            continue

        feats = compute_pair_features(
            kpts_a_xy=kp_xy[i],
            kpts_a_conf=kp_conf[i],
            kpts_b_xy=kp_xy[j],
            kpts_b_conf=kp_conf[j],
            roi_w=roi_w,
            roi_h=roi_h,
            min_conf=min_conf,
        )

        s_center = _closeness_score(feats["center_dist_norm"], 0.45)
        s_torso = _closeness_score(feats["torso_dist_norm"], 0.30)
        s_head = _closeness_score(feats["wrist_to_head_norm"], 0.18)
        s_upper_touch = _closeness_score(feats["wrist_to_upper_torso_norm"], 0.22)
        s_arm = float(feats["arm_extension_max"])
        s_align = float(feats["arm_alignment_max"])

        pair_score = (
            0.22 * s_center +
            0.24 * s_torso +
            0.18 * s_head +
            0.20 * s_upper_touch +
            0.08 * s_arm +
            0.08 * s_align
        )

        if pair_score > best_score:
            best_score = float(pair_score)
            best_pair = [int(i), int(j)]

    if best_pair is not None:
        return best_pair

    return top2_person_indices_by_area(boxes_xyxy)