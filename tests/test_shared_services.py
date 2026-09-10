import multiprocessing as mp
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fight.pipeline_mp.camera_lifecycle import CameraRuntimeManager
from fight.pipeline_mp.health import HealthEmitter, HealthPolicy, HealthRegistry
from fight.pipeline_mp.scheduling import AdmissionStopped
from fight.pipeline_mp.shared_services import SharedServices, SharedServiceStartError
from fight.pipeline_mp.speed_worker import SpeedGenerationGuard, VehicleRequest, persist_speed_event, vehicle_service_main
from tests.test_capacity_scheduling import ThreadContext
from tests.test_dynamic_camera_lifecycle import FakeProcess
from tests.test_speed_integration import camera, SpawnVehicleDetector


class Context(ThreadContext):
    JoinableQueue = staticmethod(queue.Queue)


def fixture(runtime=None, ctx=None, factory=None):
    ctx = ctx or Context()
    now = [100.0]
    generations, epochs = ctx.Array("q", 4), ctx.Array("q", 4)
    manager = CameraRuntimeManager(ctx=ctx, config={"output_dir": ".", "run_id": "test", "runtime": runtime or {}},
        stage3_queue=None, report_queue=ctx.JoinableQueue(), person_request_queue=None,
        person_result_channels={}, pose_request_queue=None, pose_result_channels={},
        slot_generations=generations, speed_epochs=epochs, monotonic=lambda: now[0],
        process_factory=lambda name, target, args: FakeProcess(name),
        terminate_process=lambda process, timeout=0: process.terminate() if process else None,
        close_queue=SharedServices._close)
    registry = HealthRegistry(monotonic=lambda: now[0])
    services = SharedServices(manager, ctx.JoinableQueue(), factory or (lambda name, target, args: FakeProcess(name)),
        manager.terminate_process, registry, lambda: now[0])
    return services, manager, registry, now


def reconcile(services, manager, cameras):
    services.prepare(cameras)
    manager.reconcile(cameras)
    services.tick()


def close(services, manager):
    manager.stop_all()
    services.close()
    services._close(manager.report_queue)
    services._close(services.incident_queue)


def test_capability_transitions_preserve_unrelated_processes_and_stable_slots():
    services, manager, registry, now = fixture()
    fight, speed = camera("fight", True, False), camera("speed", False, True)
    try:
        reconcile(services, manager, [fight])
        assert set(services.processes()) == set(services.FIGHT)
        assert manager.vehicle_requests is None and manager.vehicle_results == {}
        original_fight = services.processes().copy()
        camera_fight = manager.runtimes["fight"]
        generation = camera_fight.generation
        reconcile(services, manager, [fight, speed])
        vehicle = services.processes()["vehicle"]
        camera_speed = manager.runtimes["speed"]
        assert all(services.processes()[name] is process for name, process in original_fight.items())
        assert manager.runtimes["fight"] is camera_fight and camera_fight.generation == generation
        reconcile(services, manager, [speed])
        now[0] += 6
        services.tick()
        if services.draining is not None:
            assert services.draining.wait(1)
            services.tick()
        assert set(services.processes()) == {"vehicle"}
        assert vehicle is services.processes()["vehicle"] and manager.runtimes["speed"] is camera_speed
        reconcile(services, manager, [fight, speed])
        assert vehicle is services.processes()["vehicle"] and manager.runtimes["speed"] is camera_speed
        assert manager.runtimes["fight"].slot_id == camera_fight.slot_id
        assert manager.runtimes["fight"].generation > generation
        new_fight = {name: process for name, process in services.processes().items() if name != "vehicle"}
        reconcile(services, manager, [fight])
        now[0] += 6
        services.tick()
        assert services.processes() == new_fight and not vehicle.is_alive()
        registry.evaluate(HealthPolicy())
        assert registry.workers["vehicle"]["health"] == "HEALTHY"
        assert registry.snapshot("test")["workers"]["vehicle"]["service_state"] == "disabled"
    finally:
        close(services, manager)


def test_configured_absence_is_healthy_and_fight_fault_defers_to_bundle_owner():
    services, manager, registry, now = fixture({"use_pose": False, "use_stage3": False})
    try:
        reconcile(services, manager, [camera("speed", False, True)])
        assert set(services.processes()) == {"vehicle"}
        assert manager.person_request_queue is None and manager.stage3_queue is None
        reconcile(services, manager, [camera("speed", False, True), camera("fight", True, False)])
        assert set(services.processes()) == {"vehicle", "person", "person_router"}
        registry.evaluate(HealthPolicy(), worker_process_alive={"pose": False, "stage3": False})
        assert registry.workers["pose"]["health"] == registry.workers["stage3"]["health"] == "HEALTHY"
        registry.evaluate(HealthPolicy(), worker_process_alive={"person": False})
        assert registry.runtime_health == "DEGRADED"
        assert services.critical_processes()["person"][1] == 4
        registry.register_worker("incident")
        registry.evaluate(HealthPolicy(), worker_process_alive={"incident": False})
        assert registry.runtime_health == "FAILED"
    finally:
        close(services, manager)


