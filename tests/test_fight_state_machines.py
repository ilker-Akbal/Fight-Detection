from __future__ import annotations

import queue
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from fight.motion.src.motion.frame_diff import FrameDiffer
from fight.motion.src.motion.gate import MotionGate
from fight.pipeline.pair_selector import LivePairRoiController, select_best_pair_live, union_pair_box
from fight.pipeline.person_stabilizer import TemporalPersonStabilizer
from fight.pipeline.utils import crop_from_box, sanitize_box
from fight.pipeline_mp.camera_worker import CameraProcessRunner
from fight.pipeline_mp.messages import IncidentLifecycleMessage, ReportMessage, Stage3Job
from fight.pose.src.pose_gate import PoseGate


pytestmark = pytest.mark.unit


def test_frame_differ_no_motion_motion_and_reset():
    differ = FrameDiffer()
    black = np.zeros((8, 8), dtype=np.uint8)
    white = np.full((8, 8), 255, dtype=np.uint8)

    assert differ.compute(black).score == 0.0
    assert differ.compute(black).score == 0.0
    assert differ.compute(white).score == 255.0
    differ.reset()
    assert differ.compute(white).score == 0.0


def test_motion_gate_threshold_boundaries_hold_close_and_reset():
    gate = MotionGate(0.5, 0.2, window_size=3, min_pass=2, off_run=2)
    assert not gate.decide(0.5).pass_frame
    opened = gate.decide(0.5)
    assert opened.pass_frame and opened.motion_on_after
    assert gate.decide(0.19).pass_frame
    closed = gate.decide(0.19)
    assert not closed.pass_frame and closed.reason.startswith("CLOSE")
    gate.decide(1.0)
    gate.reset()
    assert not gate.motion_on and list(gate.history) == []


def test_person_stabilizer_matching_smoothing_aging_and_capacity():
    stabilizer = TemporalPersonStabilizer(
        max_age=1, min_hits=2, iou_match_thr=0.2, conf_alpha=0.5, max_tracks=2
    )
    assert stabilizer.update([(0.8, (10, 10, 40, 60))]) == []
    stable = stabilizer.update([(1.0, (12, 10, 42, 60))])
    assert len(stable) == 1
    assert stable[0][0] == pytest.approx(0.9)
    assert stabilizer.tracks[0]["id"] == 1

    stabilizer.update([(0.7, (100, 10, 130, 60)), (0.6, (180, 10, 210, 60))])
    assert len(stabilizer.tracks) == 2
    stabilizer.predict_only()
    stabilizer.predict_only()
    assert stabilizer.tracks == []
    stabilizer.reset()
    assert stabilizer.next_id == 1


def test_pair_selection_roi_clipping_hold_and_reset():
    shape = (360, 480, 3)
    persons = [(0.95, (130, 60, 210, 300)), (0.94, (200, 62, 280, 302))]
    pair, score, roi, boxes = select_best_pair_live(persons, shape)
    assert pair == (0, 1) and score > 0
    assert roi is not None and boxes is not None
    clipped = union_pair_box((-20, -10, 40, 100), (30, 0, 100, 120), shape)
    assert clipped[0] >= 0 and clipped[1] >= 0
    assert clipped[2] < shape[1] and clipped[3] < shape[0]
    assert select_best_pair_live(persons[:1], shape)[0] is None

    controller = LivePairRoiController(
        enter_score=0.0,
        keep_score=0.0,
        keep_frames=2,
        min_hits_to_activate=1,
        candidate_confirm_frames=1,
    )
    active = controller.update(persons, shape)
    assert active["pair_ok"] == 1 and active["roi_ok"] == 1
    assert controller.update([], shape)["roi_ok"] == 1
    assert controller.update([], shape)["roi_ok"] == 1
    reset = controller.update([], shape)
    assert reset["roi_ok"] == 0 and reset["pair_idx"] is None


def test_box_and_crop_reject_invalid_and_stay_inside_frame():
    frame = np.zeros((100, 120, 3), dtype=np.uint8)
    assert sanitize_box((50, 50, 40, 80), frame.shape) is None
    assert crop_from_box(frame, (50, 50, 40, 80)) is None
    crop = crop_from_box(frame, (-20, -10, 200, 150), out_size=64)
    assert crop.shape == (64, 64, 3)


