from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque


@dataclass
class PoseGateDecision:
    pose_ok: bool
    hist_positive: int
    current_positive: bool
    score: float
    mean_score: float
    peak_score: float
    consecutive_positive: int


class PoseGate:
    def __init__(
        self,
        window_size: int = 6,
        need_positive: int = 2,
        min_mean_score: float = 0.30,
        peak_score_thr: float = 0.54,
        min_consecutive: int = 2,
    ):
        self.window_size = int(max(1, window_size))
        self.need_positive = int(max(1, need_positive))
        self.min_mean_score = float(min_mean_score)
        self.peak_score_thr = float(peak_score_thr)
        self.min_consecutive = int(max(1, min_consecutive))

        self.hist: Deque[bool] = deque(maxlen=self.window_size)
        self.score_hist: Deque[float] = deque(maxlen=self.window_size)

    def reset(self) -> None:
        self.hist.clear()
        self.score_hist.clear()

    def update(self, score: float, positive: bool) -> PoseGateDecision:
        self.hist.append(bool(positive))
        self.score_hist.append(float(score))

        hist_positive = int(sum(self.hist))
        mean_score = float(sum(self.score_hist) / max(1, len(self.score_hist)))
        peak_score = float(max(self.score_hist)) if self.score_hist else 0.0

        consecutive_positive = 0
        for v in reversed(self.hist):
            if bool(v):
                consecutive_positive += 1
            else:
                break

        sustained_ok = (hist_positive >= self.need_positive) and (mean_score >= self.min_mean_score)
        peak_ok = peak_score >= self.peak_score_thr
        streak_ok = consecutive_positive >= self.min_consecutive

        pose_ok = sustained_ok or peak_ok or streak_ok

        return PoseGateDecision(
            pose_ok=bool(pose_ok),
            hist_positive=hist_positive,
            current_positive=bool(positive),
            score=float(score),
            mean_score=mean_score,
            peak_score=peak_score,
            consecutive_positive=consecutive_positive,
        )