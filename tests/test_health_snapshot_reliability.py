"""Bounded Windows replacement retries preserve the last complete snapshot."""
import errno
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from fight.pipeline_mp.health import HealthSnapshotStore


def windows_error(code=32):
    error = PermissionError(errno.EACCES, "snapshot replacement denied")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_snapshot_replace_retry_keeps_old_json_until_success(tmp_path, monkeypatch, code):
    path = tmp_path / "health.json"
    store = HealthSnapshotStore(path)
    store.write({"revision": 1})
    original = Path.replace
    attempts = []
    def replace(source, target):
        assert json.loads(path.read_text()) == {"revision": 1}
        assert json.loads(source.read_text()) == {"revision": 2}
        attempts.append(source)
        if len(attempts) < 3:
            raise windows_error(code)
        return original(source, target)
    sleep = Mock()
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr("fight.pipeline_mp.health.time.sleep", sleep)
    store.write({"revision": 2})
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert [call.args[0] for call in sleep.call_args_list] == [.02, .04]
    assert json.loads(path.read_text()) == {"revision": 2}
    assert not path.with_suffix(".json.tmp").exists()


def test_exhausted_snapshot_retry_raises_and_next_write_recovers(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    store = HealthSnapshotStore(path)
    store.write({"revision": 1})
    original = path.read_bytes()
    error = windows_error()
    replace, sleep = Mock(side_effect=error), Mock()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", replace)
        patch.setattr("fight.pipeline_mp.health.time.sleep", sleep)
        with pytest.raises(PermissionError) as failed:
            store.write({"revision": 2})
        assert failed.value is error
    assert replace.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [.02, .04, .08]
    assert path.read_bytes() == original
    store.write({"revision": 3})
    assert json.loads(path.read_text()) == {"revision": 3}
    assert not path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "denied"),
    OSError(errno.ENOSPC, "full"), FileNotFoundError(errno.ENOENT, "missing")])
def test_unrelated_snapshot_errors_are_not_retried(tmp_path, monkeypatch, error):
    path = tmp_path / "health.json"
    store = HealthSnapshotStore(path)
    store.write({"revision": 1})
    replace, sleep = Mock(side_effect=error), Mock()
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr("fight.pipeline_mp.health.time.sleep", sleep)
    with pytest.raises(OSError) as failed:
        store.write({"revision": 2})
    assert failed.value is error and replace.call_count == 1
    sleep.assert_not_called()
    assert json.loads(path.read_text()) == {"revision": 1}


@pytest.mark.skipif(os.name != "nt", reason="Windows read/replace sharing semantics")
def test_real_windows_reader_blocks_replace_until_handle_closes(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    store = HealthSnapshotStore(path)
    store.write({"revision": 1})
    with path.open("rb") as reader:
        waits = []
        def release_reader(delay):
            waits.append(delay)
            assert json.loads(reader.read()) == {"revision": 1}
            reader.close()
        monkeypatch.setattr("fight.pipeline_mp.health.time.sleep", release_reader)
        store.write({"revision": 2})  # Real Path.replace, no synthetic filesystem exception.
    assert waits == [.02]
    assert json.loads(path.read_text()) == {"revision": 2}


def test_exhausted_snapshot_retry_is_reported_nonfatally_by_dynamic_parent(tmp_path, monkeypatch):
    from fight.pipeline_mp import run_multiprocess as runtime
    from fight.pipeline_mp.camera_lifecycle import CameraRuntimeManager
    from fight.pipeline_mp.shared_services import SharedServices
    from tests.test_dynamic_camera_lifecycle import FakeProcess
    from tests.test_speed_integration import camera
    stopped, rows, waits = [], [], []
    monkeypatch.setattr(runtime, "_start_process", lambda name, *_: FakeProcess(name))
    monkeypatch.setattr(CameraRuntimeManager, "_default_process_factory",
                        staticmethod(lambda name, *_: FakeProcess(name)))
    monkeypatch.setattr(runtime, "install_signal_handlers", lambda event: stopped.append(event))
    monkeypatch.setattr(runtime, "_put_status", lambda _, row: rows.append(row))
    monkeypatch.setattr(runtime, "_close_queue", SharedServices._close)
    def sleep(delay):
        if delay in (.02, .04, .08):
            waits.append(delay)
        else:
            stopped[0].set()
    monkeypatch.setattr(runtime.time, "sleep", sleep)
    original = Path.replace
    def replace(source, target):
        if Path(target).name == "runtime_health.json":
            raise windows_error()
        return original(source, target)
    monkeypatch.setattr(Path, "replace", replace)
    config = {"output_dir": str(tmp_path), "cameras": [camera("cam", True, False)],
              "runtime": {"use_pose": False, "use_stage3": False, "dynamic_camera_slot_count": 1}}
    assert runtime._run_dynamic(config) == 0
    assert waits == [.02, .04, .08]
    failed = [row for row in rows if row["detail"] == "health_snapshot_write_failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "PermissionError"
    assert failed[0]["errno"] == errno.EACCES and failed[0]["winerror"] == 32
