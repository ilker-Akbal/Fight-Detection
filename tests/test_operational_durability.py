import errno
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fight.operations import DiskMonitor, atomic_json, run_lock_path
from fight.pipeline.incident_outbox import DurableWriteError, append_envelope_durable
from fight.pipeline_mp.health import DEGRADED, FAILED, HealthPolicy, HealthRegistry
from fight.retention import RetentionPass
from fight.runtime_supervisor.locking import SingletonLock, SingletonLockError
from fight.service_loop import run_service
from tests.test_incident_outbox import _envelope


NOW = 300 * 86400


def artifact(path, content=b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (1, 1))
    return path


def completed(root, name):
    run = root / name
    artifact(run / ".run_state.json", json.dumps({"state": "COMPLETED", "run_id": name}).encode())
    return run


def test_retention_protects_active_current_referenced_and_unconsumed_evidence(tmp_path):
    active = completed(tmp_path, "active")
    current = completed(tmp_path, "current")
    old = completed(tmp_path, "old")
    for run in (active, current, old):
        artifact(run / "runtime_health.json")
    evidence = artifact(old / "incidents" / "referenced.mp4")
    unconsumed = artifact(old / "incidents" / "pending.mp4")
    durable = artifact(old / "incidents.jsonl", b'{"partial":')
    stale = artifact(old / "runtime_health.json.tmp", b"partial")
    unknown = artifact(tmp_path / "aborted" / "runtime_health.json.tmp")
    with SingletonLock(run_lock_path(active)):
        cleaner = RetentionPass({"evidence_days": 180}, referenced=lambda path: path == evidence, now=NOW)
        cleaner.runs(tmp_path, protected=[current])
    assert (active / "runtime_health.json").exists()
    assert (current / "runtime_health.json").exists()
    assert not (old / "runtime_health.json").exists()
    assert not stale.exists()
    assert unknown.exists() and durable.exists()
    assert evidence.exists() and unconsumed.exists()
    cleaner = RetentionPass({"evidence_days": 1}, referenced=lambda path: path == evidence,
                            evidence_safe=True, now=NOW)
    cleaner.runs(tmp_path, protected=[active, current])
    assert evidence.exists() and not unconsumed.exists()


def test_cleanup_is_bounded_idempotent_and_removes_closed_logs_and_empty_runs(tmp_path):
    root = tmp_path / "runs"
    logs = tmp_path / "logs"
    run = completed(root, "old")
    for name in ("events.jsonl", "runtime_health.json", "run_config.json"):
        artifact(run / name)
    old_log = artifact(logs / "runtime-old.stdout.log")
    active_log = artifact(logs / "runtime-current.stdout.log")
    unknown_log = artifact(logs / "runtime-unverified.stdout.log")
    policy = {"max_files": 2}
    counts = []
    for _ in range(5):
        cleaner = RetentionPass(policy, referenced=lambda _: False, now=NOW)
        cleaner.runs(root, log_root=logs)
        cleaner.logs(logs, "current")
        counts.append(cleaner.stats["removed"])
        assert cleaner.stats["removed"] <= 2
    assert counts[-1] == 0 and not run.exists() and not old_log.exists()
    assert active_log.exists() and unknown_log.exists()
    cleaner = RetentionPass({"max_scan": 1}, referenced=lambda _: False, now=NOW)
    cleaner.logs(logs, "current")
    assert cleaner.stats["scanned"] == 1 and cleaner.stats["scan_limited"]


def test_disk_pressure_degrades_without_watchdog_actions_and_does_not_mask_dead_workers(tmp_path):
    usage = SimpleNamespace(free=15, total=100)
    monitor = DiskMonitor({"disk_warning_bytes": 20, "disk_critical_bytes": 10,
                           "disk_check_interval_sec": 1}, usage=lambda _: usage, monotonic=lambda: 100)
    registry = HealthRegistry(monotonic=lambda: 100)
    for free, state in ((15, "WARNING"), (5, "CRITICAL")):
        usage.free = free
        monitor._checked = 0
        registry.disk = monitor.sample({"runs": tmp_path})
        assert registry.disk["state"] == state
        actions, _ = registry.evaluate(HealthPolicy())
        assert actions == [] and registry.runtime_health == DEGRADED
        assert registry.snapshot("run")["reason"] == "disk_pressure"
    registry.register_worker("person")
    registry.evaluate(HealthPolicy(), worker_process_alive={"person": False})
    assert registry.runtime_health == FAILED


