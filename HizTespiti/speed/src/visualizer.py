from __future__ import annotations

import cv2

from HizTespiti.yolo.src.simple_tracker import Track
from HizTespiti.motion.src.motion_gate import MotionGateResult

from .speed_estimator import SpeedResult
from .violation_decider import ViolationDecision
from .utils import put_text


class SpeedVisualizer:
    def draw(
        self,
        frame,
        tracks: list[Track],
        speed_results: dict[int, SpeedResult],
        decisions: dict[int, ViolationDecision],
        motion_gate: MotionGateResult | None,
        frame_idx: int,
        camera_id: str,
        yolo_ran: bool,
        speed_limit_kmh: float,
        threshold_kmh: float,
        meter_per_pixel: float | None,
    ):
        out = frame.copy()

        for tr in tracks:
            x1, y1, x2, y2 = [int(v) for v in tr.box]

            decision = decisions.get(tr.track_id)
            result = speed_results.get(tr.track_id)

            is_violation = bool(decision and decision.violation)
            color = (0, 0, 255) if is_violation else (0, 200, 255)

            cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)

            speed_txt = "speed: --"
            if result and result.speed_kmh is not None:
                speed_txt = f"{result.speed_kmh:.1f} km/h"

            label = f"ID:{tr.track_id} {tr.cls_name} {speed_txt}"
            put_text(out, label, (x1, max(20, y1 - 8)), 0.50, 2, color)

            if len(tr.history) >= 2:
                pts = [(int(cx), int(cy)) for _, cx, cy in tr.history[-40:]]
                for i in range(1, len(pts)):
                    cv2.line(out, pts[i - 1], pts[i], (255, 255, 0), 2)

            if decision and decision.should_report:
                put_text(out, "SPEED VIOLATION", (x1, min(out.shape[0] - 20, y2 + 25)), 0.65, 2, (0, 0, 255))

        if motion_gate is None:
            motion_text = "motion: disabled"
        else:
            motion_text = f"motion: {'ACTIVE' if motion_gate.active else 'IDLE'} / {motion_gate.reason}"

        scale_text = "scale: missing" if meter_per_pixel is None else f"scale: {meter_per_pixel:.6f} m/px"

        put_text(out, f"camera: {camera_id}", (20, 30))
        put_text(out, f"frame: {frame_idx}", (20, 58))
        put_text(out, motion_text, (20, 86))
        put_text(out, f"yolo_ran: {yolo_ran}", (20, 114))
        put_text(out, f"limit: {speed_limit_kmh:.1f} threshold: {threshold_kmh:.1f} km/h", (20, 142))
        put_text(out, scale_text, (20, 170))
        put_text(out, f"tracks: {len(tracks)}", (20, 198))

        return out