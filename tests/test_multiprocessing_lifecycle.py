from __future__ import annotations

import multiprocessing as mp
import queue

import pytest

from fight.pipeline_mp import run_multiprocess


pytestmark = pytest.mark.multiprocessing


def _waiting_child(ready, stop):
    ready.put("ready")
    stop.wait(10)


def test_real_spawned_process_is_terminated_and_joined():
    ctx = mp.get_context("spawn")
    ready = ctx.Queue()
    stop = ctx.Event()
    process = ctx.Process(target=_waiting_child, args=(ready, stop))
    process.start()
    assert ready.get(timeout=5) == "ready"
    run_multiprocess._terminate_process(process, timeout=.5)
    assert not process.is_alive()
    assert process.exitcode is not None
    ready.close()


def test_file_camera_eof_restart_policy_and_all_file_detection(tmp_path):
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    cam = {"camera_id": "a", "source": str(first)}
    assert run_multiprocess._should_not_restart_finished_camera(cam, {}, 0)
    assert not run_multiprocess._should_not_restart_finished_camera(
        cam, {"loop_file_sources": True}, 0
    )
    assert not run_multiprocess._should_not_restart_finished_camera(cam, {}, 1)
    assert run_multiprocess._all_file_cameras([
        cam, {"camera_id": "b", "source": str(second)},
    ])
    assert not run_multiprocess._all_file_cameras([
        cam, {"camera_id": "live", "source": "rtsp://camera"},
    ])


def test_pipeline_settle_drains_stage3_before_incident(monkeypatch):
    calls = []

    class DrainQueue:
        def __init__(self, name):
            self.name = name
        def join(self):
            calls.append(self.name)
        def empty(self):
            return True
        def qsize(self):
            return 0

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()

    monkeypatch.setattr(run_multiprocess.threading, "Thread", ImmediateThread)
    reports = queue.Queue()
    run_multiprocess._wait_for_pipeline_settle(
        stage3_queue=DrainQueue("stage3"),
        incident_queue=DrainQueue("incident"),
        report_queue=reports,
        runtime={"file_run_finalize_wait_sec": 1, "file_run_queue_empty_settle_sec": 0},
    )
    assert calls == ["stage3", "incident"]
    details = [message.row["detail"] for message in list(reports.queue)]
    assert details == ["waiting_pipeline_settle", "pipeline_settled"]


def test_camera_start_receives_private_channel_and_generation(monkeypatch):
    captured = {}

    def fake_start(name, target, args):
        captured.update(name=name, target=target, args=args)
        return "process"

    monkeypatch.setattr(run_multiprocess, "_start_process", fake_start)
    channel = object()
    result = run_multiprocess._start_camera(
        config={"x": 1}, cam={"camera_id": "cam", "source": "0"},
        stage3_queue="stage3", report_queue="report", stop_event="stop",
        inference_queues={"person_detection": "jobs"}, result_channel=channel,
        session_id="session", generation_id="generation",
    )
    assert result == "process"
    assert captured["name"] == "camera_cam"
    assert captured["args"][-3:] == (channel, "session", "generation")
