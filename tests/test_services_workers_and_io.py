from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from django.test import override_settings

from services.pipeline_bridge import fight_runner
from services.pipeline_bridge.report_reader import (
    build_dashboard_report,
    read_jsonl,
)
from services.pipeline_bridge.runtime_state import PipelineRuntime
from services.speed_bridge import speed_runner
from services.speed_bridge.report_reader import (
    build_speed_dashboard_report,
)
from services.speed_bridge.runtime_state import SpeedPipelineRuntime
from fight.pipeline import clip_buffer
from fight.pipeline_mp.common import MpPaths, queue_put_drop_oldest
from fight.pipeline_mp.messages import (
    IncidentLifecycleMessage,
    ReportMessage,
    Stage3Job,
    Stage3ResultMessage,
)
from fight.pipeline_mp.reporter import BufferedJsonlWriter
from fight.pipeline_mp.stage3_worker import stage3_process_main


pytestmark = pytest.mark.unit


class FakeProcess:
    def __init__(self, return_code=None):
        self.return_code = return_code
        self.pid = 4321
        self.signals = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.return_code

    def send_signal(self, value):
        self.signals.append(value)
        self.return_code = 0

    def terminate(self):
        self.terminated = True
        self.return_code = 0

    def kill(self):
        self.killed = True
        self.return_code = -9

    def wait(self, timeout=None):
        return self.return_code


def test_fight_bridge_config_command_start_and_stop(tmp_path, monkeypatch):
    defaults = {
        "motion_config": "motion.yaml", "yolo_config": "yolo.yaml",
        "yolo_weights": "yolo.pt", "pose_config": "pose.yaml",
        "stage3_config": "stage3.yaml", "person_worker_count": 2,
    }
    fake = FakeProcess()
    monkeypatch.setattr(fight_runner.time, "strftime", lambda value: "ui_run_fixed")
    monkeypatch.setattr(fight_runner.time, "sleep", lambda value: None)
    monkeypatch.setattr(fight_runner.subprocess, "Popen", lambda *args, **kwargs: fake)
    with override_settings(
        PIPELINE_DEFAULTS=defaults,
        PIPELINE_OUTPUT_BASE=tmp_path,
        REPO_ROOT=tmp_path,
    ):
        active = fight_runner.start_pipeline([{"camera_id": "cam", "source": "0"}])
    config = json.loads(active.config_path.read_text(encoding="utf-8"))
    assert config["cameras"][0]["camera_id"] == "cam"
    assert config["runtime"]["person_worker_count"] == 2
    assert fight_runner.build_command(active.config_path)[-2:] == ["--config", str(active.config_path)]
    fight_runner.stop_pipeline(active)
    assert fake.signals or fake.terminated


def test_fight_bridge_rejects_missing_source_and_early_process_exit(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="source"):
        fight_runner._normalize_sources([{"camera_id": "cam", "source": ""}])
    fake = FakeProcess(return_code=3)
    monkeypatch.setattr(fight_runner.time, "strftime", lambda value: "failed_run")
    monkeypatch.setattr(fight_runner.time, "sleep", lambda value: None)
    monkeypatch.setattr(fight_runner.subprocess, "Popen", lambda *args, **kwargs: fake)
    defaults = {
        "motion_config": "m", "yolo_config": "y", "yolo_weights": "w",
        "pose_config": "p", "stage3_config": "s",
    }
    with override_settings(PIPELINE_DEFAULTS=defaults, PIPELINE_OUTPUT_BASE=tmp_path, REPO_ROOT=tmp_path):
        with pytest.raises(RuntimeError, match="return_code=3"):
            fight_runner.start_pipeline([{"camera_id": "cam", "source": "0"}])


def test_speed_bridge_defaults_command_and_stop(tmp_path):
    with override_settings(SPEED_PIPELINE_DEFAULTS={}, SPEED_PIPELINE_ENTRY_MODULE="fake.speed"):
        config = speed_runner.build_run_config("run", tmp_path, [{"camera_id": "cam"}])
        assert config["runtime"]["vehicle_worker_count"] == 1
        assert config["tracker"]["iou_threshold"] == .25
        command = speed_runner.build_command(tmp_path / "run.json")
        assert command[1:3] == ["-m", "fake.speed"]
    fake = FakeProcess(return_code=0)
    active = speed_runner.ActiveSpeedRun(
        fake, "run", tmp_path, tmp_path / "config.json", [],
        tmp_path / "out", tmp_path / "err", 0,
    )
    speed_runner.stop_speed_pipeline(active)
    assert not fake.signals and not fake.terminated


def test_runtime_state_set_get_clear_and_concurrent_access():
    runtime = PipelineRuntime()
    speed_runtime = SpeedPipelineRuntime()
    values = list(range(20))
    threads = [threading.Thread(target=runtime.set, args=(value,)) for value in values]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert runtime.get() in values
    speed_runtime.set("active")
    assert speed_runtime.get() == "active"
    speed_runtime.clear()
    assert speed_runtime.get() is None