def test_vehicle_recovery_replaces_transport_rejects_stale_work_and_never_replays_file(tmp_path):
    source = tmp_path / "source.mp4"
    source.touch()
    services, manager, registry, now = fixture()
    try:
        reconcile(services, manager, [camera("both"), camera("file", False, True, str(source))])
        both, file = manager.runtimes["both"], manager.runtimes["file"]
        original = dict(both.processes)
        fight = services.critical_processes()
        guard = SpeedGenerationGuard(manager.slot_generations, manager.speed_epochs, both.slot_id, both.generation, both.speed_epoch)
        requests, results = manager.vehicle_requests, manager.vehicle_results
        old_health = services.bundles["vehicle"]["health"]
        services.processes()["vehicle"].terminate()
        services.tick()
        assert both.speed_failed and file.speed_failed and not guard()
        assert services.critical_processes() == fight
        assert all(both.processes[name] is original[name] for name in ("ingest", "camera", "preview"))
        registry.evaluate(HealthPolicy())
        assert registry.runtime_health == "DEGRADED"
        assert registry.workers["vehicle"]["reason"] == "service_restarting"
        for _ in range(30):
            services.tick()
        assert services.attempts == 0 and "vehicle" not in services.processes()
        file_process = file.processes["speed"]
        now[0] += 2
        services.tick()
        assert services.attempts == 1 and not both.speed_failed and file.speed_failed
        assert file.processes["speed"] is file_process
        assert manager.vehicle_requests is not requests and manager.vehicle_results is not results
        assert both.processes["speed"] is not original["speed"]
        # Old incarnation health cannot complete an inference in the new service.
        HealthEmitter(old_health, component="vehicle", component_type="shared_worker").emit("inference_completed", force=True, progress=999)
        services.tick()
        assert registry.workers["vehicle"]["progress"] == 0
        with pytest.raises(AdmissionStopped):
            persist_speed_event(manager.config, both.camera, SimpleNamespace(), both.generation, both.speed_epoch - 2, guard)
        file.processes["ingest"].alive, file.processes["ingest"].exitcode = False, 0
        file.file_eof_event.set()
        manager.poll()
        assert file.file_done and file.speed_failed
    finally:
        close(services, manager)


def test_vehicle_stall_and_restart_exhaustion_are_bounded():
    services, manager, registry, now = fixture({"vehicle_service_restart_limit": 2,
        "health_startup_grace_sec": 5, "inference_stall_fail_sec": 10})
    try:
        reconcile(services, manager, [camera("speed", False, True)])
        health = services.bundles["vehicle"]["health"]
        HealthEmitter(health, component="vehicle", component_type="shared_worker", monotonic=lambda: now[0]).emit("request_received", force=True)
        services.tick()
        now[0] += 9
        services.tick()
        assert "vehicle" in services.processes()
        now[0] += 1
        services.tick()
        assert "vehicle" not in services.processes()
        for backoff in (2, 4):
            now[0] += backoff - .1
            services.tick()
            assert "vehicle" not in services.processes()
            now[0] += .1
            services.tick()
            services.processes()["vehicle"].terminate()
            services.tick()
        assert services.attempts == 2 and services.vehicle_failed
        now[0] += 1000
        for _ in range(30):
            services.prepare([camera("speed", False, True)])
            services.tick()
        assert services.attempts == 2 and not services.processes()
        registry.evaluate(HealthPolicy())
        assert registry.runtime_health == "DEGRADED"
        assert registry.snapshot("test")["workers"]["vehicle"]["service_state"] == "failed"
    finally:
        close(services, manager)


def test_model_error_is_explicit_shared_failure_not_negative_detection():
    services, manager, registry, now = fixture()
    try:
        reconcile(services, manager, [camera("speed", False, True)])
        item = manager.runtimes["speed"]
        manager.vehicle_requests.put(VehicleRequest(item.camera_id, item.slot_id, item.generation,
            item.speed_epoch, 1, object(), time.perf_counter(), True, 2))
        def broken(_):
            raise RuntimeError("private model config")
        with pytest.raises(RuntimeError, match="^vehicle_inference_failed$"):
            vehicle_service_main(manager.config, manager.vehicle_requests, manager.vehicle_results,
                threading.Event(), manager.slot_generations, manager.speed_epochs, detector_factory=broken)
        assert manager.vehicle_requests.empty() and manager.vehicle_results[item.slot_id].empty()
    finally:
        close(services, manager)


