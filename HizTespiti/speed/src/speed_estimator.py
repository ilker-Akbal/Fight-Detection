from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from HizTespiti.yolo.src.simple_tracker import Track

from .calibration_loader import LoadedCalibration
from .speed_config import SpeedConfig


@dataclass
class SpeedResult:
    track_id: int
    speed_kmh: float | None
    valid: bool
    reason: str
    points_used: int


@dataclass
class _TrackGateState:
    stage: str = "waiting_first"
    first_line_name: str | None = None
    second_line_name: str | None = None
    first_cross_frame: float | None = None
    second_cross_frame: float | None = None
    speed_kmh: float | None = None
    done_frame: float | None = None
    last_processed_frame: int = -1


def _line_len(line: list[list[int]]) -> float:
    if len(line) != 2:
        return 0.0

    x1, y1 = line[0]
    x2, y2 = line[1]

    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


def _signed_distance_to_line(point: tuple[float, float], line: list[list[int]]) -> float:
    """
    Noktanın çizgiye göre işaretli uzaklığı.
    Sıfırın işaret değiştirmesi çizginin geçildiğini gösterir.
    """
    px, py = point
    x1, y1 = line[0]
    x2, y2 = line[1]

    dx = x2 - x1
    dy = y2 - y1
    denom = math.sqrt(dx * dx + dy * dy)

    if denom <= 1e-6:
        return 0.0

    return ((dx * (py - y1)) - (dy * (px - x1))) / denom


def _crossing_frame(
    p1: tuple[float, float],
    p2: tuple[float, float],
    f1: float,
    f2: float,
    line: list[list[int]],
) -> float | None:
    d1 = _signed_distance_to_line(p1, line)
    d2 = _signed_distance_to_line(p2, line)

    eps = 1e-6

    if abs(d1) < eps:
        return float(f1)

    if abs(d2) < eps:
        return float(f2)

    if d1 * d2 > 0:
        return None

    ratio = abs(d1) / max(abs(d1) + abs(d2), eps)
    return float(f1 + ratio * (f2 - f1))


