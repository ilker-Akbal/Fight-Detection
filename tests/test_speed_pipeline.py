from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

from HizTespiti.speed.src.calibration_loader import LoadedCalibration, load_calibration
from HizTespiti.speed.src.evidence_writer import EvidenceWriter, FrameBuffer
from HizTespiti.speed.src.speed_config import EvidenceConfig, SpeedConfig
from HizTespiti.speed.src.speed_estimator import SpeedEstimator, SpeedResult
from HizTespiti.speed.src.violation_decider import ViolationDecider
from HizTespiti.speed_mp.config import load_mp_config
from HizTespiti.yolo.src.simple_tracker import SimpleIoUTracker, Track, iou_xyxy
from HizTespiti.yolo.src.vehicle_detector import VehicleDetection, VehicleDetector


pytestmark = pytest.mark.unit


def detection(x1=0, y1=0, x2=20, y2=20, conf=0.9, name="car"):
    return VehicleDetection((x1, y1, x2, y2), conf, 2, name)


def speed_cfg(**overrides):
    values = dict(
        min_track_points=3,
        min_time_delta_sec=0.1,
        max_time_delta_sec=5.0,
        smooth_window=6,
        min_valid_speed_kmh=1.0,
        max_valid_speed_kmh=150.0,
        confirm_frames=2,
        cooldown_sec=5.0,
    )
    values.update(overrides)
    return SpeedConfig(**values)


def calibration(mode="two_line_time_gate", **overrides):
    values = dict(
        camera_id="cam",
        speed_limit_kmh=30.0,
        tolerance_kmh=5.0,
        measurement_mode=mode,
        direction="A_TO_B",
        line_a=[[-10, 0], [10, 0]],
        line_b=[[-10, 10], [10, 10]],
        distance_m=10.0,
        road_roi_enabled=False,
        road_roi_polygon=[],
        meter_per_pixel=1.0,
        scale_confidence=1.0,
        user_corrected=True,
        ready=True,
        ready_reason="ok",
        raw={},
    )
    values.update(overrides)
    return LoadedCalibration(**values)


def test_tracker_creation_continuation_multiple_and_expiration():
    tracker = SimpleIoUTracker(iou_threshold=0.2, max_age=1, min_hits=2)
    assert tracker.update([detection()], 1) == []
    active = tracker.update([detection(1, 0, 21, 20)], 2)
    assert len(active) == 1 and active[0].track_id == 1
    assert active[0].hits == 2 and len(active[0].history) == 2

    tracker.update([detection(100, 0, 120, 20, name="truck")], 3)
    assert len(tracker.tracks) == 2
    tracker.update([], 4)
    assert all(track.missed <= 1 for track in tracker.tracks)
    tracker.update([], 5)
    assert tracker.tracks == []
    assert iou_xyxy((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou_xyxy((0, 0, 0, 0), (0, 0, 0, 0)) == 0.0


def test_vehicle_detector_filters_classes_and_empty_results_without_loading_model():
    detector = object.__new__(VehicleDetector)
    detector.vehicle_class_ids = {2}
    detector.names = {0: "person", 2: "car"}
    detector.cfg = SimpleNamespace(imgsz=640, conf=0.3, iou=0.5, device="cpu")

    def box(cls_id, confidence, coords):
        scalar = lambda value: SimpleNamespace(item=lambda: value)
        xyxy = SimpleNamespace(
            __getitem__=lambda self, index: self,
            detach=lambda: xyxy,
            cpu=lambda: xyxy,
            tolist=lambda: coords,
        )
        # Python special lookup does not use instance __getitem__.
        class Xyxy:
            def __getitem__(self, index):
                return self
            def detach(self):
                return self
            def cpu(self):
                return self
            def tolist(self):
                return coords
        return SimpleNamespace(cls=scalar(cls_id), conf=scalar(confidence), xyxy=Xyxy())

    detector.model = SimpleNamespace(
        predict=lambda **kwargs: [SimpleNamespace(boxes=[box(0, .99, [0, 0, 1, 1]), box(2, .8, [1, 2, 3, 4])])]
    )
    found = detector.detect(np.zeros((4, 4, 3), dtype=np.uint8))
    assert len(found) == 1 and found[0].cls_name == "car" and found[0].conf == .8
    detector.model.predict = lambda **kwargs: []
    assert detector.detect(np.zeros((2, 2, 3), dtype=np.uint8)) == []


def test_calibration_valid_missing_invalid_and_polygon_normalization(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({
        "camera_id": "cam-1",
        "speed_limit_kmh": 50,
        "tolerance_kmh": 5,
        "measurement": {
            "mode": "two_line_time_gate", "direction": "AUTO",
            "line_a": [[0, 0], [10, 0]], "line_b": [[0, 10], [10, 10]],
            "distance_m": 12,
        },
        "road_roi": {"enabled": True, "polygon": [[0, 0], [10, 0], [5, 8]]},
    }), encoding="utf-8")
    loaded = load_calibration(path)
    assert loaded.ready and loaded.direction == "AUTO"
    assert loaded.road_roi_enabled and loaded.distance_m == 12

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"measurement": {"mode": "two_line_time_gate"}}))
    assert not load_calibration(bad).ready
    with pytest.raises(FileNotFoundError):
        load_calibration(tmp_path / "missing.json")