def test_pose_gate_requires_temporal_evidence_and_resets():
    gate = PoseGate(
        window_size=4,
        need_positive=2,
        min_mean_score=0.4,
        peak_score_thr=0.9,
        min_consecutive=2,
    )
    assert not gate.update(0.5, True).pose_ok
    assert not gate.update(0.1, False).pose_ok
    assert gate.update(0.7, True).pose_ok
    gate.reset()
    assert not gate.update(0.89, False).pose_ok
    assert gate.update(0.9, False).pose_ok


def _event_runner(tmp_path: Path) -> CameraProcessRunner:
    runner = object.__new__(CameraProcessRunner)
    runner.camera_id = "cam-1"
    runner.source = "file.mp4"
    runner.source_is_file = True
    runner.runtime = {
        "prebuffer_frames": 3,
        "max_event_frames": 3,
        "use_stage3": True,
        "min_queue_frames": 1,
        "stage3_event_min_duration_sec": 0.0,
        "stage3_enqueue_timeout_sec": 0.01,
        "event_close_grace_frames": 2,
    }
    runner.stage3_queue = queue.Queue()
    runner.report_queue = queue.Queue()
    runner.paths = SimpleNamespace(temp_segments_dir=tmp_path)
    runner.event_counter = 0
    runner.event_chain_open = False
    runner.frame_idx = 10
    runner.last_event_close_frame_idx = -100
    runner.last_event_close_ts = 0.0
    runner.capture_fps = 10.0
    runner.clip_write_fps = 10.0
    runner.source_timeline_base_ts = 1000.0
    runner.active_event = None
    runner.prebuffer = deque(maxlen=3)
    runner.report_status = lambda *args, **kwargs: None
    runner.save_clip = lambda frames, path: path.write_bytes(b"clip")
    return runner


def test_event_segmentation_keeps_chain_open_and_prebuffer_is_not_reused(tmp_path):
    runner = _event_runner(tmp_path)
    frames = [np.full((2, 2, 3), i, dtype=np.uint8) for i in range(4)]
    runner.new_event(1001.0, frames, 0.8, "pose")
    opened = runner.stage3_queue.get_nowait()
    assert isinstance(opened, IncidentLifecycleMessage)
    assert opened.action == "event_chain_opened"
    assert len(runner.active_event.frames) == 3

    runner.append_event(frames[-1], 1001.1, 0.8, True)
    assert runner.active_event is None
    job = runner.stage3_queue.get_nowait()
    assert isinstance(job, Stage3Job)
    assert runner.event_chain_open
    assert runner.stage3_queue.empty()  # max_event_frames is not chain_closed

    runner.frame_idx += 1
    runner.new_event(1001.2, frames, 0.8, "pose")
    assert runner.active_event.frames == []  # prior segment tail is not reused
    runner.append_event(frames[-1], 1001.3, 0.8, True)
    runner.close_event("source_eof")
    assert isinstance(runner.stage3_queue.get_nowait(), Stage3Job)
    closed = runner.stage3_queue.get_nowait()
    assert isinstance(closed, IncidentLifecycleMessage)
    assert closed.action == "event_chain_closed"


def test_segmented_chain_closes_after_real_grace_without_new_segment(tmp_path):
    runner = _event_runner(tmp_path)
    runner.event_chain_open = True
    runner.last_event_close_frame_idx = 5
    runner.frame_idx = 6
    assert not runner._close_active_if_grace_expired("motion_missing")
    runner.frame_idx = 7
    assert runner._close_active_if_grace_expired("motion_missing")
    assert runner.stage3_queue.get_nowait().action == "event_chain_closed"


def test_stage3_queue_full_marks_segment_dropped_and_closes_chain(tmp_path):
    class SelectiveQueue(queue.Queue):
        def put(self, item, block=True, timeout=None):
            if isinstance(item, Stage3Job):
                raise queue.Full
            return super().put(item, block=block, timeout=timeout)

    runner = _event_runner(tmp_path)
    runner.stage3_queue = SelectiveQueue()
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    runner.new_event(1001.0, [frame], .8, "pose")
    runner.append_event(frame, 1001.1, .8, True)
    runner.close_event("source_eof")
    lifecycle = list(runner.stage3_queue.queue)
    assert [item.action for item in lifecycle] == ["event_chain_opened", "event_chain_closed"]
    events = [item for item in list(runner.report_queue.queue) if isinstance(item, ReportMessage)]
    assert events[0].row["queue_status"] == "dropped"
    assert events[0].row["queue_reason"] == "stage3_queue_full"
