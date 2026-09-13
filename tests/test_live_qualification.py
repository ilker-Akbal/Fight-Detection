import copy
import json
import time
import threading
from types import SimpleNamespace

import pytest

from benchmarks.live_qualification import (
    JsonlTail, ProcessTree, Qualification, observation, prepare_config, source_type, set_fight_capability,
)
from fight.pipeline_mp.health import HealthEvent, HealthRegistry
from fight.runtime_supervisor.camera_state import DesiredCameraStateStore
from fight.pipeline_mp import common
from fight.pipeline_mp.run_multiprocess import _runtime_child_main


def sample(progress=10, epoch=1, service_epoch=1):
    return {"camera_generation": 1, "fight_consumer_epoch": epoch, "speed_epoch": 1,
        "fight_service_epoch": service_epoch, "vehicle_service_epoch": 1,
        "pids": {"ingest": 10, "preview": 11, "speed": 12, "camera": 13 + epoch,
                 "person": 20 + service_epoch, "vehicle": 30},
        "progress": {name: progress for name in ("ingest", "preview", "fight", "speed")},
        "health": "HEALTHY", "fight_enabled": True, "fight_waiting": False,
        "fight_failed": False, "speed_failed": False,
        "restarts": {"camera": 0, "fight_consumer": 0, "speed_consumer": 0,
                     "fight_service": service_epoch - 1, "vehicle": 0, "source_reconnects": 0},
        "capacity": {}, "written_wall_time": time.time()}


def finish(q, **kwargs):
    return q.finish(**{"duration": 10, "completed": True, "exit_code": 0,
                        "leaks": [], "system": {}, **kwargs})


