from __future__ import annotations

import cv2
import numpy as np


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def sanitize_box(box, frame_shape):
    if box is None:
        return None

    h, w = frame_shape[:2]
    x1, y1, x2, y2 = box

    x1 = clamp(int(x1), 0, w - 1)
    y1 = clamp(int(y1), 0, h - 1)
    x2 = clamp(int(x2), 0, w - 1)
    y2 = clamp(int(y2), 0, h - 1)

    if x2 <= x1 or y2 <= y1:
        return None

    return (x1, y1, x2, y2)


def smooth_box(prev_box, new_box, alpha=0.30):
    if prev_box is None:
        return new_box
    if new_box is None:
        return prev_box

    px1, py1, px2, py2 = prev_box
    nx1, ny1, nx2, ny2 = new_box

    sx1 = int((1.0 - alpha) * px1 + alpha * nx1)
    sy1 = int((1.0 - alpha) * py1 + alpha * ny1)
    sx2 = int((1.0 - alpha) * px2 + alpha * nx2)
    sy2 = int((1.0 - alpha) * py2 + alpha * ny2)

    return (sx1, sy1, sx2, sy2)


def expand_box_xyxy(box, frame_shape, scale_x=1.18, scale_y=1.12):
    """
    ROI box'ı merkezini koruyarak genişletir.
    Ani hareketlerde kişilerin ROI dışına taşmasını azaltır.

    box: (x1, y1, x2, y2)
    scale_x: yatay genişletme katsayısı
    scale_y: dikey genişletme katsayısı
    """
    if box is None:
        return None

    h, w = frame_shape[:2]
    x1, y1, x2, y2 = box

    bw = max(1.0, float(x2 - x1))
    bh = max(1.0, float(y2 - y1))

    cx = 0.5 * (x1 + x2)
    cy = 0.5 * (y1 + y2)

    new_w = bw * float(scale_x)
    new_h = bh * float(scale_y)

    nx1 = int(round(cx - 0.5 * new_w))
    ny1 = int(round(cy - 0.5 * new_h))
    nx2 = int(round(cx + 0.5 * new_w))
    ny2 = int(round(cy + 0.5 * new_h))

    nx1 = clamp(nx1, 0, w - 1)
    ny1 = clamp(ny1, 0, h - 1)
    nx2 = clamp(nx2, 0, w - 1)
    ny2 = clamp(ny2, 0, h - 1)

    if nx2 <= nx1 or ny2 <= ny1:
        return None

    return (nx1, ny1, nx2, ny2)


def resize_with_padding(img, out_size=320, pad_value=114, interpolation=cv2.INTER_LINEAR):
    """
    Görüntüyü aspect ratio koruyarak out_size x out_size boyutuna getirir.
    Kısa kenarlara padding ekler, stretch yapmaz.
    """
    if img is None or img.size == 0:
        return None

    h, w = img.shape[:2]
    if h <= 0 or w <= 0:
        return None

    if isinstance(out_size, int):
        target_h = out_size
        target_w = out_size
    else:
        target_w, target_h = out_size

    scale = min(target_w / float(w), target_h / float(h))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))

    resized = cv2.resize(img, (new_w, new_h), interpolation=interpolation)

    pad_w = target_w - new_w
    pad_h = target_h - new_h

    left = pad_w // 2
    right = pad_w - left
    top = pad_h // 2
    bottom = pad_h - top

    padded = cv2.copyMakeBorder(
        resized,
        top,
        bottom,
        left,
        right,
        borderType=cv2.BORDER_CONSTANT,
        value=(pad_value, pad_value, pad_value),
    )
    return padded


def crop_from_box(frame_bgr, box, out_size=320, pad_value=114, return_raw=False):
    """
    Box'tan crop alır.
    return_raw=False ise aspect ratio korunarak padding'li şekilde out_size'a taşır.
    return_raw=True ise sadece ham crop döner.
    """
    if box is None:
        return None

    H, W = frame_bgr.shape[:2]
    x1, y1, x2, y2 = box

    x1 = max(0, min(W - 1, int(x1)))
    y1 = max(0, min(H - 1, int(y1)))
    x2 = max(0, min(W, int(x2)))
    y2 = max(0, min(H, int(y2)))

    if x2 <= x1 or y2 <= y1:
        return None

    crop = frame_bgr[y1:y2, x1:x2]
    if crop is None or crop.size == 0:
        return None

    if return_raw:
        return crop

    return resize_with_padding(
        crop,
        out_size=out_size,
        pad_value=pad_value,
        interpolation=cv2.INTER_LINEAR,
    )


def box_area(box):
    x1, y1, x2, y2 = box
    return float(max(0, x2 - x1) * max(0, y2 - y1))


def box_center(box):
    x1, y1, x2, y2 = box
    return (0.5 * (x1 + x2), 0.5 * (y1 + y2))


def box_iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    denom = area_a + area_b - inter
    return float(inter / denom) if denom > 0 else 0.0


def open_source(source: str):
    if source.isdigit():
        cap = cv2.VideoCapture(int(source), cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)

    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap