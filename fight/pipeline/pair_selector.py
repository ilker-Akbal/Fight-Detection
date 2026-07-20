from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os

from fight.pipeline.utils import box_area, box_center, box_iou, clamp, expand_box_xyxy


LOGGER = logging.getLogger("pair_selector")


def _pair_debug_enabled() -> bool:
    v = os.getenv("PAIR_DEBUG", "0").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _box_w(box):
    return float(max(1, box[2] - box[0]))


def _box_h(box):
    return float(max(1, box[3] - box[1]))


def _top_y(box):
    return float(box[1])


def _bottom_y(box):
    return float(box[3])


def _box_to_cxcywh(box):
    x1, y1, x2, y2 = box
    cx = 0.5 * (x1 + x2)
    cy = 0.5 * (y1 + y2)
    w = x2 - x1
    h = y2 - y1
    return cx, cy, w, h


def _cxcywh_to_box(cx, cy, w, h, frame_shape):
    x1 = int(round(cx - 0.5 * w))
    y1 = int(round(cy - 0.5 * h))
    x2 = int(round(cx + 0.5 * w))
    y2 = int(round(cy + 0.5 * h))
    return _clip_box((x1, y1, x2, y2), frame_shape)


def _clip_box(box, frame_shape):
    if box is None:
        return None
    h, w = frame_shape[:2]
    x1, y1, x2, y2 = box
    x1 = int(clamp(x1, 0, w - 1))
    y1 = int(clamp(y1, 0, h - 1))
    x2 = int(clamp(x2, 0, w))
    y2 = int(clamp(y2, 0, h))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def _vertical_overlap_ratio(box_a, box_b):
    top = max(_top_y(box_a), _top_y(box_b))
    bottom = min(_bottom_y(box_a), _bottom_y(box_b))
    inter = max(0.0, bottom - top)
    denom = max(1.0, min(_box_h(box_a), _box_h(box_b)))
    return float(inter / denom)


def _union_width_by_avg_height(box_a, box_b, union_box):
    uw = float(max(1, union_box[2] - union_box[0]))
    ah = 0.5 * (_box_h(box_a) + _box_h(box_b))
    return float(uw / max(1.0, ah))


def _soft_inverse_score(value, good, bad):
    if value <= good:
        return 1.0
    if value >= bad:
        return 0.0
    t = (value - good) / max(1e-6, bad - good)
    return float(1.0 - t)


def _soft_forward_score(value, bad, good):
    if value <= bad:
        return 0.0
    if value >= good:
        return 1.0
    t = (value - bad) / max(1e-6, good - bad)
    return float(t)


def union_pair_box(box_a, box_b, frame_shape, pad_ratio=0.14, min_pad_px=8):
    h, w = frame_shape[:2]

    x1 = min(box_a[0], box_b[0])
    y1 = min(box_a[1], box_b[1])
    x2 = max(box_a[2], box_b[2])
    y2 = max(box_a[3], box_b[3])

    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)

    pad_x = max(int(bw * pad_ratio), int(min_pad_px))
    pad_y = max(int(bh * pad_ratio), int(min_pad_px))

    x1 = clamp(x1 - pad_x, 0, w - 1)
    y1 = clamp(y1 - pad_y, 0, h - 1)
    x2 = clamp(x2 + pad_x, 0, w - 1)
    y2 = clamp(y2 + pad_y, 0, h - 1)

    if x2 <= x1 or y2 <= y1:
        return None

    return (int(x1), int(y1), int(x2), int(y2))


def pair_temporal_bonus(box_a, box_b, prev_pair_boxes):
    if prev_pair_boxes is None:
        return 0.0

    pa, pb = prev_pair_boxes
    s1 = 0.5 * (box_iou(box_a, pa) + box_iou(box_b, pb))
    s2 = 0.5 * (box_iou(box_a, pb) + box_iou(box_b, pa))
    return float(max(s1, s2))


def pair_identity_similarity(pair_a, pair_b):
    if pair_a is None or pair_b is None:
        return 0.0

    a1, a2 = pair_a
    b1, b2 = pair_b

    s1 = 0.5 * (box_iou(a1, b1) + box_iou(a2, b2))
    s2 = 0.5 * (box_iou(a1, b2) + box_iou(a2, b1))
    return float(max(s1, s2))


