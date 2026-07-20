from __future__ import annotations

import cv2
import numpy as np


def draw_text(img, text, x, y, scale=0.7, color=(0, 255, 0), thickness=2):
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def build_debug_lines(
    fps,
    motion_score,
    persons_count,
    pose_eff,
    pose_score,
    last_fight_prob,
    fight_active,
    clip_len,
    pose_enabled,
):
    lines = [
        f"fps={fps:.1f}",
        f"motion_score={motion_score:.4f}",
        f"persons={persons_count}",
        f"pose_eff={1 if pose_eff else 0}",
        f"fight_prob={last_fight_prob:.3f}",
        f"clip_len={clip_len}",
        f"fight_active={'FIGHT' if fight_active else 'NO_FIGHT'}",
    ]

    if pose_enabled:
        lines.append(f"pose={pose_score:.3f}")
    else:
        lines.append("pose=off")

    return lines


def compose_debug_view(frame_bgr, lines, panel_width=440):
    h, _ = frame_bgr.shape[:2]
    panel = np.zeros((h, panel_width, 3), dtype=np.uint8)

    y = 35
    for line in lines:
        draw_text(panel, line, 12, y, scale=0.75, color=(0, 255, 0), thickness=2)
        y += 34

    return np.hstack([frame_bgr, panel])