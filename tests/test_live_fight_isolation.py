"""Production lifecycle isolation with deterministic faults and real spawn transport."""
import multiprocessing as mp
import queue
import threading
import time
from dataclasses import replace

import numpy as np
import pytest

from fight.pipeline_mp.camera_ingest import run_camera_ingest_loop
from fight.pipeline_mp.camera_lifecycle import CameraLifecycleError
from fight.pipeline_mp.fight_identity import FightChannel, FightGenerations
from fight.pipeline_mp.health import HealthEvent, HealthPolicy, RuntimeWatchdog
from fight.pipeline_mp.messages import CameraFrame, PersonInferenceResult
from fight.pipeline_mp.person_worker import PersonInferenceClient, PersonInferenceError
from fight.pipeline_mp.run_multiprocess import _terminate_process
from fight.pipeline_mp.shared_services import SharedServices
from fight.pipeline_mp.speed_worker import vehicle_service_main
from tests.test_shared_services import fixture, reconcile, close
from tests.test_speed_integration import camera, SpawnVehicleDetector
from tests.test_fight_service_recovery import fake_person
from tests.test_camera_ingest import FakeCapture, _drain


@pytest.mark.parametrize("change", ["remove_fight", "remove_camera", "source", "add_speed", "reenable", "stop"])
def test_capability_churn_during_fight_backoff(change):
    services, manager, registry, now = fixture()
    cam = camera(fight=True, speed=change != "add_speed")
    try:
        reconcile(services, manager, [cam])
        item = manager.runtimes["camera"]
        before = dict(item.processes)
        generation = item.generation
        services.processes()["person"].terminate()
        services.tick()
        if change == "stop":
            manager.global_stop = threading.Event()
            manager.global_stop.set()
        elif change == "remove_camera":
            reconcile(services, manager, [])
        elif change in {"remove_fight", "reenable"}:
            reconcile(services, manager, [{**cam, "use_fight_detection": False}])
            if change == "reenable":
                reconcile(services, manager, [cam])
        elif change == "source":
            reconcile(services, manager, [{**cam, "source": "rtsp://host/replacement"}])
        else:
            reconcile(services, manager, [{**cam, "use_speed_detection": True}])
            assert item.processes["speed"].is_alive() and manager.speed_service_available
        now[0] += 3
        for _ in range(3):
            services.tick()
            manager.poll()
        if change == "remove_camera":
            assert not manager.runtimes
            assert not any(process.is_alive() for process in before.values())
        elif change == "stop":
            assert "fight" not in services.bundles and "camera" not in item.processes
        elif change == "source":
            current = manager.runtimes["camera"]
            assert current is not item and current.generation == generation + 1
            assert not any(process.is_alive() for process in before.values())
            assert current.processes["camera"].is_alive() and not current.fight_service_waiting
        else:
            assert manager.runtimes["camera"] is item and item.generation == generation
            for name in ("ingest", "preview", "speed"):
                if name in before:
                    assert item.processes[name] is before[name] and before[name].is_alive()
            assert ("camera" in item.processes) == (change != "remove_fight")
            assert not item.fight_service_waiting
    finally:
        close(services, manager)


