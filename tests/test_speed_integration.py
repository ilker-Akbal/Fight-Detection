import json
import multiprocessing as mp
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from fight.pipeline_mp.camera_ingest import run_camera_ingest_loop, publish_speed
from fight.pipeline_mp.camera_lifecycle import CameraRuntimeManager
from fight.pipeline_mp.health import HealthRegistry, HealthPolicy, HealthEmitter
from fight.pipeline_mp.messages import CameraFrame, CameraIngestSignal
from fight.pipeline_mp.scheduling import FairRequestQueue, AdmissionStopped
from fight.pipeline_mp.speed_worker import (
    SpeedProcessor, SpeedGenerationGuard, VehicleRequest, vehicle_service_main, persist_speed_event, speed_process_main,
)
from tests.test_camera_ingest import FakeCapture, _drain
from tests.test_capacity_scheduling import ThreadContext
from tests.test_dynamic_camera_lifecycle import FakeProcess


def camera(cid="camera", fight=True, speed=True, source="rtsp://host/camera"):
    return {"camera_id": cid, "source": source, "use_fight_detection": fight, "use_speed_detection": speed}


class SpawnVehicleDetector:
    def __init__(self, cfg):
        pass

    def detect(self, frame):
        return []


class SpawnSpeedProcessor:
    def __init__(self, config, camera, frame, client, generation, epoch, valid):
        self.client = client

    def process(self, frame):
        self.client.detect(frame.frame, frame)


def test_spawned_speed_consumer_drains_common_ingest_without_gpu(tmp_path):
    ctx = mp.get_context("spawn")
    stop, consumer_stop = ctx.Event(), ctx.Event()
    requests = FairRequestQueue(ctx, 1, 1)
    results = {0: ctx.Queue(1)}
    frames, health = ctx.Queue(2), ctx.Queue(32)
    generations, epochs = ctx.Array("q", [1]), ctx.Array("q", [1])
    source = tmp_path / "file.mp4"
    source.touch()
    config = {"output_dir": str(tmp_path), "run_id": "spawn", "runtime": {}}
    cam = camera(fight=False, source=str(source))
    service = ctx.Process(target=vehicle_service_main, args=(config, requests, results, stop,
        generations, epochs, health, SpawnVehicleDetector))
    consumer = ctx.Process(target=speed_process_main, args=(config, cam, frames, requests.for_slot(0),
        results[0], consumer_stop, 1, 0, epochs, 1, generations, health, SpawnSpeedProcessor))
    service.start()
    consumer.start()
    try:
        capture = FakeCapture([np.zeros((8, 12, 3), dtype=np.uint8)] * 2)
        run_camera_ingest_loop(config, cam, None, queue.Queue(2), queue.Queue(), stop, 1,
            capture_factory=lambda _: capture, speed_channel=frames, speed_stop=consumer_stop)
        consumer.join(10)
        assert consumer.exitcode == 0
        requests.put(None, timeout=1)
        service.join(10)
        assert service.exitcode == 0 and requests.empty()
        messages = _drain(health)
        assert any(item.component == "speed_worker" and item.event_type == "eof" and item.progress == 2 for item in messages)
        assert capture.release_count == 1
    finally:
        stop.set()
        consumer_stop.set()
        for process in (consumer, service):
            if process.is_alive():
                process.terminate()
            process.join(3)
        for channel in (requests, results[0], frames, health):
            channel.cancel_join_thread()
            channel.close()


