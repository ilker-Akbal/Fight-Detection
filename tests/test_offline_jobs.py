"""One-shot job correctness reuses the production lifecycle, not a second model."""
import json
import queue
import uuid
from unittest.mock import Mock

import pytest

from fight.operations import atomic_json
from fight.pipeline_mp.offline_jobs import OfflineJobs, OfflineDrain, drain_ack_path
from tests.test_shared_services import fixture, reconcile, close
from tests.test_speed_integration import camera


@pytest.fixture
def job(tmp_path):
    services, manager, registry, clock = fixture()
    config = {"output_dir": str(tmp_path / "output")}
    jobs = OfflineJobs(tmp_path / "jobs", config, manager, services, services.incident_queue)
    source = tmp_path / "asset.mp4"
    source.touch()
    cam = {**camera(source=str(source)), "camera_id": "offline_" + uuid.uuid4().hex}
    atomic_json(jobs.root / "request.json", {"camera": cam, "cancel": False})
    jobs.tick([])
    yield jobs, manager, services, cam
    close(services, manager)


def state(jobs, cam):
    return json.loads((jobs.root / (cam["camera_id"] + ".json")).read_text())


def test_completion_requires_eof_each_consumer_and_durable_downstream_ack(job):
    jobs, manager, services, cam = job
    item = manager.runtimes[cam["camera_id"]]
    item.file_eof_event.set()
    for process in item.processes.values():
        process.alive, process.exitcode = False, 0
    manager.poll()
    assert item.file_done
    jobs.tick([])
    marker = manager.stage3_queue.get(timeout=1)
    manager.stage3_queue.task_done()
    assert isinstance(marker, OfflineDrain)
    assert state(jobs, cam)["state"] == "PROCESSING"
    atomic_json(drain_ack_path(jobs.config, cam["camera_id"]), {
        "generation": item.generation, "consumer_epoch": item.fight_epoch, "outbox_offset": 321})
    jobs.tick([])
    assert state(jobs, cam)["state"] == "COMPLETED"
    assert state(jobs, cam)["outbox_offset"] == 321
    jobs.tick([])
    replacement = OfflineJobs(jobs.root, jobs.config, manager, services, services.incident_queue)
    replacement.tick([])
    assert cam["camera_id"] not in manager.runtimes  # no replay on restart


def test_pre_eof_zero_speed_exit_stays_failed_and_live_source_survives(job):
    jobs, manager, services, cam = job
    live = camera(source="http://127.0.0.1:8090/stream.mjpg")
    reconcile(services, manager, [live, cam])
    live_ingest = manager.runtimes["camera"].processes["ingest"]
    item = manager.runtimes[cam["camera_id"]]
    item.processes["speed"].alive, item.processes["speed"].exitcode = False, 0
    manager.poll()
    jobs.tick([live])
    assert state(jobs, cam)["state"] == "FAILED"
    assert manager.runtimes["camera"].processes["ingest"] is live_ingest
    jobs.tick([live])
    assert cam["camera_id"] not in manager.runtimes


def test_ingest_failure_and_watchdog_never_restart_offline_source(job):
    jobs, manager, services, cam = job
    item = manager.runtimes[cam["camera_id"]]
    generation = item.generation
    manager.restart_camera(item.camera_id, cam, reason="frame_stall")
    assert item.file_done and item.state == "FAILED" and item.generation == generation
    jobs.tick([])
    assert state(jobs, cam)["state"] == "FAILED"


def test_interrupted_claim_is_terminal_even_before_spawn(job):
    jobs, manager, services, cam = job
    manager.stop_camera(cam["camera_id"])
    replacement = OfflineJobs(jobs.root, jobs.config, manager, services, services.incident_queue)
    replacement.tick([])
    assert state(jobs, cam)["error"] == "runtime_interrupted"
    assert not manager.runtimes