def test_local_fight_failure_fences_health_results_and_is_bounded():
    services, manager, registry, now = fixture({"watchdog_camera_restart_limit": 1})
    try:
        reconcile(services, manager, [camera()])
        item = manager.runtimes["camera"]
        before = dict(item.processes)
        bundle = services.processes()
        old_epoch = item.fight_epoch
        registry.sync_cameras(manager.get_camera_status())
        before["camera"].terminate()
        manager.poll()
        registry.sync_cameras(manager.get_camera_status())
        assert not registry.handle(HealthEvent("camera_worker", "camera", "frame_consumed", now[0],
            camera_id=item.camera_id, slot_id=item.slot_id, generation=item.generation,
            consumer_epoch=old_epoch, progress=999))
        assert registry.handle(HealthEvent("camera_worker", "camera", "frame_consumed", now[0],
            camera_id=item.camera_id, slot_id=item.slot_id, generation=item.generation,
            consumer_epoch=item.fight_epoch, progress=3))
        old = PersonInferenceResult(item.camera_id, item.generation, 1, 1, slot_id=item.slot_id,
                                    consumer_epoch=old_epoch)
        assert not is_valid(old, manager)
        transport = queue.Queue()
        transport.put(old)
        current = replace(old, consumer_epoch=item.fight_epoch)
        transport.put(current)
        assert FightChannel(transport, item.fight_epoch).get(timeout=.1) is current
        item.processes["camera"].terminate()
        for _ in range(5):
            now[0] += 121
            manager.poll()
        assert item.fight_failed and item.fight_restarts == 1 and "camera" not in item.processes
        assert services.processes() == bundle and services.fight_attempts == 0
        for name in ("ingest", "speed", "preview"):
            assert item.processes[name] is before[name] and before[name].is_alive()
        # Waiting/failure of Fight cannot mask an independently dead source.
        watchdog = RuntimeWatchdog(registry, HealthPolicy(), queue.Queue(), monotonic=lambda: now[0])
        before["ingest"].terminate()
        watchdog.tick(manager, bundle)
        assert manager.runtimes["camera"].generation > item.generation
    finally:
        close(services, manager)


def is_valid(message, manager):
    from fight.pipeline_mp.generation import is_current_generation
    return is_current_generation(message, FightGenerations(manager.slot_generations, manager.fight_publication_floor))


def test_unsafe_fight_withdrawal_does_not_attach_duplicate_consumer():
    services, manager, registry, now = fixture()
    try:
        reconcile(services, manager, [camera()])
        item = manager.runtimes["camera"]
        old = item.processes["camera"]
        terminate = manager.terminate_process
        manager.terminate_process = lambda *_args, **_kwargs: None
        with pytest.raises(CameraLifecycleError):
            manager.restart_fight(item, "hung_consumer")
        assert item.processes["camera"] is old and item.fight_pause.is_set()
        manager.terminate_process = terminate
    finally:
        close(services, manager)


def test_poisoned_frame_transport_fails_closed_and_shutdown_skips_old_results():
    services, manager, registry, now = fixture()
    try:
        reconcile(services, manager, [camera()])
        item = manager.runtimes["camera"]
        class Poisoned:
            def get(self, **_kwargs):
                raise OSError("broken frame pipe")
        item.fight_queue = Poisoned()
        with pytest.raises(CameraLifecycleError, match="fight_frame_transport_unsafe"):
            manager.restart_fight(item, "process_dead")
        assert item.fight_failed and item.fight_pause.is_set() and item.fight_restarts == 0
        drained = []
        manager._drain = lambda channel: drained.append(channel)
        manager.stop_all()
        assert not drained and not manager.runtimes
    finally:
        close(services, manager)


@pytest.mark.parametrize("origin", ["local", "recovery", "enable"])
def test_local_spawn_failure_does_not_recycle_shared_services(origin):
    services, manager, registry, now = fixture({"watchdog_camera_restart_limit": 2,
                                               "watchdog_camera_restart_cooldown_sec": 1})
    try:
        reconcile(services, manager, [camera(fight=origin != "enable")])
        item = manager.runtimes["camera"]
        def fail(*_args):
            raise OSError("spawn failed")
        manager.process_factory = fail
        if origin == "local":
            item.processes["camera"].terminate()
            manager.poll()
        elif origin == "recovery":
            services.processes()["person"].terminate()
            services.tick()
            now[0] += 3
            services.tick()
        else:
            reconcile(services, manager, [camera()])
        shared = services.processes()
        for _ in range(4):
            manager.poll()
        assert item.fight_restarts == 1 and item.fight_failed
        now[0] += 1
        manager.poll()
        now[0] += 100
        manager.poll()
        assert item.fight_restarts == 2 and item.fight_failed
        assert services.processes() == shared and services.fight_attempts == (1 if origin == "recovery" else 0)
        assert item.processes["speed"].is_alive() and not item.speed_failed
    finally:
        close(services, manager)