def test_single_ingest_fans_out_both_and_live_speed_does_not_block_fight(tmp_path):
    source = tmp_path / "source.mp4"
    source.touch()
    opens = []
    frame = np.zeros((8, 12, 3), dtype=np.uint8)
    capture = FakeCapture([frame, frame])
    fight, speed, preview = queue.Queue(3), queue.Queue(3), queue.Queue(3)
    def open_once(value):
        opens.append(value)
        return capture
    run_camera_ingest_loop({"runtime": {}}, camera(source=str(source)), fight, preview,
        queue.Queue(), threading.Event(), 2, capture_factory=open_once, speed_channel=speed,
        speed_stop=threading.Event())
    assert opens == [str(source)] and capture.release_count == 1
    fight_items, speed_items = _drain(fight), _drain(speed)
    assert [item.frame_seq for item in fight_items] == [1, 2, 2]
    assert [item.frame_seq for item in speed_items] == [1, 2, 2]
    assert isinstance(speed_items[-1], CameraIngestSignal) and speed_items[-1].detail == "eof"
    speed = queue.Queue(1)
    stop = threading.Event()
    assert publish_speed(speed, 1, stop, stop, False, 0.01) == (True, 0)
    assert publish_speed(speed, 2, stop, stop, False, 0.01) == (True, 1)
    entered, finished = threading.Event(), threading.Event()
    def producer():
        entered.set()
        publish_speed(speed, 3, stop, stop, True, 0.01)
        finished.set()
    thread = threading.Thread(target=producer)
    thread.start()
    assert entered.wait(1) and not finished.is_set()
    assert speed.get() == 2
    assert finished.wait(1) and speed.get() == 3
    thread.join(1)


def manager_fixture():
    context = ThreadContext()
    channels = {slot: queue.Queue(1) for slot in range(4)}
    generations, epochs = [0] * 4, [0] * 4
    manager = CameraRuntimeManager(ctx=context, config={"output_dir": ".", "runtime": {}},
        stage3_queue=queue.Queue(), report_queue=queue.Queue(), person_request_queue=queue.Queue(),
        person_result_channels=channels, pose_request_queue=None, pose_result_channels={},
        slot_generations=generations, speed_epochs=epochs,
        vehicle_requests=FairRequestQueue(context, 4, 1), vehicle_results=channels,
        process_factory=lambda name, target, args: FakeProcess(name),
        terminate_process=lambda process, timeout=0: process.terminate() if process else None,
        close_queue=lambda _: None)
    return manager


def test_camera_modes_reconfigure_and_consumer_failure_are_isolated(tmp_path):
    manager = manager_fixture()
    cameras = [camera("fight", True, False), camera("speed", False, True),
               camera("both"), camera("neither", False, False)]
    manager.reconcile(cameras)
    assert set(manager.runtimes) == {"fight", "speed", "both"}
    assert set(manager.runtimes["fight"].processes) == {"ingest", "camera", "preview"}
    assert set(manager.runtimes["speed"].processes) == {"ingest", "speed", "preview"}
    both = manager.runtimes["both"]
    original = dict(both.processes)
    original["speed"].alive, original["speed"].exitcode = False, 1
    manager.poll()
    assert both.processes["ingest"] is original["ingest"] and both.processes["camera"] is original["camera"]
    assert both.processes["speed"] is not original["speed"]
    assert both.speed_epoch > 1
    registry = HealthRegistry()
    registry.sync_cameras(manager.get_camera_status())
    health_queue = queue.Queue()
    HealthEmitter(health_queue, component="speed_worker", component_type="camera",
        camera_id=both.camera_id, slot_id=both.slot_id, generation=both.generation,
        consumer_epoch=both.speed_epoch - 1).emit("frame_consumed", force=True, progress=900)
    assert not registry.handle(health_queue.get())
    guard = SpeedGenerationGuard(manager.slot_generations, manager.speed_epochs, both.slot_id, both.generation, both.speed_epoch)
    original_generation, original_slot = both.generation, both.slot_id
    cameras[2]["speed_config"] = {"speed_limit_kmh": 45}
    manager.reconcile(cameras)
    assert not guard()
    assert manager.runtimes["both"].slot_id == original_slot
    assert manager.runtimes["both"].generation == original_generation + 1
    manager.stop_all()


def test_file_eof_waits_for_speed_and_failure_is_not_clean(tmp_path):
    source = tmp_path / "file.mp4"
    source.touch()
    manager = manager_fixture()
    manager.reconcile([camera(source=str(source))])
    item = manager.runtimes["camera"]
    for name in ("ingest", "camera"):
        item.processes[name].alive = False
        item.processes[name].exitcode = 0
    manager.poll()
    assert not item.file_done
    item.processes["speed"].alive = False
    item.processes["speed"].exitcode = 1
    manager.poll()
    assert item.file_done and item.speed_failed and item.speed_stop.is_set()
    assert item.speed_restarts == 0
    registry = HealthRegistry()
    registry.sync_cameras(manager.get_camera_status())
    actions, _ = registry.evaluate(HealthPolicy())
    assert actions == [] and registry.runtime_health == "DEGRADED"
    assert registry.snapshot("run")["cameras"]["camera"]["speed"]["failed"]
    manager.stop_all()


