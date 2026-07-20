from __future__ import annotations

from pathlib import Path
import time

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


def put_text(
    frame,
    text: str,
    org: tuple[int, int],
    scale: float = 0.55,
    thickness: int = 2,
    color: tuple[int, int, int] = (0, 255, 255),
):
    cv2.putText(
        frame,
        text,
        org,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color,
        thickness,
        cv2.LINE_AA,
    )