def test_cancel_does_not_change_live_desired_or_replay(job):
    jobs, manager, services, cam = job
    atomic_json(jobs.root / "request.json", {"camera": cam, "cancel": True})
    jobs.tick([])
    assert state(jobs, cam)["state"] == "CANCELLED"
    assert not manager.runtimes


def test_offline_drain_finalizes_before_ack(tmp_path, monkeypatch):
    from fight.pipeline_mp import incident_worker
    config = {"output_dir": str(tmp_path), "runtime": {"incident_outbox_path": str(tmp_path / "outbox")}}
    marker = OfflineDrain("offline_" + uuid.uuid4().hex, 0, 1, 1)
    channel, reports = queue.Queue(), queue.Queue()
    channel.put(marker)
    channel.put(None)
    agg = Mock()
    agg.finalize.side_effect = lambda cid: (tmp_path / "outbox").write_bytes(b"durable")
    monkeypatch.setattr(incident_worker, "IncidentAggregator", Mock(return_value=agg))
    incident_worker.incident_process_main(config, channel, reports, Mock(is_set=lambda: False), [1])
    agg.finalize.assert_called_once_with(marker.camera_id)
    assert json.loads(drain_ack_path(config, marker.camera_id).read_text())["outbox_offset"] == 7


def test_fight_offline_time_is_video_relative():
    from fight.pipeline_mp.camera_worker import CameraProcessRunner
    worker = CameraProcessRunner.__new__(CameraProcessRunner)
    worker.camera_id, worker.source_is_file, worker.capture_fps = "offline_" + uuid.uuid4().hex, True, 25
    worker.source_timeline_base_ts = 123456789
    assert worker.timestamp_for_frame_idx(50) == 2


def test_stage3_failure_cannot_be_overwritten_by_later_eof_ack(job):
    from fight.pipeline_mp.offline_jobs import record_failure
    jobs, manager, services, cam = job
    item = manager.runtimes[cam["camera_id"]]
    record_failure(jobs.config, item.camera_id, item.generation, "stage3_failed")
    item.file_eof_event.set()
    item.file_done = True
    atomic_json(drain_ack_path(jobs.config, item.camera_id), {
        "generation": item.generation, "consumer_epoch": item.fight_epoch, "outbox_offset": 0})
    jobs.tick([])
    assert state(jobs, cam)["state"] == "FAILED"


def test_evidence_failure_waits_for_authoritative_eof_and_downstream_drain(job):
    from fight.pipeline_mp.offline_jobs import record_failure
    jobs, manager, services, cam = job
    item = manager.runtimes[cam["camera_id"]]
    ingest = item.processes["ingest"]
    record_failure(jobs.config, item.camera_id, item.generation, "evidence_write_failed")
    jobs.tick([])
    assert state(jobs, cam)["state"] == "PROCESSING" and ingest.is_alive()
    item.file_eof_event.set()
    for process in item.processes.values():
        process.alive, process.exitcode = False, 0
    manager.poll()
    jobs.tick([])
    assert state(jobs, cam)["state"] == "PROCESSING"
    atomic_json(drain_ack_path(jobs.config, item.camera_id), {
        "generation": item.generation, "consumer_epoch": item.fight_epoch, "outbox_offset": 0})
    jobs.tick([])
    assert state(jobs, cam)["state"] == "FAILED"
    assert state(jobs, cam)["error"] == "evidence_write_failed"
    assert not manager.runtimes


def test_genuine_fight_crash_before_eof_remains_fail_closed(job):
    jobs, manager, services, cam = job
    item = manager.runtimes[cam["camera_id"]]
    ingest, generation = item.processes["ingest"], item.generation
    item.processes["camera"].terminate()
    manager.poll()
    assert item.fight_failed and not manager.file_eof_reached(item)
    assert item.processes["ingest"] is ingest and item.generation == generation
    jobs.tick([])
    assert state(jobs, cam)["state"] == "FAILED"
    assert state(jobs, cam)["error"] == "required_consumer_failed"
    jobs.tick([])
    assert not manager.runtimes