def test_replacement_event_ids_and_timing_populations_are_separate():
    from fight.pipeline_mp.camera_worker import CameraProcessRunner
    from fight.pipeline_mp.performance import build_performance_summary
    ids, rows = [], []
    for epoch in (1, 2):
        runner = CameraProcessRunner.__new__(CameraProcessRunner)
        runner.counters, runner.event_counter = {"events_opened": 0}, 0
        runner.camera_id, runner.generation, runner.consumer_epoch = "camera", 1, epoch
        runner.runtime, runner.source_public = {}, "rtsp://host/camera"
        runner.frame_idx, runner.last_event_close_frame_idx = 100, 90
        runner.capture_fps = runner.clip_write_fps = 25
        runner.report_status = lambda *_: None
        runner.new_event(100, [], .9, "test")
        ids.append(runner.active_event.event_id)
        rows.append({"stage": "camera_summary", "detail": "completed", "camera_id": "camera",
            "generation": 1, "consumer_epoch": epoch, "frames_read": epoch,
            "person": {"_samples": {"round_trip_ms": [epoch * 10]}}})
    assert ids == ["camera_g1_f1_000001", "camera_g1_f2_000001"]
    rows.append(rows[0])  # A late old producer cannot replace the current summary.
    rows.extend({"stage": "person_inference", "detail": "summary", "service_epoch": epoch,
                 "inference_ms": {"mean": epoch}} for epoch in (2, 1))
    summary = build_performance_summary({"cameras": [camera()]}, rows, 1)
    assert len(summary["cameras"]) == 1 and summary["cameras"][0]["consumer_epoch"] == 2
    assert summary["person"]["round_trip_ms"]["mean"] == 20
    assert summary["person"]["inference_ms"]["mean"] == 2


@pytest.mark.parametrize("failed_spawns", [1, 99])
def test_speed_spawn_failure_during_fight_backoff_retries_without_source_restart(failed_spawns):
    services, manager, registry, now = fixture({"watchdog_camera_restart_cooldown_sec": 1,
                                               "watchdog_camera_restart_limit": 2})
    try:
        reconcile(services, manager, [camera(speed=False)])
        item = manager.runtimes["camera"]
        source, preview = item.processes["ingest"], item.processes["preview"]
        services.processes()["person"].terminate()
        services.tick()
        original_spawn = manager.process_factory
        attempts = []
        def spawn(name, target, args):
            if name.startswith("speed_"):
                attempts.append(name)
                if len(attempts) <= failed_spawns:
                    raise OSError("spawn failed")
            return original_spawn(name, target, args)
        manager.process_factory = spawn
        reconcile(services, manager, [camera()])
        assert item.speed_failed and "speed" not in item.processes
        manager.poll()
        assert len(attempts) == 2 and item.speed_restarts == 1
        if failed_spawns == 1:
            assert item.processes["speed"].is_alive() and not item.speed_failed
        else:
            manager.poll()
            assert len(attempts) == 2  # Cooldown still applies without a handle.
            now[0] += 1
            manager.poll()
            now[0] += 100
            manager.poll()
            assert len(attempts) == 3 and item.speed_restarts == 2
            assert item.speed_failed and "speed" not in item.processes
        assert item.processes["ingest"] is source and item.processes["preview"] is preview
        assert item.generation == 1 and item.fight_service_waiting
        assert services.fight_attempts == 0 and services.attempts == 0
    finally:
        close(services, manager)


def test_ordered_file_error_delivery_stops_when_fight_is_withdrawn(tmp_path):
    source = tmp_path / "file.mp4"
    source.touch()
    paused, stopped, eof = threading.Event(), threading.Event(), threading.Event()
    class BrokenCapture(FakeCapture):
        def read(self):
            raise OSError("decode failed")
    class WithdrawingQueue:
        attempts = 0
        def put(self, message, **kwargs):
            self.attempts += 1
            paused.set()
            if self.attempts > 1:
                stopped.set()  # Bound this test even with the original bug.
            raise queue.Full
    capture, fight = BrokenCapture([]), WithdrawingQueue()
    speed = queue.Queue(4)
    run_camera_ingest_loop({"runtime": {}}, {**camera(), "source": str(source)},
        fight, None, None, stopped, 1, capture_factory=lambda _: capture,
        speed_channel=speed, fight_stop=paused, file_eof_event=eof)
    assert fight.attempts == 1 and not stopped.is_set()
    assert not eof.is_set() and capture.release_count == 1
    assert any(message.detail == "fatal_error" for message in _drain(speed))