def smooth_box(
    prev_box,
    new_box,
    frame_shape,
    alpha_pos=0.26,
    alpha_size_grow=0.38,
    alpha_size_shrink=0.16,
    max_center_shift_ratio=0.28,
    fast_move_ratio=0.10,
    fast_alpha_pos=0.54,
    fast_alpha_size_grow=0.62,
):
    if new_box is None:
        return prev_box
    if prev_box is None:
        return _clip_box(new_box, frame_shape)

    pcx, pcy, pw, ph = _box_to_cxcywh(prev_box)
    ncx, ncy, nw, nh = _box_to_cxcywh(new_box)

    prev_side = max(pw, ph, 1.0)

    raw_dx = ncx - pcx
    raw_dy = ncy - pcy
    move_ratio = max(abs(raw_dx), abs(raw_dy)) / prev_side

    use_alpha_pos = alpha_pos
    use_alpha_size_grow = alpha_size_grow

    if move_ratio >= fast_move_ratio:
        use_alpha_pos = fast_alpha_pos
        use_alpha_size_grow = fast_alpha_size_grow

    max_shift = prev_side * max_center_shift_ratio
    dx = max(-max_shift, min(max_shift, raw_dx))
    dy = max(-max_shift, min(max_shift, raw_dy))

    target_cx = pcx + dx
    target_cy = pcy + dy

    cx = (1.0 - use_alpha_pos) * pcx + use_alpha_pos * target_cx
    cy = (1.0 - use_alpha_pos) * pcy + use_alpha_pos * target_cy

    aw = use_alpha_size_grow if nw >= pw else alpha_size_shrink
    ah = use_alpha_size_grow if nh >= ph else alpha_size_shrink

    w = (1.0 - aw) * pw + aw * nw
    h = (1.0 - ah) * ph + ah * nh

    return _cxcywh_to_box(cx, cy, w, h, frame_shape)


def pair_score_live(
    person_a,
    person_b,
    frame_shape,
    prev_union_box=None,
    prev_pair_boxes=None,
    debug=False,
    pair_indices=None,
):
    h, w = frame_shape[:2]
    frame_diag = float(max(math.hypot(w, h), 1.0))
    frame_area = float(max(w * h, 1))

    conf_a, box_a = person_a
    conf_b, box_b = person_b

    wa = _box_w(box_a)
    wb = _box_w(box_b)
    ha = _box_h(box_a)
    hb = _box_h(box_b)

    cax, cay = box_center(box_a)
    cbx, cby = box_center(box_b)

    dx = abs(cax - cbx)
    dy = abs(cay - cby)
    dist = float(math.hypot(dx, dy))
    dist_norm = dist / frame_diag

    width_sum = max(1.0, wa + wb)
    x_dist_ratio = dx / width_sum

    max_h = max(ha, hb, 1.0)
    bottom_diff_ratio = abs(_bottom_y(box_a) - _bottom_y(box_b)) / max_h
    height_ratio = min(ha, hb) / max(ha, hb, 1.0)
    y_overlap_ratio = _vertical_overlap_ratio(box_a, box_b)

    union_box = union_pair_box(box_a, box_b, frame_shape=frame_shape, pad_ratio=0.12, min_pad_px=6)
    if union_box is None:
        return -1e9

    union_area = box_area(union_box)
    union_ratio = union_area / frame_area
    fill_ratio = (box_area(box_a) + box_area(box_b)) / max(union_area, 1.0)
    union_width_height_ratio = _union_width_by_avg_height(box_a, box_b, union_box)

    min_box_h = min(ha, hb)
    min_box_w = min(wa, wb)

    if min_box_h < 18 or min_box_w < 8:
        return -1e9
    if dist_norm > 0.36:
        return -1e9
    if x_dist_ratio > 1.80:
        return -1e9
    if bottom_diff_ratio > 0.56:
        return -1e9
    if height_ratio < 0.45:
        return -1e9
    if y_overlap_ratio < 0.12:
        return -1e9
    if union_ratio > 0.42:
        return -1e9
    if fill_ratio < 0.10:
        return -1e9
    if union_width_height_ratio > 3.40:
        return -1e9

    conf_score = 0.5 * (float(conf_a) + float(conf_b))
    proximity = _soft_inverse_score(dist_norm, good=0.06, bad=0.30)
    x_close = _soft_inverse_score(x_dist_ratio, good=0.20, bad=1.55)
    bottom_align = _soft_inverse_score(bottom_diff_ratio, good=0.03, bad=0.42)
    height_sim = _soft_forward_score(height_ratio, bad=0.45, good=0.90)
    y_overlap_score = _soft_forward_score(y_overlap_ratio, bad=0.10, good=0.88)
    fill_score = _soft_forward_score(fill_ratio, bad=0.10, good=0.65)
    compact_score = _soft_inverse_score(union_width_height_ratio, good=1.0, bad=2.8)

    temporal_union = 0.0
    if prev_union_box is not None:
        temporal_union = box_iou(union_box, prev_union_box)

    temporal_pair = pair_temporal_bonus(box_a, box_b, prev_pair_boxes)

    score = (
        0.08 * conf_score
        + 0.17 * proximity
        + 0.18 * x_close
        + 0.12 * bottom_align
        + 0.08 * height_sim
        + 0.09 * y_overlap_score
        + 0.08 * fill_score
        + 0.08 * compact_score
        + 0.05 * temporal_union
        + 0.07 * temporal_pair
    )

    if debug:
        LOGGER.info(
            "[PAIRDBG][KEEP] i=%s j=%s score=%.4f conf=%.3f prox=%.3f x=%.3f "
            "bottom=%.3f h=%.3f yov=%.3f fill=%.3f compact=%.3f tU=%.3f tP=%.3f union=%s",
            None if pair_indices is None else pair_indices[0],
            None if pair_indices is None else pair_indices[1],
            float(score),
            conf_score,
            proximity,
            x_close,
            bottom_align,
            height_sim,
            y_overlap_score,
            fill_score,
            compact_score,
            temporal_union,
            temporal_pair,
            union_box,
        )

    return float(score)


