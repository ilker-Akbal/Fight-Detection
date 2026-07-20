from __future__ import annotations

from dataclasses import dataclass

from .motion_config import MotionConfig


@dataclass
class MotionGateResult:
    active: bool
    opened: bool
    closed: bool
    open_counter: int
    close_counter: int
    reason: str


class MotionGate:
    def __init__(self, cfg: MotionConfig):
        self.cfg = cfg
        self.active = False
        self.open_counter = 0
        self.close_counter = 0

    def update(self, frame_idx: int, motion_score: float, boxes_count: int) -> MotionGateResult:
        if frame_idx < self.cfg.ignore_first_frames:
            return MotionGateResult(
                active=False,
                opened=False,
                closed=False,
                open_counter=self.open_counter,
                close_counter=self.close_counter,
                reason="warmup",
            )

        has_motion = (
            motion_score >= self.cfg.min_motion_score
            and boxes_count > 0
        )

        opened = False
        closed = False

        if has_motion:
            self.open_counter += 1
            self.close_counter = 0
        else:
            self.close_counter += 1
            self.open_counter = 0

        if not self.active and self.open_counter >= self.cfg.open_frames:
            self.active = True
            opened = True

        if self.active and self.close_counter >= self.cfg.close_frames:
            self.active = False
            closed = True

        reason = "motion" if has_motion else "no_motion"

        return MotionGateResult(
            active=self.active,
            opened=opened,
            closed=closed,
            open_counter=self.open_counter,
            close_counter=self.close_counter,
            reason=reason,
        )