def test_live_fanout_pause_epoch_and_interruptible_reconnect():
    paused, stopped = threading.Event(), threading.Event()
    fight, speed, preview, reports = (queue.Queue(10) for _ in range(4))
    epochs = [1]
    class ChangingCapture(FakeCapture):
        def read(self):
            if self.read_count == 1:
                paused.set()
            if self.read_count == 2:
                epochs[0] = 2
                paused.clear()
            return super().read()
    capture = ChangingCapture([np.zeros((2, 2, 3), np.uint8)] * 3,
                               stop_event=stopped, stop_after_frame=3)
    run_camera_ingest_loop({"runtime": {}}, camera(), fight, preview, None, stopped, 1,
        capture_factory=lambda _: capture, speed_channel=speed, fight_stop=paused,
        fight_epochs=epochs, slot_id=0)
    offered = [frame for frame in _drain(fight) if isinstance(frame, CameraFrame)]
    assert [(frame.frame_seq, frame.consumer_epoch) for frame in offered] == [(1, 1), (3, 2)]
    assert len([frame for frame in _drain(speed) if isinstance(frame, CameraFrame)]) == 3
    assert capture.release_count == 1
    stopped.clear()
    calls = []
    def unavailable(source):
        calls.append(source)
        raise OSError("offline")
    thread = threading.Thread(target=run_camera_ingest_loop, args=({"runtime": {
        "camera_reconnect_initial_delay_sec": 8}}, camera(), None, preview, reports, stopped, 1),
        kwargs={"capture_factory": unavailable})
    thread.start()
    try:
        while reports.get(timeout=2).row["detail"] != "reconnecting":
            pass
        stopped.set()
        thread.join(1)
        assert not thread.is_alive() and len(calls) == 1
    finally:
        stopped.set()
        thread.join(2)


def test_fight_waiting_health_still_observes_source_and_speed():
    services, manager, registry, now = fixture()
    try:
        reconcile(services, manager, [camera()])
        item = manager.runtimes["camera"]
        services.processes()["person"].terminate()
        services.tick()
        registry.sync_cameras(manager.get_camera_status())
        for component, event, epoch in (("camera_ingest", "frame_progress", 0),
                                        ("camera_preview", "preview_published", 0),
                                        ("speed_worker", "frame_consumed", item.speed_epoch)):
            assert registry.handle(HealthEvent(component, "camera", event, now[0],
                camera_id=item.camera_id, slot_id=item.slot_id, generation=item.generation,
                consumer_epoch=epoch, progress=10))
        actions, _ = registry.evaluate(HealthPolicy())
        assert not actions and registry.cameras[item.camera_id]["health"] == "DEGRADED"
        snapshot = registry.snapshot("test")["cameras"][item.camera_id]
        assert snapshot["fight"]["waiting"] and snapshot["speed"]["progress"] == 10
        assert not snapshot["speed"]["failed"]
        actions, _ = registry.evaluate(HealthPolicy(), camera_process_alive={item.camera_id: {"ingest": False}})
        assert actions[0]["action"] == "restart_camera"
    finally:
        close(services, manager)


class LiveCapture:
    def __init__(self, source):
        self.frame = np.zeros((16, 16, 3), np.uint8)

    def isOpened(self):
        return True

    def read(self):
        time.sleep(.02)
        return True, self.frame

    def get(self, prop):
        return 25

    def release(self):
        pass