def select_best_pair_live(persons, frame_shape, prev_union_box=None, prev_pair_boxes=None, debug=False):
    n = len(persons)
    if n < 2:
        if debug:
            LOGGER.info("[PAIRDBG][BEST] pair=None score=-1000000000.0000 union=None")
        return None, -1e9, None, None

    best_pair = None
    best_score = -1e9
    best_union_box = None
    best_pair_boxes = None

    for i in range(n):
        for j in range(i + 1, n):
            score = pair_score_live(
                persons[i],
                persons[j],
                frame_shape=frame_shape,
                prev_union_box=prev_union_box,
                prev_pair_boxes=prev_pair_boxes,
                debug=debug,
                pair_indices=(i, j),
            )

            if score > best_score:
                best_score = score
                best_pair = (i, j)
                best_union_box = union_pair_box(
                    persons[i][1],
                    persons[j][1],
                    frame_shape=frame_shape,
                    pad_ratio=0.12,
                    min_pad_px=6,
                )
                best_pair_boxes = (persons[i][1], persons[j][1])

    if debug:
        LOGGER.info(
            "[PAIRDBG][BEST] pair=%s score=%.4f union=%s",
            best_pair,
            float(best_score),
            best_union_box,
        )

    return best_pair, float(best_score), best_union_box, best_pair_boxes


@dataclass
class PairRoiState:
    pair_idx: tuple[int, int] | None = None
    pair_score: float = 0.0
    pair_boxes: tuple | None = None
    raw_union_box: tuple | None = None
    smooth_union_box: tuple | None = None
    hit_count: int = 0
    miss_count: int = 0

    candidate_pair: tuple[int, int] | None = None
    candidate_boxes: tuple | None = None
    candidate_score: float = 0.0
    candidate_hits: int = 0


