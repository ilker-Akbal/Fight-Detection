"""Phase 25.1: a temp encoder failure is not an inference-consumer crash."""
import json
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from fight.pipeline.evidence_metadata import compact_evidence_name, source_time_fields
from fight.pipeline_mp.camera_worker import CameraProcessRunner
from fight.pipeline_mp.common import ts_to_str
from fight.pipeline_mp.messages import ActiveEvent, CameraFrame, CameraIngestSignal
from fight.pipeline_mp.performance import BoundedMetricCollector


CID = "offline_e0bf230f422d4b8ebfeb0a6e469e47c3"


def runner_at_event(tmp_path):
    runner = CameraProcessRunner.__new__(CameraProcessRunner)
    runner.camera_id, runner.generation, runner.consumer_epoch, runner.slot_id = CID, 1, 1, 0
    runner.source_public = "historical.mp4"
    runner.source_is_file, runner.capture_fps, runner.clip_write_fps = True, 30.014, 30.014
    runner.frame_idx, runner.event_counter = 299, 1
    runner.last_event_close_frame_idx = -10000
    runner.runtime = {"use_stage3": True}
    runner.paths = SimpleNamespace(temp_segments_dir=tmp_path / "temp_segments")
    runner.report_status = Mock()
    runner.report_queue, runner.stage3_queue = queue.Queue(), queue.Queue()
    runner.stop_event = threading.Event()
    runner.health = None
    runner.prebuffer = deque()
    runner.counters = {"events_closed": 0, "events_opened": 1, "stage3_jobs_submitted": 0, "frames_read": 0}
    runner.active_event = ActiveEvent(CID + "_g1_f1_000001", CID, "historical.mp4", 5.53, 9.996,
        299, frames=[np.zeros((4, 4, 3), dtype=np.uint8) for _ in range(4)], pose_scores=[.9], positive_hits=4)
    runner._should_queue_stage3 = Mock(return_value=(True, "stage3_queue"))
    return runner


def test_compact_filename_full_identity_safe_and_deep_path(tmp_path):
    event = CID + "_g1_f1_000001"
    name = compact_evidence_name(CID, event, 5.53, generation=1, consumer_epoch=1)
    assert name == compact_evidence_name(CID, event, 5.53, generation=1, consumer_epoch=1)
    assert re.fullmatch(r"evt_[0-9a-f]{32}_g1_f1_000001_v5530\.mp4", name)
    assert CID not in name and len(name) < 75
    # Same directory depth as the reported 277-character path, platform-neutral.
    root = Path("C:/") / ("r" * 135) / "temp_segments"
    old = root / f"cam_{CID}__evt_{event}__19700101_030005_530.mp4"
    new = root / name
    assert len(str(old)) > 260 and len(str(new)) < 240
    variants = [compact_evidence_name(cid, eid, 5.53, generation=g, consumer_epoch=f)
        for cid, eid, g, f in [(CID, event, 1, 1), (CID + "x", event, 1, 1),
                               (CID, event, 2, 1), (CID, event, 1, 2), (CID, event + "2", 1, 1)]]
    assert len(set(variants)) == len(variants)
    hostile = compact_evidence_name('CON<>:"/\\|?*', 'bad<>:"/\\|?*', time.time())
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", hostile)


def test_temp_failure_keeps_frames_and_reaches_all_903_file_frames(tmp_path, monkeypatch):
    runner = runner_at_event(tmp_path)
    monkeypatch.setattr("fight.pipeline_mp.camera_worker.save_clip_mp4", Mock(side_effect=OSError("encoder failed")))
    runner.centralized_ingest = True
    runner.frame_queue = queue.Queue()
    runner.frame_age_ms = BoundedMetricCollector(enabled=False)
    runner.last_ingest_frame_seq, runner.source_frame_count = 0, 903
    for sequence in range(1, 904):
        runner.frame_queue.put(CameraFrame(CID, 1, sequence, time.perf_counter(), time.time(),
            np.zeros((4, 4, 3), dtype=np.uint8), source_fps=30.014, source_frame_count=903))
    runner.frame_queue.put(CameraIngestSignal(CID, 1, "eof", frame_seq=903))
    runner.process_frame = lambda frame: runner.close_event("max_event_frames") if runner.counters["frames_read"] == 300 else None
    monkeypatch.setattr("fight.pipeline_mp.camera_worker.open_source", Mock(side_effect=AssertionError("second opener")))
    runner.run_loop()
    assert runner.counters["frames_read"] == 903
    assert runner.counters["stage3_jobs_submitted"] == 1
    job = runner.stage3_queue.get_nowait()
    assert len(job.frames) == 4 and job.frames[0].shape == (4, 4, 3)
    assert job.clip_path == "" and job.evidence_error == "evidence_write_failed"
    assert job.slot_id == 0 and job.generation == 1
    failure = next(call.args[2] for call in runner.report_status.call_args_list if call.args[1] == "save_failed")
    assert failure["reason"] == "evidence_write_failed" and not failure["clip_path"]
    assert any(call.args[1] == "eof" for call in runner.report_status.call_args_list)
    event = runner.report_queue.get_nowait().row
    assert event["event_start"] is None and event["source_start_time_sec"] == 5.53
    assert "1970-01-01" not in json.dumps(event)


