import multiprocessing as mp
import queue
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from fight.pipeline.incident_aggregator import IncidentAggregator, Stage3Result
from fight.pipeline_mp.generation import is_current_generation
from fight.pipeline_mp.fight_identity import FightGenerations
from fight.pipeline_mp.health import HealthEmitter, HealthPolicy
from fight.pipeline_mp.messages import PersonInferenceResult, Stage3ResultMessage
from fight.pipeline_mp.person_worker import route_person_result
from fight.pipeline_mp.shared_services import SharedServices
from tests.test_shared_services import fixture, reconcile, close
from tests.test_speed_integration import camera
from tests.test_capacity_scheduling import request
from tests.test_dynamic_camera_lifecycle import FakeProcess


@pytest.mark.parametrize("component", SharedServices.FIGHT)
def test_fight_component_death_replaces_bundle_and_preserves_speed(component):
    services, manager, registry, now = fixture()
    try:
        reconcile(services, manager, [camera("fight", True, False), camera("both"), camera("speed", False, True)])
        originals = services.processes()
        speed = manager.runtimes["speed"]
        old = manager.runtimes["both"]
        old_generation, slot = old.generation, old.slot_id
        old_epoch = old.fight_epoch
        identities = {cid: dict(item.processes) for cid, item in manager.runtimes.items()}
        speed_epoch = old.speed_epoch
        old.processes["speed"].state_marker = {"track": 123, "cooldown": 456}
        old_request = replace(request(slot, generation=old_generation), consumer_epoch=old_epoch)
        guard = FightGenerations(manager.slot_generations, manager.fight_publication_floor)
        old_channels = manager.person_result_channels
        old_admission = manager.person_request_queue
        old_health = services.bundles["fight"]["health"]
        originals[component].terminate()
        services.tick()
        assert manager.runtimes["both"].fight_service_waiting
        assert not is_current_generation(old_request, guard)
        assert manager.runtimes["speed"] is speed
        assert services.processes() == {"vehicle": originals["vehicle"]}
        registry.sync_cameras(manager.get_camera_status())
        actions, _ = registry.evaluate(HealthPolicy())
        assert actions == [] and registry.runtime_health == "DEGRADED"
        for _ in range(10):
            services.tick()
        assert services.fight_attempts == 0
        now[0] += 2
        services.tick()
        current = manager.runtimes["both"]
        assert current is old and current.slot_id == slot and current.generation == old_generation
        assert current.fight_epoch > old_epoch and current.speed_epoch == speed_epoch
        for cid, before in identities.items():
            for name, process in before.items():
                if name != "camera":
                    assert manager.runtimes[cid].processes[name] is process and process.is_alive()
                else:
                    assert manager.runtimes[cid].processes[name] is not process and not process.is_alive()
        assert current.processes["speed"].state_marker == {"track": 123, "cooldown": 456}
        assert not current.speed_failed
        assert not current.fight_service_waiting
        assert services.processes()["vehicle"] is originals["vehicle"]
        assert manager.runtimes["speed"] is speed
        assert all(services.processes()[name] is not originals[name] for name in services.FIGHT)
        assert manager.person_request_queue is not old_admission and manager.person_result_channels is not old_channels
        HealthEmitter(old_health, component="person", component_type="shared_worker").emit("inference_completed", force=True, progress=999)
        services.tick()
        assert registry.workers["person"]["progress"] == 0
        stale = SimpleNamespace(slot_id=slot, generation=old_generation, camera_id="both", consumer_epoch=old_epoch)
        routed, reason = route_person_result(stale, manager.person_result_channels,
            timeout_sec=.1, slot_generations=guard)
        assert not routed and reason == "stale_generation"
        assert registry.snapshot("run")["workers"]["person"]["restart_count"] == 1
    finally:
        close(services, manager)