def test_shared_vehicle_service_rejects_old_generations_and_sheds_only_live(tmp_path):
    requests = FairRequestQueue(ThreadContext(), 3, 2)
    results = {slot: queue.Queue(2) for slot in range(3)}
    health = queue.Queue()
    now = time.perf_counter() - 10
    for slot, generation, file in ((0, 1, True), (1, 2, False), (2, 2, True)):
        requests.put(VehicleRequest(str(slot), slot, generation, 3, 1, np.zeros((4, 4, 3)), now, file, 2))
    requests.put(None)
    detected = []
    config = {"output_dir": str(tmp_path), "speed": {"base_config": str(Path("HizTespiti/speed/configs/speed.yaml").resolve())}}
    vehicle_service_main(config, requests, results, threading.Event(), [2] * 3, [3] * 3, health,
        detector_factory=lambda _: SimpleNamespace(detect=lambda frame: detected.append(True) or []))
    assert results[0].empty()
    assert results[1].get().outcome == "stale"
    assert results[2].get().outcome == "accepted" and detected == [True]
    assert requests.empty()
    assert requests.snapshot()["stale_generation"] == 1


def test_speed_local_pipeline_quiet_traffic_never_opens_source_or_loads_model(tmp_path, monkeypatch):
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"measurement": {"mode": "two_line_time_gate",
        "line_a": [[0, 1], [12, 1]], "line_b": [[0, 6], [12, 6]], "distance_m": 10}}))
    cam = camera()
    cam["speed_config"] = {"calibration_path": str(calibration), "save_clip": False, "save_snapshot": False}
    config = {"output_dir": str(tmp_path), "run_id": "run", "speed": {"motion": {"enabled": False},
              "runtime": {"resize_width": 12}, "yolo": {"stride": 1}}}
    def forbidden(*args, **kwargs):
        raise AssertionError("camera-local Speed must not open sources or load models")
    monkeypatch.setattr("cv2.VideoCapture", forbidden)
    monkeypatch.setattr("HizTespiti.yolo.src.vehicle_detector.VehicleDetector.__init__", forbidden)
    frames = CameraFrame("camera", 1, 1, time.perf_counter(), time.time(), np.zeros((8, 12, 3), dtype=np.uint8), 25)
    processor = SpeedProcessor(config, cam, frames, SimpleNamespace(detect=lambda *_: []), 1, 1, lambda: True)
    processor.process(frames)
    frames.frame_seq = 20
    processor.process(frames)
    assert not list(tmp_path.rglob("*speed_violations.jsonl"))


def test_speed_evidence_failure_cannot_publish_legacy_success(tmp_path):
    from HizTespiti.speed.src.evidence_writer import EvidenceWriter, FrameBuffer
    import errno
    attempts = []
    def fail(event):
        assert Path(event.snapshot_path).is_file()
        attempts.append(event)
        raise OSError(errno.ENOSPC, "outbox write failed")
    writer = EvidenceWriter(tmp_path, "camera", SimpleNamespace(save_snapshot=True, save_clip=False,
                            jpeg_quality=80), 25, persist=fail)
    with pytest.raises(OSError):
        writer.save_event(1, 0.04, np.zeros((8, 12, 3), dtype=np.uint8),
            SimpleNamespace(track_id=1, cls_name="car", box=[0, 0, 10, 5]),
            75, 50, 10, 60, FrameBuffer(2))
    assert attempts and not writer.events_path.exists()


def test_speed_django_incident_and_registry_in_isolated_database():
    backend = Path(__file__).resolve().parents[1] / "Fight_backend_project" / "backend_frontend_project"
    code = (
        "import os; os.environ['DJANGO_SETTINGS_MODULE']='backend_frontend_project.settings'; "
        "from django.conf import settings; "
        "settings.DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}}; "
        "import django; django.setup(); from django.core.management import call_command; "
        "call_command('test','incidents.phase13_tests',verbosity=0,interactive=False)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=backend, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