def spawned_ingest(config, camera, fight, preview, reports, stop, generation,
                   health, slot, speed, speed_stop, eof, fight_stop, epochs):
    run_camera_ingest_loop(config, camera, fight, preview, reports, stop, generation,
        capture_factory=LiveCapture, health_queue=health, slot_id=slot, speed_channel=speed,
        speed_stop=speed_stop, file_eof_event=eof, fight_stop=fight_stop, fight_epochs=epochs)


def spawned_fight(config, camera, stage3, reports, stop, requests, results,
                  pose, pose_results, generation, frames, slot, health):
    # Real client and lifecycle queue wrappers, with no camera-local model fixtures.
    client = PersonInferenceClient(camera_id=camera["camera_id"], generation=generation,
        request_queue=requests, result_queue=results, report_queue=reports, stop_event=stop,
        slot_id=slot, source_is_file=False, inference_timeout_sec=2, enqueue_timeout_sec=.1)
    while not stop.is_set():
        try:
            frame = frames.get(timeout=.1)
        except queue.Empty:
            continue
        if isinstance(frame, CameraFrame):
            try:
                client.infer(frame.frame, frame.frame_seq)
            except PersonInferenceError:
                if stop.is_set():
                    return
                raise
            config["fight_progress"][0] += 1


class StatefulSpeed:
    def __init__(self, config, camera, first, client, generation, epoch, valid):
        self.progress = config["speed_progress"]
        self.progress[1] += 1  # Number of camera-local tracker constructions.
        self.frames_seen = 0

    def process(self, frame):
        self.frames_seen += 1
        self.progress[0] = self.frames_seen


def wait_until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), "spawned consumer did not progress before bounded deadline"


def test_real_spawn_mixed_recovery_keeps_source_speed_state_and_preview(tmp_path):
    ctx = mp.get_context("spawn")
    processes = []
    def spawn(name, target, args):
        if name == "person":
            target = fake_person
        elif name == "vehicle":
            args = (*args[:7], SpawnVehicleDetector, *args[8:])
        elif name.startswith("camera_ingest_"):
            target = spawned_ingest
        elif name.startswith("speed_"):
            args = (*args[:-2], StatefulSpeed, args[-1])
        elif name.startswith("camera_") and not name.startswith("camera_preview_"):
            target = spawned_fight
        process = ctx.Process(name=name, target=target, args=args)
        process.start()
        processes.append(process)
        return process
    services, manager, registry, now = fixture({"use_pose": False, "use_stage3": False}, ctx=ctx, factory=spawn)
    manager.config.update(output_dir=str(tmp_path), fight_progress=ctx.Array("q", 1), speed_progress=ctx.Array("q", 2))
    manager.process_factory = spawn
    manager.terminate_process = services.terminate = _terminate_process
    try:
        reconcile(services, manager, [camera()])
        item = manager.runtimes["camera"]
        before = dict(item.processes)
        vehicle = services.processes()["vehicle"]
        wait_until(lambda: manager.config["fight_progress"][0] >= 2 and manager.config["speed_progress"][0] >= 2)
        baseline = manager.config["speed_progress"][0]
        services.processes()["person"].terminate()
        services.processes()["person"].join(5)
        services.tick()
        wait_until(lambda: manager.config["speed_progress"][0] > baseline + 3)
        fight_before = manager.config["fight_progress"][0]
        now[0] += 3
        services.tick()
        wait_until(lambda: manager.config["fight_progress"][0] > fight_before + 1)
        assert item.generation == 1 and item.speed_epoch == 1 and item.fight_epoch == 2
        assert manager.config["speed_progress"][1] == 1  # Tracker/history really survived.
        assert services.processes()["vehicle"] is vehicle and not item.speed_failed
        assert item.processes["camera"] is not before["camera"]
        for name in ("ingest", "speed", "preview"):
            assert item.processes[name] is before[name] and before[name].is_alive()
        assert len([p for p in processes if p.name == "camera_ingest_camera"]) == 1
    finally:
        manager.stop_all()
        services.close(graceful=False)
        for process in processes:
            _terminate_process(process, timeout=.2)
        for channel in (manager.report_queue, services.incident_queue):
            SharedServices._close(channel)