@pytest.mark.parametrize("fair", [True, False])
def test_last_fight_removal_waits_for_acknowledged_work(fair):
    services, manager, registry, now = fixture({"fair_scheduling_enabled": fair, "shared_service_idle_grace_sec": 0})
    try:
        reconcile(services, manager, [camera("fight", True, False), camera("speed", False, True)])
        stage3 = manager.stage3_queue
        stage3.put(SimpleNamespace(slot_id=0))
        stage3.get(timeout=1)  # Inference is in flight even though the FIFO is empty.
        vehicle = services.processes()["vehicle"]
        reconcile(services, manager, [camera("speed", False, True)])
        assert "stage3" in services.processes() and vehicle is services.processes()["vehicle"]
        stage3.task_done()
        services.tick()
        if services.draining is not None:
            assert services.draining.wait(1)
            services.tick()
        assert set(services.processes()) == {"vehicle"}
    finally:
        close(services, manager)


def test_completed_file_and_local_consumer_failure_are_not_replayed_by_service_recovery(tmp_path):
    source = tmp_path / "file.mp4"
    source.touch()
    services, manager, registry, now = fixture()
    try:
        reconcile(services, manager, [camera("file", False, True, str(source)), camera("local", False, True)])
        file, local = manager.runtimes["file"], manager.runtimes["local"]
        file.processes["speed"].alive, file.processes["speed"].exitcode = False, 0
        file.file_eof_event.set()
        file.processes["ingest"].alive, file.processes["ingest"].exitcode = False, 0
        manager.disable_speed(local, "speed_process_dead")
        local.speed_restarts = 3  # Exhausted local budget must not be reset by shared recovery.
        local_process = local.processes["speed"]
        services.processes()["vehicle"].terminate()
        services.tick()
        manager.poll()
        assert file.file_done and not file.speed_failed
        now[0] += 2
        services.tick()
        manager.poll()
        assert local.processes["speed"] is local_process and local.speed_failed
    finally:
        close(services, manager)


def test_start_failure_is_bounded_for_vehicle_and_critical_for_fight():
    def fail(*args):
        raise OSError("spawn unavailable")
    services, manager, registry, now = fixture({"vehicle_service_restart_limit": 1}, factory=fail)
    try:
        reconcile(services, manager, [camera("speed", False, True)])
        assert manager.runtimes["speed"].speed_failed
        now[0] += 2
        services.tick()
        assert services.vehicle_failed and services.attempts == 1 and not services.bundles
        with pytest.raises(SharedServiceStartError):
            services.prepare([camera("fight", True, False)])
    finally:
        close(services, manager)


def test_recreated_vehicle_handles_are_windows_spawn_safe():
    ctx = mp.get_context("spawn")
    def spawn(name, target, args):
        process = ctx.Process(target=target, args=(*args[:7], SpawnVehicleDetector, *args[8:]))
        process.start()
        return process
    services, manager, registry, now = fixture(ctx=ctx, factory=spawn)
    try:
        services.prepare([camera("speed", False, True)])
        for incarnation in (1, 2):
            manager.slot_generations[0], manager.speed_epochs[0] = incarnation, incarnation
            manager.vehicle_requests.put(VehicleRequest("speed", 0, incarnation, incarnation,
                incarnation, None, time.perf_counter(), True, 2), timeout=1)
            result = manager.vehicle_results[0].get(timeout=10)
            assert result.request.epoch == incarnation and result.outcome == "accepted"
            process = services.processes()["vehicle"]
            process.terminate()
            process.join(3)
            if incarnation == 1:
                services.tick()
                now[0] += 2
                services.tick()
    finally:
        close(services, manager)


def test_speed_stream_source_ownership_in_isolated_django():
    backend = Path(__file__).resolve().parents[1] / "Fight_backend_project" / "backend_frontend_project"
    code = (
        "import os; os.environ['DJANGO_SETTINGS_MODULE']='backend_frontend_project.settings'; "
        "from django.conf import settings; "
        "settings.DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}}; "
        "import django; django.setup(); from django.core.management import call_command; "
        "call_command('test','speed_detection.phase14_tests',verbosity=0,interactive=False)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=backend, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("fight", [True, False])
def test_actual_dynamic_parent_only_starts_required_workers(tmp_path, monkeypatch, fight):
    from fight.pipeline_mp import run_multiprocess as runtime
    started, stop = [], []
    def spawn(name, target, args):
        started.append(name)
        return FakeProcess(name)
    monkeypatch.setattr(runtime, "_start_process", spawn)
    monkeypatch.setattr(CameraRuntimeManager, "_default_process_factory", staticmethod(spawn))
    monkeypatch.setattr(runtime, "install_signal_handlers", lambda event: stop.append(event))
    monkeypatch.setattr(runtime.time, "sleep", lambda _: stop[0].set())
    monkeypatch.setattr(runtime, "_close_queue", SharedServices._close)
    config = {"output_dir": str(tmp_path), "run_id": "parent", "cameras": [camera("cam", fight, not fight)],
              "runtime": {"health_enabled": False, "use_pose": False, "use_stage3": False, "dynamic_camera_slot_count": 2}}
    assert runtime._run_dynamic(config) == 0
    assert ("vehicle" in started) is (not fight)
    assert ("person" in started) is fight
    assert "pose" not in started and "stage3" not in started
    assert (tmp_path / "performance_summary.json").is_file()