class LivePairRoiController:
    def __init__(
        self,
        enter_score=0.48,
        keep_score=0.30,
        keep_frames=18,
        min_hits_to_activate=2,
        candidate_confirm_frames=2,
        pair_identity_iou_thr=0.32,
        switch_margin=0.05,
        roi_expand_x=1.22,
        roi_expand_y=1.16,
        debug=None,
    ):
        self.enter_score = float(enter_score)
        self.keep_score = float(keep_score)
        self.keep_frames = int(keep_frames)
        self.min_hits_to_activate = int(min_hits_to_activate)
        self.candidate_confirm_frames = int(candidate_confirm_frames)
        self.pair_identity_iou_thr = float(pair_identity_iou_thr)
        self.switch_margin = float(switch_margin)
        self.roi_expand_x = float(roi_expand_x)
        self.roi_expand_y = float(roi_expand_y)
        self.debug = _pair_debug_enabled() if debug is None else bool(debug)
        self.state = PairRoiState()

    def reset(self):
        self.state = PairRoiState()

    def _build_result(self, pair_ok, pair_idx, pair_score, roi_ok, roi_box, pair_boxes):
        return {
            "pair_ok": int(pair_ok),
            "pair_idx": pair_idx,
            "pair_score": float(pair_score),
            "roi_ok": int(roi_ok),
            "roi_box": roi_box,
            "pair_boxes": pair_boxes,
            "hit_count": int(self.state.hit_count),
            "miss_count": int(self.state.miss_count),
        }

    def _expanded_union(self, union_box, frame_shape):
        return expand_box_xyxy(
            union_box,
            frame_shape=frame_shape,
            scale_x=self.roi_expand_x,
            scale_y=self.roi_expand_y,
        )

    def _activate_or_update_active(self, pair_idx, pair_score, pair_boxes, union_box, frame_shape):
        same_prev = pair_identity_similarity(pair_boxes, self.state.pair_boxes) >= self.pair_identity_iou_thr

        if same_prev:
            self.state.hit_count += 1
        else:
            self.state.hit_count = 1

        self.state.miss_count = 0
        self.state.pair_idx = pair_idx
        self.state.pair_score = float(pair_score)
        self.state.pair_boxes = pair_boxes

        expanded_union_box = self._expanded_union(union_box, frame_shape)
        self.state.raw_union_box = expanded_union_box
        self.state.smooth_union_box = smooth_box(
            self.state.smooth_union_box,
            expanded_union_box,
            frame_shape=frame_shape,
        )

        self.state.candidate_pair = None
        self.state.candidate_boxes = None
        self.state.candidate_score = 0.0
        self.state.candidate_hits = 0

        roi_ok = self.state.hit_count >= self.min_hits_to_activate
        roi_box = self.state.smooth_union_box if roi_ok else None

        return self._build_result(
            pair_ok=1,
            pair_idx=pair_idx,
            pair_score=pair_score,
            roi_ok=roi_ok,
            roi_box=roi_box,
            pair_boxes=pair_boxes,
        )

    def _hold_with_roi(self):
        roi_box = self.state.smooth_union_box if self.state.smooth_union_box is not None else None
        roi_ok = 1 if roi_box is not None and self.state.hit_count >= self.min_hits_to_activate else 0

        return self._build_result(
            pair_ok=0,
            pair_idx=self.state.pair_idx,
            pair_score=float(self.state.pair_score),
            roi_ok=roi_ok,
            roi_box=roi_box if roi_ok else None,
            pair_boxes=self.state.pair_boxes,
        )

    def _can_hold(self):
        return (
            self.state.pair_boxes is not None
            and self.state.smooth_union_box is not None
            and self.state.miss_count < self.keep_frames
            and self.state.hit_count >= self.min_hits_to_activate
        )

    def _update_candidate(self, best_pair, best_pair_boxes, best_score):
        cand_similarity = pair_identity_similarity(best_pair_boxes, self.state.candidate_boxes)

        if cand_similarity >= self.pair_identity_iou_thr:
            self.state.candidate_hits += 1
            self.state.candidate_score = float(best_score)
        else:
            self.state.candidate_pair = best_pair
            self.state.candidate_boxes = best_pair_boxes
            self.state.candidate_score = float(best_score)
            self.state.candidate_hits = 1

    def update(self, persons, frame_shape):
        prev_union = self.state.raw_union_box
        prev_pair_boxes = self.state.pair_boxes

        best_pair, best_score, best_union_box, best_pair_boxes = select_best_pair_live(
            persons,
            frame_shape=frame_shape,
            prev_union_box=prev_union,
            prev_pair_boxes=prev_pair_boxes,
            debug=self.debug,
        )

        if best_pair is None or best_union_box is None or best_pair_boxes is None or best_score <= -1e8:
            if self._can_hold():
                self.state.miss_count += 1
                return self._hold_with_roi()

            self.reset()
            return self._build_result(0, None, 0.0, 0, None, None)

        active_exists = self.state.pair_boxes is not None
        active_similarity = pair_identity_similarity(best_pair_boxes, self.state.pair_boxes)
        active_matches = active_similarity >= self.pair_identity_iou_thr

        if active_exists and active_matches:
            if best_score >= self.keep_score:
                return self._activate_or_update_active(
                    best_pair, best_score, best_pair_boxes, best_union_box, frame_shape
                )

            if self._can_hold():
                self.state.miss_count += 1
                return self._hold_with_roi()

            self.reset()
            return self._build_result(0, None, 0.0, 0, None, None)

        if active_exists and not active_matches:
            strong_switch = best_score >= (max(self.enter_score, self.state.pair_score) + self.switch_margin)

            if strong_switch:
                self._update_candidate(best_pair, best_pair_boxes, best_score)

                if self.state.candidate_hits >= self.candidate_confirm_frames:
                    return self._activate_or_update_active(
                        best_pair, best_score, best_pair_boxes, best_union_box, frame_shape
                    )

            if self._can_hold():
                self.state.miss_count += 1
                return self._hold_with_roi()

            self.reset()
            return self._build_result(0, None, 0.0, 0, None, None)

        if best_score >= self.enter_score:
            self._update_candidate(best_pair, best_pair_boxes, best_score)

            if self.state.candidate_hits >= self.candidate_confirm_frames:
                return self._activate_or_update_active(
                    best_pair, best_score, best_pair_boxes, best_union_box, frame_shape
                )

        return self._build_result(0, None, 0.0, 0, None, None)