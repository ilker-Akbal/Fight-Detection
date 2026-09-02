from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = (
    Path(__file__).resolve().parents[1]
    / "Fight_backend_project"
    / "backend_frontend_project"
)
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fight.runtime_supervisor.client import SupervisorUnavailable  # noqa: E402
from services.pipeline_bridge import fight_runner  # noqa: E402


class _UnavailableClient:
    def status(self):
        raise SupervisorUnavailable("offline")


class _StartedClient:
    def __init__(self, config_path):
        self.config_path = str(config_path)

    def start(self, _config_path):
        return {
            "ok": True,
            "result": "started",
            "supervisor_state": "RUNNING",
            "runtime_state": "RUNNING",
            "runtime_pid": 9876,
            "runtime_exit_code": None,
            "config_path": self.config_path,
            "run_id": "bridge-run-id",
        }


class SupervisorDjangoBridgeTests(unittest.TestCase):
    def test_supervisor_unavailable_is_not_reported_as_stopped(self):
        with (
            patch.object(fight_runner, "_control_mode", return_value="supervisor"),
            patch.object(fight_runner, "_supervisor_client", return_value=_UnavailableClient()),
        ):
            status = fight_runner.get_pipeline_status()
        self.assertFalse(status["available"])
        self.assertEqual(status["runtime_state"], "UNKNOWN")
        self.assertEqual(status["last_failure"], "supervisor_unavailable")

    def test_supervisor_mode_never_calls_django_popen(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run"
            run_dir.mkdir()
            config_path = run_dir / "run_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_name": "run",
                        "output_dir": str(run_dir),
                        "cameras": [{"camera_id": "cam_A", "source": "sample.mp4"}],
                    }
                ),
                encoding="utf-8",
            )
            prepared = (
                "run",
                run_dir,
                run_dir / "stdout.log",
                run_dir / "stderr.log",
                config_path,
                {},
            )
            with (
                patch.object(fight_runner, "_control_mode", return_value="supervisor"),
                patch.object(fight_runner, "_prepare_run", return_value=prepared),
                patch.object(
                    fight_runner,
                    "_supervisor_client",
                    return_value=_StartedClient(config_path),
                ),
                patch.object(fight_runner.subprocess, "Popen") as popen,
            ):
                active = fight_runner.start_pipeline(
                    [{"camera_id": "cam_A", "source": "sample.mp4"}]
                )
            popen.assert_not_called()
            self.assertIsNone(active.process)
            self.assertEqual(active.runtime_pid, 9876)
            self.assertEqual(active.run_id, "bridge-run-id")


if __name__ == "__main__":
    unittest.main()