def test_two_line_speed_known_trajectory_and_cleanup():
    estimator = SpeedEstimator(speed_cfg(), calibration(), fps=10)
    track = Track(1, (0, 0, 2, 2), "car", .9, history=[(0, 0, -1), (2, 0, 1)])
    assert estimator.estimate(track).reason == "waiting_second_line"
    track.history.extend([(10, 0, 9), (12, 0, 11)])
    measured = estimator.estimate(track)
    assert measured.valid and measured.speed_kmh == pytest.approx(36.0)
    assert estimator.estimate(track).speed_kmh == pytest.approx(36.0)
    estimator.cleanup(set())
    assert estimator._states == {}


def test_pixel_scale_zero_constant_speed_and_invalid_time_window():
    estimator = SpeedEstimator(speed_cfg(), calibration("pixel_scale"), fps=10)
    moving = Track(1, (0, 0, 1, 1), "car", .9, history=[(0, 0, 0), (5, 5, 0), (10, 10, 0)])
    assert estimator.estimate(moving).speed_kmh == pytest.approx(36.0)
    still = Track(2, (0, 0, 1, 1), "car", .9, history=[(0, 0, 0), (5, 0, 0), (10, 0, 0)])
    assert estimator.estimate(still).reason == "speed_too_low"
    short = Track(3, (0, 0, 1, 1), "car", .9, history=[(0, 0, 0)])
    assert estimator.estimate(short).reason == "not_enough_points"
    slow_cfg = speed_cfg(max_time_delta_sec=.5)
    assert SpeedEstimator(slow_cfg, calibration("pixel_scale"), fps=10).estimate(moving).reason == "time_delta_too_high"


def test_violation_boundary_confirmation_cooldown_and_cleanup(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("HizTespiti.speed.src.violation_decider.time.time", lambda: now[0])
    decider = ViolationDecider(50, 10, confirm_frames=2, cooldown_sec=5)
    at_limit = decider.update(SpeedResult(1, 60.0, True, "ok", 4))
    assert not at_limit.violation and at_limit.reason == "under_limit"
    first = decider.update(SpeedResult(1, 61.0, True, "ok", 4))
    assert first.violation and not first.should_report
    second = decider.update(SpeedResult(1, 61.0, True, "ok", 4))
    assert second.should_report
    assert decider.update(SpeedResult(1, 61.0, True, "ok", 4)).reason == "cooldown"
    now[0] += 6
    assert decider.update(SpeedResult(1, 61.0, True, "ok", 4)).should_report
    decider.cleanup(set())
    assert decider.states == {}


def test_evidence_snapshot_metadata_jsonl_and_frame_buffer(tmp_path):
    cfg = EvidenceConfig(True, False, 1, 1, 90)
    writer = EvidenceWriter(tmp_path, "cam", cfg, fps=10)
    buffer = FrameBuffer(2)
    for index in range(3):
        buffer.add(index, np.full((8, 8, 3), index, dtype=np.uint8))
    assert [idx for idx, _ in buffer.get_from(1)] == [1, 2]
    track = Track(7, (1, 2, 6, 7), "car", .9)
    event = writer.save_event(3, .3, np.zeros((8, 8, 3), dtype=np.uint8), track, 70, 50, 10, 60, buffer)
    assert event.snapshot_path and (tmp_path / "snapshots").is_dir()
    assert event.clip_path is None and event.box_xyxy == [1, 2, 6, 7]
    row = json.loads(writer.events_path.read_text().strip())
    assert row["track_id"] == 7 and row["speed_kmh"] == 70


@pytest.mark.xfail(reason="existing EvidenceWriter reports a snapshot path even when cv2.imwrite fails")
def test_evidence_write_failure_does_not_claim_snapshot(tmp_path, monkeypatch):
    writer = EvidenceWriter(tmp_path, "cam", EvidenceConfig(True, False, 1, 1, 90), fps=10)
    monkeypatch.setattr("HizTespiti.speed.src.evidence_writer.cv2.imwrite", lambda *args, **kwargs: False)
    event = writer.save_event(
        1, .1, np.zeros((2, 2, 3), dtype=np.uint8), Track(1, (0, 0, 1, 1), "car", .9),
        70, 50, 10, 60, FrameBuffer(2),
    )
    assert event.snapshot_path is None


def test_speed_mp_config_parses_types_and_rejects_empty_camera_list(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({
        "run_name": "run",
        "output_dir": str(tmp_path),
        "cameras": [{
            "camera_id": "cam", "source": "video.mp4", "speed_limit_kmh": "55.5",
            "roi_enabled": "yes", "roi_polygon": "[[1,2],[3,4],[5,6]]",
            "save_snapshot": "false",
        }],
    }))
    cfg = load_mp_config(path)
    assert cfg.cameras[0].speed_limit_kmh == 55.5
    assert cfg.cameras[0].roi_enabled and not cfg.cameras[0].save_snapshot
    assert cfg.cameras[0].roi_polygon == [[1, 2], [3, 4], [5, 6]]
    path.write_text(json.dumps({"cameras": []}))
    with pytest.raises(RuntimeError):
        load_mp_config(path)

