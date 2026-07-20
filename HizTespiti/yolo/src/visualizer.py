from __future__ import annotations

import cv2

from HizTespiti.motion.src.motion_gate import MotionGateResult

from .simple_tracker import Track
from .utils import put_text


class YoloVisualizer:
    def draw(
        self,
        frame,
        detections_count: int,
        tracks: list[Track],
        motion_gate: MotionGateResult | None,
        frame_idx: int,
        camera_id: str,
        yolo_ran: bool,
    ):
        out = frame.copy()

        for tr in tracks:
            x1, y1, x2, y2 = [int(v) for v in tr.box]

            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 200, 255), 2)

            label = f"ID:{tr.track_id} {tr.cls_name} {tr.conf:.2f}"
            put_text(out, label, (x1, max(20, y1 - 8)), 0.50, 2)

            if len(tr.history) >= 2:
                pts = [(int(cx), int(cy)) for _, cx, cy in tr.history[-30:]]
                for i in range(1, len(pts)):
                    cv2.line(out, pts[i - 1], pts[i], (255, 255, 0), 2)

        if motion_gate is None:
            motion_text = "motion: disabled"
            active = True
        else:
            active = motion_gate.active
            motion_text = f"motion: {'ACTIVE' if active else 'IDLE'} / {motion_gate.reason}"

        put_text(out, f"camera: {camera_id}", (20, 30))
        put_text(out, f"frame: {frame_idx}", (20, 58))
        put_text(out, motion_text, (20, 86))
        put_text(out, f"yolo_ran: {yolo_ran}", (20, 114))
        put_text(out, f"detections: {detections_count}", (20, 142))
        put_text(out, f"tracks: {len(tracks)}", (20, 170))

        if active:
            cv2.rectangle(out, (5, 5), (out.shape[1] - 5, out.shape[0] - 5), (0, 0, 255), 3)

        return out