def test_outbox_partial_tail_and_atomic_failure_preserve_previous_bytes(tmp_path, monkeypatch):
    target = artifact(tmp_path / "outbox.jsonl", b'{"event_id":"unfinished')
    prefix = target.read_bytes()
    envelope = _envelope()
    append_envelope_durable(target, envelope)
    assert target.read_bytes().startswith(prefix + b"\n")
    assert json.loads(target.read_bytes().splitlines()[1])["event_id"] == envelope.event_id
    state = tmp_path / "state.json"
    atomic_json(state, {"revision": 1})
    original = state.read_bytes()
    with monkeypatch.context() as patch:
        def disk_full(_):
            raise OSError(errno.ENOSPC, "disk full")
        patch.setattr(os, "fsync", disk_full)
        with pytest.raises(DurableWriteError, match="outbox_persistence_failed"):
            append_envelope_durable(target, _envelope())
        with pytest.raises(OSError):
            atomic_json(state, {"revision": 2})
    assert state.read_bytes() == original
    assert state.with_suffix(".json.tmp").exists()
    atomic_json(state, {"revision": 3})
    assert json.loads(state.read_text())["revision"] == 3


def test_incident_write_failure_never_emits_legacy_success(tmp_path, monkeypatch, capsys):
    from fight.pipeline.incident_aggregator import IncidentAggregator, Stage3Result

    aggregator = IncidentAggregator(out_dir=tmp_path / "incidents", run_id="run",
                                    outbox_path=tmp_path / "outbox.jsonl", single_strong_fight_thr=0.5)
    aggregator._stop_event.set()
    aggregator._sweeper.join(2)
    part = artifact(tmp_path / "part.mp4")
    monkeypatch.setattr(aggregator, "_wait_clips_ready", lambda _: True)
    monkeypatch.setattr(aggregator, "_concat_mp4s", lambda _, output: bool(artifact(output)))
    monkeypatch.setattr(aggregator, "_add_ai_overlay_to_clip", lambda *_: True)
    attempted = []
    def fail(*_):
        attempted.append(True)
        raise DurableWriteError(errno.ENOSPC, "outbox_persistence_failed")
    monkeypatch.setattr("fight.pipeline.incident_aggregator.append_envelope_durable", fail)
    aggregator.submit(Stage3Result(camera_id="cam", source="0", event_id="event",
                                  event_start_ts=100, event_end_ts=102, clip_path=str(part),
                                  fight_prob=0.95, fight_label="fight", pose_score_max=0.9, pose_score_mean=0.8))
    capsys.readouterr()
    with pytest.raises(DurableWriteError):
        aggregator.finalize("cam", force=True)
    assert attempted == [True]
    assert "[INCIDENT] camera=" not in capsys.readouterr().out
    assert not (tmp_path / "incidents.jsonl").exists()
    assert part.exists()
    aggregator._fatal_error = DurableWriteError("sweeper_write_failed")
    with pytest.raises(DurableWriteError):
        aggregator.close_all()


def test_service_singleton_backoff_and_interruptible_shutdown(tmp_path):
    path = tmp_path / "service.lock"
    with SingletonLock(path):
        with pytest.raises(SingletonLockError):
            run_service(lambda: {}, path, once=True)
    class Stop:
        delays = []
        def is_set(self):
            return len(self.delays) == 4
        def wait(self, delay):
            self.delays.append(delay)
    stop = Stop()
    rows = []
    def fail():
        raise OSError(errno.ENOSPC, "rtsp://secret:password@source")
    run_service(fail, path, interval=1, max_backoff=5, stop=stop, report=rows.append)
    assert stop.delays == [2, 4, 5, 5]
    assert "password" not in json.dumps(rows)
    assert not SingletonLock(path).acquired
    run_service(lambda: {}, path, once=True)  # Lock was released after shutdown.


def test_orm_cleanup_guards_in_isolated_database():
    # Never connect tests to the configured production database. Keep Django's
    # global app/settings state out of the existing runtime-only test process.
    backend = Path(__file__).resolve().parents[1] / "Fight_backend_project" / "backend_frontend_project"
    code = (
        "import os; os.environ['DJANGO_SETTINGS_MODULE']='backend_frontend_project.settings'; "
        "from django.conf import settings; "
        "settings.DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}}; "
        "import django; django.setup(); from django.core.management import call_command; "
        "call_command('test','incidents.phase12_tests',verbosity=0,interactive=False)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=backend, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