def test_stage3_infers_memory_and_incident_records_specific_failure(tmp_path, monkeypatch):
    from fight.pipeline_mp import stage3_worker, incident_worker
    from fight.pipeline_mp.offline_jobs import OfflineDrain, drain_ack_path
    runner = runner_at_event(tmp_path)
    runner.save_clip = Mock(side_effect=OSError("encoder failed"))
    runner.close_event("max_event_frames")
    job = runner.stage3_queue.queue[0]
    runner.stage3_queue.put(None)
    config = {"output_dir": str(tmp_path), "models": {"stage3_config": "fake"},
              "runtime": {"incident_outbox_path": str(tmp_path / "outbox.jsonl")}}
    detector = SimpleNamespace(infer=Mock(return_value=.95))
    monkeypatch.setattr(stage3_worker, "Stage3Adapter", lambda _: detector)
    monkeypatch.setattr(stage3_worker, "configure_process_runtime", lambda **_: None)
    output = queue.Queue()
    stage3_worker.stage3_process_main(config, runner.stage3_queue, output, runner.report_queue, runner.stop_event, [1])
    assert detector.infer.call_args.args[0] is job.frames
    result = output.queue[0]
    assert result.fight_prob == .95 and result.clip_path == "" and result.evidence_error == "evidence_write_failed"
    agg = Mock()
    monkeypatch.setattr(incident_worker, "IncidentAggregator", Mock(return_value=agg))
    output.put(OfflineDrain(CID, 0, 1, 1))
    output.put(None)
    incident_worker.incident_process_main(config, output, runner.report_queue, runner.stop_event, [1])
    agg.submit.assert_not_called()
    failure = drain_ack_path(config, CID).with_suffix(".failed.json")
    assert json.loads(failure.read_text())["reason"] == "evidence_write_failed"
    assert drain_ack_path(config, CID).exists()
    assert not (tmp_path / "outbox.jsonl").exists()
    rows = [message.row for message in runner.report_queue.queue]
    assert any(row.get("jobs_completed") == 1 for row in rows)
    assert "1970-01-01" not in json.dumps(rows)


def test_successful_serialization_publishes_real_compact_path(tmp_path):
    runner = runner_at_event(tmp_path)
    def save(frames, path):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"serialized")
    runner.save_clip = save
    runner.close_event("source_eof")
    job = runner.stage3_queue.get_nowait()
    assert Path(job.clip_path).read_bytes() == b"serialized" and job.evidence_error == ""


def test_offline_started_time_and_live_wall_time_remain_distinct(tmp_path):
    runner = runner_at_event(tmp_path)
    runner.new_event(5.53, [], .9, "test")
    row = runner.report_status.call_args.args[2]
    assert row["event_start_ts"] is None and row["source_start_time_sec"] == 5.53
    assert row["now_ts"] is None and row["source_end_time_sec"] == 5.53
    stamp = 1750000005.53
    assert source_time_fields("live_camera", stamp, stamp + 1) == {
        "event_start": ts_to_str(stamp), "event_end": ts_to_str(stamp + 1)}
    runner.camera_id, runner.source_is_file = "live_camera", False
    from unittest.mock import patch
    with patch("fight.pipeline_mp.camera_worker.time.time", return_value=stamp):
        assert runner.timestamp_for_frame_idx(7) == stamp


def test_final_offline_evidence_metadata_does_not_format_video_position_as_epoch(tmp_path, monkeypatch):
    from fight.pipeline.incident_aggregator import IncidentAggregator, Stage3Result
    agg = IncidentAggregator(out_dir=tmp_path / "incidents", run_id="offline-test",
        outbox_path=tmp_path / "outbox.jsonl", single_strong_fight_thr=.5,
        clip_ready_wait_sec=0, stale_finalize_sec=60)
    def concatenate(parts, output):
        Path(output).write_bytes(b"evidence")
        return True
    overlay = Mock(return_value=True)
    monkeypatch.setattr(agg, "_concat_mp4s", concatenate)
    monkeypatch.setattr(agg, "_wait_clips_ready", lambda _: True)
    monkeypatch.setattr(agg, "_add_ai_overlay_to_clip", overlay)
    try:
        agg.submit(Stage3Result(camera_id=CID, source="historical.mp4", event_id=CID + "_g1_f1_000001",
            event_start_ts=5.53, event_end_ts=7.53, clip_path="part.mp4", fight_prob=.95,
            fight_label="fight", pose_score_max=.9, pose_score_mean=.8))
        agg.finalize(CID, force=True)
    finally:
        agg.close_all()
    row = json.loads((tmp_path / "incidents.jsonl").read_text().splitlines()[0])
    outbox = json.loads((tmp_path / "outbox.jsonl").read_text().splitlines()[0])
    assert row["start_ts"] is None and row["start_ts_epoch"] is None
    assert row["source_start_time_sec"] == 5.53
    assert outbox["video_time_sec"] == 5.53 and not outbox["detected_at"].startswith("1970")
    assert overlay.call_args.args[1]["start_ts"] == "video +5.530s"
    assert "1970" not in json.dumps(row)