def test_report_readers_ignore_malformed_lines_and_merge_latest(tmp_path):
    run = tmp_path / "pipeline_runs" / "run-1"
    run.mkdir(parents=True)
    (run / "camera_status.jsonl").write_text(
        '{"camera_id":"cam","detail":"old"}\ninvalid\n'
        '{"camera_id":"cam","detail":"new","persons":"2"}\n',
        encoding="utf-8",
    )
    (run / "stage3_results.jsonl").write_text('{"camera_id":"cam","fight_prob":0.8}\n')
    clip = run / "incidents" / "clip.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"video")
    (run / "incidents.jsonl").write_text(json.dumps({
        "camera_id": "cam", "incident_id": "i1", "clip_path": str(clip), "part_count": 3,
    }) + "\n")
    assert len(read_jsonl(run / "camera_status.jsonl")) == 2
    report = build_dashboard_report(
        run, [{"camera_id": "cam", "source": "0"}], True, 1, 2, None, tmp_path
    )
    assert report["cameras"][0]["detail"] == "new"
    assert report["cameras"][0]["persons"] == 2
    assert report["recent_incidents"][0]["clip_name"] == "clip.mp4"

    speed_run = tmp_path / "speed_runs" / "speed-1"
    speed_run.mkdir(parents=True)
    (speed_run / "camera_status.jsonl").write_text(
        'bad\n{"camera_id":"cam","stage":"ready","tracks":2}\n'
    )
    (speed_run / "speed_events.jsonl").write_text(
        '{"camera_id":"cam","speed_kmh":70,"speed_limit_kmh":50,'
        '"tolerance_kmh":10,"threshold_kmh":60,"created_at":1}\n'
    )
    speed_report = build_speed_dashboard_report(
        speed_run, [{"camera_id": "cam"}], False, None, None, 0, tmp_path
    )
    assert speed_report["cameras"][0]["tracks"] == 2
    assert speed_report["events"][0]["speed_kmh"] == 70


def test_jsonl_writer_paths_and_bounded_drop_oldest(tmp_path):
    path = tmp_path / "nested" / "rows.jsonl"
    writer = BufferedJsonlWriter(path)
    writer.write({"a": 1})
    writer.close()
    assert json.loads(path.read_text()) == {"a": 1}
    q = queue.Queue(maxsize=1)
    q.put("old")
    assert queue_put_drop_oldest(q, "new")
    assert q.get_nowait() == "new"
    paths = MpPaths.from_output_dir(tmp_path / "run")
    paths.mkdirs()
    assert paths.incidents_dir.is_dir() and paths.previews_dir.is_dir()


def test_clip_buffer_empty_resize_and_failure_cleanup(tmp_path, monkeypatch):
    target = tmp_path / "clip.mp4"
    assert clip_buffer.save_clip_mp4([], str(target), 10) is None
    frames = [np.zeros((10, 12, 3), dtype=np.uint8), np.zeros((8, 9, 3), dtype=np.uint8)]
    calls = []
    monkeypatch.setattr(clip_buffer, "_transcode_to_h264_ffmpeg", lambda frames, path, fps: False)
    monkeypatch.setattr(
        clip_buffer,
        "_write_with_opencv",
        lambda frames_bgr, out_path, fps, fourcc_codes: calls.append(
            (len(frames_bgr), fps, fourcc_codes)
        ) or True,
    )
    clip_buffer.save_clip_mp4(frames, str(target), 12.5)
    assert calls[0][0:2] == (2, 12.5)
    monkeypatch.setattr(clip_buffer, "_write_with_opencv", lambda **kwargs: False)
    with pytest.raises(RuntimeError, match="Clip"):
        clip_buffer.save_clip_mp4(frames, str(target), 10)


def _stage3_job(event_id="e1"):
    return Stage3Job(
        camera_id="cam", source="src", event_id=event_id,
        event_start_ts=1, event_end_ts=2, pose_score_max=.8,
        pose_score_mean=.7, clip_path="clip.mp4", frames=[1, 2],
    )


def test_stage3_worker_threshold_lifecycle_result_and_order(monkeypatch):
    class Adapter:
        device = "cpu"
        model_name = "fake"
        def __init__(self, path):
            pass
        def infer(self, frames):
            return .75

    monkeypatch.setattr("fight.pipeline_mp.stage3_worker.Stage3Adapter", Adapter)
    monkeypatch.setattr("fight.pipeline_mp.stage3_worker.configure_process_runtime", lambda **kwargs: None)
    jobs, incidents, reports = queue.Queue(), queue.Queue(), queue.Queue()
    jobs.put(IncidentLifecycleMessage("cam", "src", "event_chain_opened"))
    jobs.put(_stage3_job())
    jobs.put(IncidentLifecycleMessage("cam", "src", "event_chain_closed"))
    jobs.put(None)
    stage3_process_main(
        {"runtime": {"fight_thr": .7}, "models": {"stage3_config": "fake"}},
        jobs, incidents, reports, threading.Event(),
    )
    messages = []
    while not incidents.empty():
        messages.append(incidents.get_nowait())
    assert [type(item) for item in messages] == [
        IncidentLifecycleMessage, IncidentLifecycleMessage,
        Stage3ResultMessage, IncidentLifecycleMessage,
    ]
    assert messages[1].action == "stage3_pending"
    assert messages[2].fight_label == "fight" and messages[2].fight_prob == .75


def test_stage3_exception_emits_drop_without_leaking_worker(monkeypatch):
    class Adapter:
        def __init__(self, path):
            pass
        def infer(self, frames):
            raise RuntimeError("model exploded")

    monkeypatch.setattr("fight.pipeline_mp.stage3_worker.Stage3Adapter", Adapter)
    monkeypatch.setattr("fight.pipeline_mp.stage3_worker.configure_process_runtime", lambda **kwargs: None)
    jobs, incidents, reports = queue.Queue(), queue.Queue(), queue.Queue()
    jobs.put(_stage3_job())
    jobs.put(None)
    stage3_process_main(
        {"runtime": {}, "models": {"stage3_config": "fake"}},
        jobs, incidents, reports, threading.Event(),
    )
    assert incidents.get_nowait().action == "stage3_pending"
    assert incidents.get_nowait().action == "stage3_dropped"
    rows = [item.row for item in list(reports.queue) if isinstance(item, ReportMessage)]
    assert any(row.get("detail") == "failed" for row in rows)