def test_hung_inference_and_replacement_start_failure_exhaust_bounded_budget():
    services, manager, registry, now = fixture({"fight_service_restart_limit": 2,
        "health_startup_grace_sec": 5, "inference_stall_fail_sec": 10})
    try:
        reconcile(services, manager, [camera("fight", True, False)])
        health = services.bundles["fight"]["health"]
        HealthEmitter(health, component="pose", component_type="shared_worker", monotonic=lambda: now[0]).emit("request_received", force=True)
        services.tick()
        now[0] += 9
        services.tick()
        assert services.fight_retry_at is None
        now[0] += 1
        services.tick()
        assert services.fight_retry_at == now[0] + 2
        def fail(*args):
            raise OSError("replacement spawn failed")
        services.start_process = fail
        for delay in (2, 4):
            now[0] += delay - .1
            services.tick()
            attempts = services.fight_attempts
            now[0] += .1
            services.tick()
            assert services.fight_attempts == attempts + 1
        assert services.fight_failed and services.fight_attempts == 2
        for _ in range(20):
            now[0] += 100
            services.prepare([camera("fight", True, False)])
            services.tick()
        registry.evaluate(HealthPolicy())
        assert registry.runtime_health == "FAILED" and services.fight_attempts == 2
        assert registry.workers["person"]["service_state"] == "failed"
    finally:
        close(services, manager)


def test_file_fight_failure_is_fatal_without_replay(tmp_path):
    file = tmp_path / "file.mp4"
    file.touch()
    services, manager, registry, now = fixture()
    try:
        reconcile(services, manager, [camera("file", True, False, str(file))])
        old = manager.runtimes["file"]
        processes = list(old.processes.values())
        services.processes()["stage3"].terminate()
        services.tick()
        assert services.fight_failed and services.fight_failure_reason == "fight_file_incomplete"
        now[0] += 100
        services.tick()
        assert manager.runtimes["file"] is old and not old.file_done
        assert old.fight_failed and "camera" not in old.processes
        assert all(old.processes[name].is_alive() for name in ("ingest", "preview"))
        registry.evaluate(HealthPolicy())
        assert registry.runtime_health == "FAILED" and services.fight_attempts == 0
    finally:
        close(services, manager)


@pytest.mark.parametrize("invalidate_during_encode", [False, True])
@pytest.mark.parametrize("boundary", ["service", "consumer"])
def test_buffered_incident_from_failed_incarnation_cannot_publish(tmp_path, monkeypatch, invalidate_during_encode, boundary):
    floor = mp.get_context("spawn").Array("q", [0, 0])
    floor_index = 0 if boundary == "service" else 1
    agg = IncidentAggregator(str(tmp_path / "incidents"), run_id="test", publication_floor=floor,
        stale_finalize_sec=999, sweep_interval_sec=999)
    part = tmp_path / "part.mp4"
    part.write_bytes(b"fixture")
    result = Stage3Result("cam", "0", "event", 10, 12, str(part), .99, "fight", .9, .9,
                          service_epoch=1, consumer_epoch=1, slot_id=0)
    def encode(parts, output):
        output.write_bytes(b"fixture")
        if invalidate_during_encode:
            with floor.get_lock():
                floor[floor_index] = 2
        return True
    monkeypatch.setattr(agg, "_concat_mp4s", encode)
    monkeypatch.setattr(agg, "_add_ai_overlay_to_clip", lambda *_: True)
    try:
        agg.submit(result)
        if not invalidate_during_encode:
            floor[floor_index] = 2
        agg.finalize("cam", force=True)
        assert not agg.outbox_path.exists() and not agg.incidents_jsonl.exists()
        agg.submit(result)
        assert "cam" not in agg.by_camera
        monkeypatch.setattr(agg, "_concat_mp4s", lambda _, output: bool(output.write_bytes(b"fixture")))
        agg.submit(replace(result, service_epoch=2, consumer_epoch=2, event_id="current"))
        agg.finalize("cam", force=True)
        assert agg.outbox_path.is_file() and agg.incidents_jsonl.is_file()
    finally:
        agg.close_all()