@pytest.mark.parametrize("scenario", ["baseline", "fight-shared", "fight-local", "capability-churn", "shutdown-backoff"])
def test_qualification_expected_real_identity_observations(scenario):
    q = Qualification(scenario, "LIVE-LIKE / SYNTHETIC SOURCE", "camera")
    q.add(sample(), 0)
    if scenario != "baseline":
        q.action = {"role": "camera" if scenario == "fight-local" else "person"}
    if scenario == "fight-shared":
        pending = sample(12)
        pending.update(fight_waiting=True, health="DEGRADED")
        q.add(pending, 2)
    if scenario == "capability-churn":
        disabled = sample(12)
        disabled.update(fight_enabled=False, health="DEGRADED")
        q.add(disabled, 2)
    if scenario == "shutdown-backoff":
        pending = sample(12)
        pending.update(fight_waiting=True, health="DEGRADED")
        q.add(pending, 2)
    else:
        q.add(sample(20, 1 if scenario == "baseline" else 2,
                     2 if scenario == "fight-shared" else 1), 10)
    result = finish(q)
    assert result["classification"] == ("PASS" if scenario == "baseline" else "PASS_WITH_RECOVERY")
    assert result["stale_durable_publication_verified"] is None
    assert result["rss_bytes"]["start"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("key", ["camera_generation", "speed_epoch", "vehicle_service_epoch"])
def test_shared_fault_does_not_excuse_unrelated_incarnation_changes(key):
    q = Qualification("fight-shared", "RTSP", "camera")
    q.add(sample(), 0)
    q.action = {"role": "person"}
    changed = sample(20, 2, 2)
    changed[key] = 2
    q.add(changed, 2)
    q.add(sample(30, 2, 2), 3)  # Returning to an old value cannot erase failure.
    assert finish(q)["classification"] == "FAIL"
    assert "unexpected_" + key in finish(q)["failures"]


def test_leaks_storms_missing_progress_and_bounded_observations():
    q = Qualification("baseline", "RTSP", "camera", max_samples=2)
    for n in range(100):
        q.add(sample(n + 1), n, {"runtime_tree_rss_bytes": n + 10})
    assert len(q.samples) == 2
    result = finish(q, leaks=[{"pid": 100, "created": 1}])
    assert result["classification"] == "FAIL" and "child_process_leak" in result["failures"]
    assert result["rss_bytes"] == {"start": 10, "end": 109, "peak": 109, "samples": 100, "delta": 99}
    assert finish(Qualification("baseline", "RTSP", "camera"))["classification"] == "INCOMPLETE"
    assert finish(q, observation_errors=1)["classification"] == "FAIL"  # Leak failure stays latched.
    q2 = Qualification("baseline", "RTSP", "camera")
    q2.add(sample(), 0)
    assert finish(q2)["classification"] == "FAIL"  # No advancing branches.


def test_partial_jsonl_and_bounded_reader(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(b'{"a":1}\n{"b":')
    reader = JsonlTail(path)
    assert reader.read() == [{"a": 1}]
    assert reader.read() == [] and reader.offset == 8
    assert reader.pending_bytes == 5
    with path.open("ab") as handle:
        handle.write(b'2}\n')
    assert reader.read() == [{"b": 2}] and reader.errors == 0
    assert reader.pending_bytes == 0
    reader = JsonlTail(path)
    assert reader.read(budget=3) == [] and reader.errors == 1


def test_sources_never_masquerade_as_rtsp_and_protected_configuration_unchanged(tmp_path):
    base = {"models": {"person": "model.pt"}, "runtime": {"person_batch_enabled": False},
        "cameras": [{"camera_id": "mixed", "source": "movie.mp4", "use_speed_detection": True,
                     "speed_config": {"calibration": {"scale": 5}}}]}
    original = copy.deepcopy(base)
    config, label = prepare_config(base, tmp_path, generated_source="http://127.0.0.1/fixture")
    assert label == "LIVE-LIKE / SYNTHETIC SOURCE"
    assert config["models"] == original["models"]
    assert config["cameras"][0]["speed_config"] == original["cameras"][0]["speed_config"]
    assert base == original
    with pytest.raises(ValueError):
        source_type("movie.mp4")
    assert source_type("http://localhost/stream").startswith("LIVE HTTP")
    base["runtime"]["person_batch_enabled"] = True
    with pytest.raises(ValueError):
        prepare_config(base, tmp_path, generated_source="http://127.0.0.1/fixture")


def test_fault_target_requires_current_owned_identity_and_fresh_snapshot():
    class Process:
        pid = 2
        killed = False
        def create_time(self): return 1
        def is_running(self): return True
        def ppid(self): return 1
        def kill(self): self.killed = True
    process = Process()
    tree = ProcessTree.__new__(ProcessTree)
    tree.root = SimpleNamespace(pid=1, is_running=lambda: True)
    tree.known = {(2, 1): process}
    tree.capture = lambda: None
    row = {"written_wall_time": time.time(), "pids": {"person": 999}}
    with pytest.raises(RuntimeError):
        tree.inject(row, "person")
    row["pids"]["person"] = 2
    row["written_wall_time"] = 0
    with pytest.raises(RuntimeError):
        tree.inject(row, "person")
    assert not process.killed
    row["written_wall_time"] = time.time()
    tree.known[(2, .5)] = process
    with pytest.raises(RuntimeError):
        tree.inject(row, "person")
    assert not process.killed
    tree.known.pop((2, .5))
    assert tree.inject(row, "person")["pid"] == 2 and process.killed


def test_snapshot_exposes_existing_progress_and_pids_without_changing_classification():
    registry = HealthRegistry(monotonic=lambda: 100)
    registry.sync_cameras({"cam": {"slot_id": 0, "generation": 1,
        "pids": {"ingest": 123}, "state": "RUNNING", "fight_epoch": 1, "speed_epoch": 1}})
    for component in ("camera_ingest", "camera_preview"):
        registry.handle(HealthEvent(component, "camera", "frame_progress", 100,
            camera_id="cam", slot_id=0, generation=1, progress=12))
    before = registry.runtime_health
    row = observation(registry.snapshot("run"), "cam")
    assert row["pids"]["ingest"] == 123
    assert row["progress"]["ingest"] == row["progress"]["preview"] == 12
    assert registry.runtime_health == before


def test_capability_scenario_uses_real_desired_revision_contract(tmp_path):
    camera = {"camera_id": "cam", "source": "rtsp://localhost/live",
              "use_fight_detection": True, "use_speed_detection": True}
    store = DesiredCameraStateStore(tmp_path / "desired.json")
    store.bootstrap([camera])
    supervisor = SimpleNamespace(desired_cameras=store.load, update_desired_cameras=store.update)
    set_fight_capability(supervisor, camera, False)
    assert store.load()["revision"] == 2 and not store.load()["cameras"][0]["use_fight_detection"]
    set_fight_capability(supervisor, camera, True)
    assert store.load()["revision"] == 3 and store.load()["cameras"][0]["use_fight_detection"]


@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGBREAK"])
def test_windows_supervisor_break_reaches_normal_runtime_stop(monkeypatch, signal_name):
    handlers = {}
    stopped = threading.Event()
    held = threading.Lock()
    monkeypatch.setattr(common.signal, "SIGBREAK", 12345, raising=False)
    monkeypatch.setattr(common.signal, "signal", lambda signum, handler: handlers.update({signum: handler}))
    def set_stop():
        with held:
            stopped.set()
    event = SimpleNamespace(set=set_stop)
    common.install_signal_handlers(event)
    signum = getattr(common.signal, signal_name)
    with held:  # Simulate a signal interrupting the Event lock owner.
        handlers[signum](signum, None)
        handlers[signum](signum, None)  # Repeated signal is idempotent.
        assert not stopped.is_set()
    assert stopped.wait(1)


def test_windows_children_leave_group_break_shutdown_to_runtime_parent(monkeypatch):
    calls = []
    monkeypatch.setattr(common.signal, "SIGBREAK", 12345, raising=False)
    monkeypatch.setattr(common.signal, "signal", lambda *args: calls.append(args))
    _runtime_child_main(lambda value: calls.append(value), ("target_ran",))
    assert calls == [(12345, common.signal.SIG_IGN), "target_ran"]


def test_non_windows_child_wrapper_does_not_install_signal_handlers(monkeypatch):
    calls = []
    monkeypatch.delattr(common.signal, "SIGBREAK", raising=False)
    monkeypatch.setattr(common.signal, "signal", lambda *args: calls.append(args))
    _runtime_child_main(lambda: calls.append("target_ran"), ())
    assert calls == ["target_ran"]
