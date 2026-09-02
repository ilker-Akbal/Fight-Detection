from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import queue

from fight.runtime_supervisor.client import (
    RuntimeSupervisorClient,
    SupervisorRequestError,
    SupervisorUnavailable,
)
from fight.runtime_supervisor.core import (
    FAILED,
    RUNNING,
    STOPPED,
    RuntimeSupervisor,
    SupervisorConfig,
)
from fight.runtime_supervisor.http_api import create_http_server
from fight.runtime_supervisor.locking import SingletonLock, SingletonLockError
from fight.pipeline_mp.messages import ReportMessage
from fight.pipeline_mp.performance import build_performance_summary
from fight.pipeline_mp.reporter import reporter_process_main


class FakeChild:
    next_pid = 41000

    def __init__(self, *, graceful=True, hanging=False):
        self.pid = FakeChild.next_pid
        FakeChild.next_pid += 1
        self.returncode = None
        self.graceful = graceful
        self.hanging = hanging
        self.signals = []
        self.terminate_count = 0
        self.kill_count = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if self.hanging:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = 0
        return 0

    def send_signal(self, value):
        self.signals.append(value)
        if self.graceful and not self.hanging:
            self.returncode = 0

    def terminate(self):
        self.terminate_count += 1
        if not self.hanging:
            self.returncode = -15

    def kill(self):
        self.kill_count += 1
        self.returncode = -9


class Factory:
    def __init__(self, child_builder=None):
        self.calls = []
        self.children = []
        self.child_builder = child_builder or (lambda: FakeChild())

    def __call__(self, command, **kwargs):
        child = self.child_builder()
        self.calls.append((list(command), dict(kwargs)))
        self.children.append(child)
        return child


class RuntimeSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.supervisors = []
        self.root = Path(self.temp.name)
        self.config_path = self.root / "run.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "output_dir": str(self.root / "output"),
                    "cameras": [
                        {
                            "camera_id": "cam_A",
                            "source": "rtsp://camera-user:camera-password@example.test/live",
                        }
                    ],
                    "runtime": {},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        for supervisor in reversed(self.supervisors):
            supervisor.close(stop_runtime=True)
        self.temp.cleanup()

    def make_supervisor(self, factory=None, **kwargs):
        config = SupervisorConfig(
            repo_root=self.root,
            state_dir=self.root / "state",
            allowed_config_dirs=(self.root,),
            stop_grace_sec=0.01,
            kill_grace_sec=0.01,
            monitor_interval_sec=0.01,
        )
        supervisor = RuntimeSupervisor(
            config,
            popen_factory=factory or Factory(),
            platform_name=kwargs.pop("platform_name", "nt"),
            start_monitor=kwargs.pop("start_monitor", False),
            **kwargs,
        )
        self.supervisors.append(supervisor)
        return supervisor

    def test_starts_stopped_and_start_is_idempotent(self):
        factory = Factory()
        supervisor = self.make_supervisor(factory)
        self.assertEqual(supervisor.status()["runtime_state"], STOPPED)
        first = supervisor.start(self.config_path)
        second = supervisor.start(self.config_path)
        self.assertEqual(first["runtime_state"], RUNNING)
        self.assertEqual(first["result"], "started")
        self.assertEqual(second["result"], "already_running")
        self.assertEqual(len(factory.calls), 1)
        safe_metadata = repr(second) + supervisor.state_path.read_text(encoding="utf-8")
        safe_metadata += supervisor.telemetry_path.read_text(encoding="utf-8")
        self.assertNotIn("camera-user", safe_metadata)
        self.assertNotIn("camera-password", safe_metadata)
        self.assertGreater(factory.calls[0][1]["creationflags"], 0)
        command = factory.calls[0][0]
        self.assertEqual(command[1:4], ["-m", "fight.pipeline_mp.run_multiprocess", "--config"])

    def test_ten_concurrent_starts_spawn_exactly_one_child(self):
        factory = Factory()
        supervisor = self.make_supervisor(factory)
        barrier = threading.Barrier(10)

        def start_once():
            barrier.wait()
            return supervisor.start(self.config_path)["result"]

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _: start_once(), range(10)))
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(results.count("started"), 1)
        self.assertEqual(results.count("already_running"), 9)

    def test_stop_is_graceful_and_idempotent(self):
        factory = Factory()
        supervisor = self.make_supervisor(factory)
        supervisor.start(self.config_path)
        stopped = supervisor.stop()
        again = supervisor.stop()
        self.assertEqual(stopped["result"], "stopped")
        self.assertEqual(again["result"], "already_stopped")
        self.assertEqual(factory.children[0].returncode, 0)
        self.assertEqual(supervisor.status()["runtime_state"], STOPPED)

    def test_restart_closes_old_child_first_and_run_id_is_unique(self):
        factory = Factory()
        supervisor = self.make_supervisor(factory)
        first = supervisor.start(self.config_path)
        first_child = factory.children[0]
        second = supervisor.restart()
        self.assertIsNotNone(first_child.returncode)
        self.assertEqual(len(factory.calls), 2)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["restart_count"], 1)
        launch_config = json.loads(
            Path(second["launch_config_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(launch_config["run_id"], second["run_id"])
        self.assertEqual(launch_config["runtime"]["run_id"], second["run_id"])

    def test_unexpected_exit_records_failed_state_and_exit_code(self):
        factory = Factory()
        supervisor = self.make_supervisor(factory)
        supervisor.start(self.config_path)
        factory.children[0].returncode = 17
        status = supervisor.status()
        self.assertEqual(status["runtime_state"], FAILED)
        self.assertEqual(status["runtime_exit_code"], 17)
        self.assertIn("17", status["last_failure"])

    def test_windows_forced_tree_termination_fallback(self):
        factory = Factory(lambda: FakeChild(graceful=False, hanging=True))
        taskkill_calls = []

        def command_runner(command, **_kwargs):
            taskkill_calls.append(command)
            factory.children[0].returncode = -9

        supervisor = self.make_supervisor(factory, command_runner=command_runner)
        supervisor.start(self.config_path)
        supervisor.stop()
        self.assertEqual(taskkill_calls[0][0], "taskkill")
        self.assertIn("/T", taskkill_calls[0])
        self.assertIn("/F", taskkill_calls[0])

    def test_linux_process_group_path_uses_term_before_kill(self):
        factory = Factory(lambda: FakeChild(graceful=False, hanging=True))
        supervisor = self.make_supervisor(factory, platform_name="posix")
        supervisor.start(self.config_path)
        child = factory.children[0]
        signals = []

        def fake_killpg(pid, sig):
            signals.append((pid, sig))
            if len(signals) == 2:
                child.returncode = -9

        with patch(
            "fight.runtime_supervisor.core.os.killpg",
            side_effect=fake_killpg,
            create=True,
        ):
            supervisor.stop()
        self.assertEqual(len(signals), 2)
        self.assertNotEqual(signals[0][1], signals[1][1])

    def test_persisted_stale_pid_is_not_killed(self):
        state_dir = self.root / "state"
        state_dir.mkdir()
        (state_dir / "runtime_state.json").write_text(
            json.dumps({"runtime_state": "RUNNING", "runtime_pid": 999999}),
            encoding="utf-8",
        )
        supervisor = self.make_supervisor(pid_exists=lambda _pid: False)
        status = supervisor.status()
        self.assertEqual(status["runtime_state"], STOPPED)
        self.assertIsNone(status["runtime_pid"])

    def test_live_persisted_pid_becomes_orphan_detected_without_kill(self):
        state_dir = self.root / "state"
        state_dir.mkdir()
        (state_dir / "runtime_state.json").write_text(
            json.dumps({"runtime_state": "RUNNING", "runtime_pid": 12345}),
            encoding="utf-8",
        )
        supervisor = self.make_supervisor(pid_exists=lambda _pid: True)
        status = supervisor.status()
        self.assertEqual(status["runtime_state"], FAILED)
        self.assertTrue(status["orphan_detected"])
        self.assertEqual(status["runtime_pid"], 12345)

    def test_config_outside_allowlist_is_rejected(self):
        outside_dir = tempfile.TemporaryDirectory()
        try:
            outside = Path(outside_dir.name) / "run.json"
            outside.write_text(self.config_path.read_text(encoding="utf-8"), encoding="utf-8")
            supervisor = self.make_supervisor()
            with self.assertRaises(ValueError):
                supervisor.start(outside)
        finally:
            outside_dir.cleanup()

    def test_http_health_status_auth_and_secret_redaction(self):
        secret = "do-not-log-this-token"
        factory = Factory()
        supervisor = self.make_supervisor(factory)
        server = create_http_server(supervisor, host="127.0.0.1", port=0, token=secret)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            valid = RuntimeSupervisorClient(url, secret, timeout=1.0)
            invalid = RuntimeSupervisorClient(url, "wrong", timeout=1.0)
            self.assertTrue(valid.health()["ok"])
            self.assertEqual(valid.status()["runtime_state"], STOPPED)
            with self.assertRaises(SupervisorRequestError):
                invalid.start(str(self.config_path))
            self.assertEqual(len(factory.calls), 0)
            self.assertEqual(valid.start(str(self.config_path))["runtime_state"], RUNNING)
            self.assertNotIn(secret, supervisor.telemetry_path.read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_unavailable_client_is_explicit(self):
        client = RuntimeSupervisorClient("http://127.0.0.1:1", "token", timeout=0.1)
        with self.assertRaises(SupervisorUnavailable):
            client.status()

    def test_singleton_lock_rejects_second_owner(self):
        path = self.root / "supervisor.lock"
        first = SingletonLock(path)
        second = SingletonLock(path)
        first.acquire()
        try:
            with self.assertRaises(SingletonLockError):
                second.acquire()
        finally:
            first.release()

    def test_reporter_and_performance_summary_carry_run_id(self):
        output_dir = self.root / "telemetry"
        reports = queue.Queue()
        reports.put(
            ReportMessage(
                kind="status",
                row={"camera_id": "cam_A", "stage": "camera", "detail": "ok"},
            )
        )
        reports.put(None)
        config = {
            "run_id": "run-test-123",
            "run_name": "fixture",
            "output_dir": str(output_dir),
            "cameras": [{"camera_id": "cam_A"}],
            "runtime": {},
        }
        reporter_process_main(config, reports, threading.Event())
        rows = [
            json.loads(line)
            for line in (output_dir / "camera_status.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertTrue(rows)
        self.assertTrue(all(row["run_id"] == "run-test-123" for row in rows))
        summary = build_performance_summary(config, rows, 1.0)
        self.assertEqual(summary["run_id"], "run-test-123")


if __name__ == "__main__":
    unittest.main()