def fake_person(config, requests, results, report, stop, generations, health):
    while not stop.is_set():
        try:
            item = requests.get(timeout=.1)
        except queue.Empty:
            continue
        try:
            if is_current_generation(item, generations):
                results.put(PersonInferenceResult(camera_id=item.camera_id, generation=item.generation,
                    request_id=item.request_id, frame_idx=item.frame_idx,
                    detections=[], slot_id=item.slot_id, consumer_epoch=item.consumer_epoch), timeout=1)
        finally:
            requests.task_done()


def test_recreated_fight_transport_works_under_windows_spawn():
    ctx = mp.get_context("spawn")
    def spawn(name, target, args):
        process = ctx.Process(target=fake_person if name == "person" else target, args=args)
        process.start()
        return process
    services, manager, registry, now = fixture({"use_pose": False, "use_stage3": False}, ctx=ctx, factory=spawn)
    def terminate(process, timeout=0):
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(3)
    services.terminate = terminate
    try:
        reconcile(services, manager, [camera("fight", True, False)])
        for incarnation in (1, 2):
            item = manager.runtimes["fight"]
            manager.person_request_queue.put(replace(request(item.slot_id, generation=item.generation),
                                                    consumer_epoch=item.fight_epoch), timeout=1)
            result = manager.person_result_channels[item.slot_id].get(timeout=10)
            assert result.generation == item.generation
            if incarnation == 1:
                terminate(services.processes()["person"])
                services.tick()
                now[0] += 2
                services.tick()
        assert services.fight_attempts == 1 and "vehicle" not in services.processes()
    finally:
        close(services, manager)


@pytest.mark.parametrize("component,file_source,expected", [
    ("person", False, 0), ("person", True, 10), ("incident", False, 3), ("reporter", False, 8),
])
def test_parent_recovers_live_fight_but_keeps_file_incident_reporter_fatal(tmp_path, monkeypatch, component, file_source, expected):
    from fight.pipeline_mp import run_multiprocess as runtime
    from fight.pipeline_mp.camera_lifecycle import CameraRuntimeManager
    processes, stop, now, ticks = [], [], [100.0], [0]
    initialize = SharedServices.__init__
    def init(self, *args, **kwargs):
        kwargs["monotonic"] = lambda: now[0]
        initialize(self, *args, **kwargs)
    def spawn(name, target, args):
        # A replacement camera cannot overlap its old physical source owner.
        if name.startswith("camera_ingest"):
            assert not any(p.name == name and p.is_alive() for p in processes)
        process = FakeProcess(name)
        processes.append(process)
        return process
    def tick(_):
        ticks[0] += 1
        now[0] += 1
        if ticks[0] == 1:
            next(p for p in processes if p.name == component).terminate()
        if len([p for p in processes if p.name == "person"]) == 2:
            stop[0].set()
        assert ticks[0] < 15, "runtime failed to recover or terminate"
    monkeypatch.setattr(SharedServices, "__init__", init)
    monkeypatch.setattr(runtime, "_start_process", spawn)
    monkeypatch.setattr(CameraRuntimeManager, "_default_process_factory", staticmethod(spawn))
    monkeypatch.setattr(runtime, "install_signal_handlers", lambda event: stop.append(event))
    monkeypatch.setattr(runtime.time, "sleep", tick)
    monkeypatch.setattr(runtime, "_close_queue", SharedServices._close)
    source = tmp_path / "file.mp4"
    source.touch()
    config = {"output_dir": str(tmp_path), "run_id": "same-parent", "cameras": [
        camera("fight", True, False, str(source) if file_source else "rtsp://host/fight"),
        camera("speed", False, True)], "runtime": {"health_enabled": False, "use_pose": False,
            "use_stage3": False, "dynamic_camera_slot_count": 2}}
    assert runtime._run_dynamic(config) == expected
    assert len([p for p in processes if p.name == "person"]) == (2 if expected == 0 else 1)
    for name in ("vehicle", "incident", "reporter", "camera_ingest_speed", "speed_speed"):
        assert len([p for p in processes if p.name == name]) == 1