class SpeedEstimator:
    """
    Ürün modu:
    - two_line_time_gate:
      Araç önce başlangıç çizgisini, sonra bitiş çizgisini geçer.
      Hız = gerçek mesafe / süre.

    Geriye dönük destek:
    - pixel_scale:
      Eski meter_per_pixel hesabı.
    """

    def __init__(
        self,
        cfg: SpeedConfig,
        calibration: LoadedCalibration,
        fps: float,
    ):
        self.cfg = cfg
        self.calibration = calibration
        self.fps = max(float(fps), 1.0)
        self._states: dict[int, _TrackGateState] = {}

    def estimate(self, track: Track) -> SpeedResult:
        mode = str(self.calibration.measurement_mode or "two_line_time_gate").lower()

        if mode == "two_line_time_gate":
            return self._estimate_two_line(track)

        if mode == "pixel_scale":
            return self._estimate_pixel_scale(track)

        return SpeedResult(track.track_id, None, False, f"unsupported_mode:{mode}", 0)

    def cleanup(self, active_track_ids: set[int]) -> None:
        stale = [tid for tid in self._states if tid not in active_track_ids]

        for tid in stale:
            self._states.pop(tid, None)

    def _estimate_two_line(self, track: Track) -> SpeedResult:
        if not self.calibration.ready:
            return SpeedResult(track.track_id, None, False, self.calibration.ready_reason, 0)

        if self.calibration.distance_m is None or self.calibration.distance_m <= 0:
            return SpeedResult(track.track_id, None, False, "missing_distance", 0)

        if _line_len(self.calibration.line_a) <= 1:
            return SpeedResult(track.track_id, None, False, "invalid_line_a", 0)

        if _line_len(self.calibration.line_b) <= 1:
            return SpeedResult(track.track_id, None, False, "invalid_line_b", 0)

        hist = list(track.history)

        if len(hist) < 2:
            return SpeedResult(track.track_id, None, False, "not_enough_points", len(hist))

        state = self._states.setdefault(track.track_id, _TrackGateState())

        direction = str(self.calibration.direction or "A_TO_B").upper()

        if direction == "B_TO_A":
            first_name = "B"
            first_line = self.calibration.line_b
            second_name = "A"
            second_line = self.calibration.line_a
        elif direction == "AUTO":
            first_name = None
            first_line = None
            second_name = None
            second_line = None
        else:
            first_name = "A"
            first_line = self.calibration.line_a
            second_name = "B"
            second_line = self.calibration.line_b

        # Daha önce hız hesaplandıysa birkaç frame daha aynı sonucu döndür.
        # Böylece ViolationDecider confirm_frames ile raporu kaçırmaz.
        if state.speed_kmh is not None and state.done_frame is not None:
            current_frame = int(hist[-1][0])
            emit_frames = max(int(self.cfg.confirm_frames) + 3, 5)

            if current_frame <= state.done_frame + emit_frames:
                return self._validate_speed(
                    track_id=track.track_id,
                    speed_kmh=state.speed_kmh,
                    points_used=len(hist),
                )

            return SpeedResult(track.track_id, state.speed_kmh, False, "already_measured", len(hist))

        # Son iki nokta üzerinden çizgi geçişi yakala.
        f1, x1, y1 = hist[-2]
        f2, x2, y2 = hist[-1]

        if int(f2) <= state.last_processed_frame:
            return SpeedResult(track.track_id, None, False, "already_processed_frame", len(hist))

        state.last_processed_frame = int(f2)

        p1 = (float(x1), float(y1))
        p2 = (float(x2), float(y2))

        if direction == "AUTO":
            return self._estimate_auto_direction(
                track=track,
                state=state,
                p1=p1,
                p2=p2,
                f1=float(f1),
                f2=float(f2),
                points_used=len(hist),
            )

        if state.stage == "waiting_first":
            cross = _crossing_frame(p1, p2, float(f1), float(f2), first_line)

            if cross is not None:
                state.stage = "waiting_second"
                state.first_line_name = first_name
                state.second_line_name = second_name
                state.first_cross_frame = cross

            return SpeedResult(track.track_id, None, False, "waiting_second_line", len(hist))

        if state.stage == "waiting_second":
            cross = _crossing_frame(p1, p2, float(f1), float(f2), second_line)

            if cross is None:
                return SpeedResult(track.track_id, None, False, "waiting_second_line", len(hist))

            state.second_cross_frame = cross
            frame_delta = state.second_cross_frame - float(state.first_cross_frame or 0)

            if frame_delta <= 0:
                return SpeedResult(track.track_id, None, False, "invalid_crossing_order", len(hist))

            time_delta = frame_delta / self.fps

            if time_delta < self.cfg.min_time_delta_sec:
                return SpeedResult(track.track_id, None, False, "time_delta_too_low", len(hist))

            if time_delta > self.cfg.max_time_delta_sec:
                return SpeedResult(track.track_id, None, False, "time_delta_too_high", len(hist))

            speed_mps = float(self.calibration.distance_m) / max(time_delta, 1e-6)
            speed_kmh = speed_mps * 3.6

            state.speed_kmh = float(speed_kmh)
            state.done_frame = float(f2)
            state.stage = "done"

            return self._validate_speed(
                track_id=track.track_id,
                speed_kmh=speed_kmh,
                points_used=len(hist),
            )

        return SpeedResult(track.track_id, None, False, state.stage, len(hist))

    def _estimate_auto_direction(
        self,
        track: Track,
        state: _TrackGateState,
        p1: tuple[float, float],
        p2: tuple[float, float],
        f1: float,
        f2: float,
        points_used: int,
    ) -> SpeedResult:
        cross_a = _crossing_frame(p1, p2, f1, f2, self.calibration.line_a)
        cross_b = _crossing_frame(p1, p2, f1, f2, self.calibration.line_b)

        if state.stage == "waiting_first":
            if cross_a is not None:
                state.stage = "waiting_second"
                state.first_line_name = "A"
                state.second_line_name = "B"
                state.first_cross_frame = cross_a
                return SpeedResult(track.track_id, None, False, "waiting_line_b", points_used)

            if cross_b is not None:
                state.stage = "waiting_second"
                state.first_line_name = "B"
                state.second_line_name = "A"
                state.first_cross_frame = cross_b
                return SpeedResult(track.track_id, None, False, "waiting_line_a", points_used)

            return SpeedResult(track.track_id, None, False, "waiting_first_line", points_used)

        if state.stage == "waiting_second":
            if state.second_line_name == "A":
                cross = cross_a
            else:
                cross = cross_b

            if cross is None:
                return SpeedResult(track.track_id, None, False, "waiting_second_line", points_used)

            state.second_cross_frame = cross
            frame_delta = state.second_cross_frame - float(state.first_cross_frame or 0)

            if frame_delta <= 0:
                return SpeedResult(track.track_id, None, False, "invalid_crossing_order", points_used)

            time_delta = frame_delta / self.fps

            if time_delta < self.cfg.min_time_delta_sec:
                return SpeedResult(track.track_id, None, False, "time_delta_too_low", points_used)

            if time_delta > self.cfg.max_time_delta_sec:
                return SpeedResult(track.track_id, None, False, "time_delta_too_high", points_used)

            speed_mps = float(self.calibration.distance_m) / max(time_delta, 1e-6)
            speed_kmh = speed_mps * 3.6

            state.speed_kmh = float(speed_kmh)
            state.done_frame = float(f2)
            state.stage = "done"

            return self._validate_speed(track.track_id, speed_kmh, points_used)

        return SpeedResult(track.track_id, None, False, state.stage, points_used)

    def _estimate_pixel_scale(self, track: Track) -> SpeedResult:
        meter_per_pixel = self.calibration.meter_per_pixel

        if meter_per_pixel is None or meter_per_pixel <= 0:
            return SpeedResult(track.track_id, None, False, "missing_scale", 0)

        hist = track.history[-self.cfg.smooth_window:]

        if len(hist) < self.cfg.min_track_points:
            return SpeedResult(track.track_id, None, False, "not_enough_points", len(hist))

        f1, x1, y1 = hist[0]
        f2, x2, y2 = hist[-1]

        frame_delta = max(1, int(f2 - f1))
        time_delta = frame_delta / self.fps

        if time_delta < self.cfg.min_time_delta_sec:
            return SpeedResult(track.track_id, None, False, "time_delta_too_low", len(hist))

        if time_delta > self.cfg.max_time_delta_sec:
            return SpeedResult(track.track_id, None, False, "time_delta_too_high", len(hist))

        pixel_dist = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
        meter_dist = pixel_dist * meter_per_pixel

        speed_mps = meter_dist / max(time_delta, 1e-6)
        speed_kmh = speed_mps * 3.6

        return self._validate_speed(track.track_id, speed_kmh, len(hist))

    def _validate_speed(self, track_id: int, speed_kmh: float, points_used: int) -> SpeedResult:
        if speed_kmh < self.cfg.min_valid_speed_kmh:
            return SpeedResult(track_id, speed_kmh, False, "speed_too_low", points_used)

        if speed_kmh > self.cfg.max_valid_speed_kmh:
            return SpeedResult(track_id, speed_kmh, False, "speed_too_high", points_used)

        return SpeedResult(track_id, float(speed_kmh), True, "ok", points_used)