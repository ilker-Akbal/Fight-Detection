from __future__ import annotations

from dataclasses import dataclass, field
import time

from .speed_estimator import SpeedResult


@dataclass
class TrackViolationState:
    over_counter: int = 0
    last_alert_time: float = 0.0
    already_reported: bool = False
    last_speed_kmh: float | None = None


@dataclass
class ViolationDecision:
    violation: bool
    should_report: bool
    reason: str
    threshold_kmh: float
    speed_kmh: float | None


class ViolationDecider:
    def __init__(
        self,
        speed_limit_kmh: float,
        tolerance_kmh: float,
        confirm_frames: int,
        cooldown_sec: float,
    ):
        self.speed_limit_kmh = speed_limit_kmh
        self.tolerance_kmh = tolerance_kmh
        self.threshold_kmh = speed_limit_kmh + tolerance_kmh
        self.confirm_frames = max(1, int(confirm_frames))
        self.cooldown_sec = float(cooldown_sec)
        self.states: dict[int, TrackViolationState] = {}

    def update(self, result: SpeedResult) -> ViolationDecision:
        st = self.states.setdefault(result.track_id, TrackViolationState())

        if not result.valid or result.speed_kmh is None:
            st.over_counter = 0
            return ViolationDecision(
                violation=False,
                should_report=False,
                reason=result.reason,
                threshold_kmh=self.threshold_kmh,
                speed_kmh=result.speed_kmh,
            )

        st.last_speed_kmh = result.speed_kmh

        if result.speed_kmh > self.threshold_kmh:
            st.over_counter += 1
        else:
            st.over_counter = 0
            return ViolationDecision(
                violation=False,
                should_report=False,
                reason="under_limit",
                threshold_kmh=self.threshold_kmh,
                speed_kmh=result.speed_kmh,
            )

        if st.over_counter < self.confirm_frames:
            return ViolationDecision(
                violation=True,
                should_report=False,
                reason="waiting_confirm",
                threshold_kmh=self.threshold_kmh,
                speed_kmh=result.speed_kmh,
            )

        now = time.time()

        if st.already_reported and now - st.last_alert_time < self.cooldown_sec:
            return ViolationDecision(
                violation=True,
                should_report=False,
                reason="cooldown",
                threshold_kmh=self.threshold_kmh,
                speed_kmh=result.speed_kmh,
            )

        st.already_reported = True
        st.last_alert_time = now

        return ViolationDecision(
            violation=True,
            should_report=True,
            reason="speed_violation",
            threshold_kmh=self.threshold_kmh,
            speed_kmh=result.speed_kmh,
        )

    def cleanup(self, active_track_ids: set[int]) -> None:
        dead = [tid for tid in self.states.keys() if tid not in active_track_ids]
        for tid in dead:
            self.states.pop(tid, None)