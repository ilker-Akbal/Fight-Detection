from __future__ import annotations

import time
from pathlib import Path

import cv2


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def resize_keep_aspect(frame, width: int):
    if width <= 0:
        return frame

    h, w = frame.shape[:2]
    if w == width:
        return frame

    scale = width / float(w)
    new_h = int(h * scale)
    return cv2.resize(frame, (width, new_h), interpolation=cv2.INTER_AREA)


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def put_text(frame, text: str, org: tuple[int, int], scale: float = 0.55, thickness: int = 2):
    cv2.putText(
        frame,
        text,
        org,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (0, 255, 255),
        thickness,
        cv2.LINE_AA,
    )


def bbox_center_xyxy(box: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def xywh_to_xyxy(x: float, y: float, w: float, h: float):
    return x, y, x + w, y + h


def clip_box_xyxy(box, width: int, height: int):
    x1, y1, x2, y2 = box
    x1 = max(0, min(width - 1, int(x1)))
    y1 = max(0, min(height - 1, int(y1)))
    x2 = max(0, min(width - 1, int(x2)))
    y2 = max(0, min(height - 1, int(y2)))
    return x1, y1, x2, y2