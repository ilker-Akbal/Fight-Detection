from __future__ import annotations

from typing import Dict, Sequence

import cv2
import numpy as np

from fight.pipeline.utils import resize_with_padding


def make_square_pair_roi(
    frame: np.ndarray,
    box_a: Sequence[float],
    box_b: Sequence[float],
    out_size: int = 224,
    margin: float = 0.15,
    pad_value: int = 114,
) -> Dict[str, object]:
    h, w = frame.shape[:2]

    ax1, ay1, ax2, ay2 = [int(round(v)) for v in box_a[:4]]
    bx1, by1, bx2, by2 = [int(round(v)) for v in box_b[:4]]

    x1 = min(ax1, bx1)
    y1 = min(ay1, by1)
    x2 = max(ax2, bx2)
    y2 = max(ay2, by2)

    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)

    mx = int(round(bw * margin))
    my = int(round(bh * margin))

    x1 -= mx
    y1 -= my
    x2 += mx
    y2 += my

    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    side = max(bw, bh)

    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0

    sx1 = int(round(cx - side / 2.0))
    sy1 = int(round(cy - side / 2.0))
    sx2 = sx1 + side
    sy2 = sy1 + side

    crop_x1 = max(0, sx1)
    crop_y1 = max(0, sy1)
    crop_x2 = min(w, sx2)
    crop_y2 = min(h, sy2)

    roi = frame[crop_y1:crop_y2, crop_x1:crop_x2]

    pad_left = max(0, -sx1)
    pad_top = max(0, -sy1)
    pad_right = max(0, sx2 - w)
    pad_bottom = max(0, sy2 - h)

    if roi.size == 0:
        roi = np.full((side, side, 3), pad_value, dtype=np.uint8)
    elif pad_left > 0 or pad_top > 0 or pad_right > 0 or pad_bottom > 0:
        roi = cv2.copyMakeBorder(
            roi,
            pad_top,
            pad_bottom,
            pad_left,
            pad_right,
            borderType=cv2.BORDER_CONSTANT,
            value=(pad_value, pad_value, pad_value),
        )

    roi_resized = resize_with_padding(
        roi,
        out_size=out_size,
        pad_value=pad_value,
        interpolation=cv2.INTER_LINEAR,
    )

    return {
        "roi_bgr": roi_resized,
        "square_box_xyxy": [sx1, sy1, sx2, sy2],
        "union_box_xyxy": [x1, y1, x2, y2],
    }