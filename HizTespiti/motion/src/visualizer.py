from __future__ import annotations

import cv2

from .motion_gate import MotionGateResult
from .utils import put_text


class MotionVisualizer:
    def draw(
        self,
        frame,
        boxes,
        motion_score: float,
        gate: MotionGateResult,
        frame_idx: int,
        camera_id: str,
    ):
        out = frame.copy()

        for x, y, w, h, area in boxes:
            cv2.rectangle(out, (x, y), (x + w, y + h), (0, 180, 255), 2)
            put_text(out, f"{int(area)}", (x, max(20, y - 5)), 0.45, 1)

        state = "ACTIVE" if gate.active else "IDLE"

        put_text(out, f"camera: {camera_id}", (20, 30))
        put_text(out, f"frame: {frame_idx}", (20, 58))
        put_text(out, f"motion_score: {motion_score:.5f}", (20, 86))
        put_text(out, f"gate: {state} / {gate.reason}", (20, 114))
        put_text(out, f"open_ctr: {gate.open_counter} close_ctr: {gate.close_counter}", (20, 142))

        if gate.active:
            cv2.rectangle(out, (5, 5), (out.shape[1] - 5, out.shape[0] - 5), (0, 0, 255), 3)

        return out