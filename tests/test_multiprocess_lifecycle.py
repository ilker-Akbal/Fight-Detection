from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fight.pipeline_mp import run_multiprocess


class _FakeProcess:
    next_pid = 1000

    def __init__(self, name: str):
        self.name = name
        self.pid = _FakeProcess.next_pid
        _FakeProcess.next_pid += 1
        self.exitcode = 0 if name.startswith("camera_") else None

    def is_alive(self):
        return not self.name.startswith("camera_")


class MultiprocessLifecycleTests(unittest.TestCase):
    def test_centralized_file_camera_starts_one_ingest_before_consumers_and_shuts_down_cleanly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "a.mp4"
            source.touch()
            config = {
                "output_dir": str(root / "output"),
                "cameras": [{"camera_id": "cam_A", "source": str(source)}],
                "runtime": {
                    "camera_ingest_mode": "centralized",
                    "use_pose": False,
                    "stage3_queue_size": 2,
                    "incident_queue_size": 2,
                    "report_queue_size": 64,
                    "person_request_queue_size": 1,
                    "person_result_queue_size": 1,
                    "person_camera_result_queue_size": 1,
                    "restart_camera_processes": False,
                    "loop_file_sources": False,
                },
            }
            started = []
            terminated = []

            def fake_start(name, target, args):
                process = _FakeProcess(name)
                started.append(process)
                return process

            with (
                patch.object(run_multiprocess, "_start_process", side_effect=fake_start),
                patch.object(
                    run_multiprocess,
                    "_terminate_process",
                    side_effect=lambda process, timeout=5.0: terminated.append(process.name)
                    if process is not None
                    else None,
                ),
                patch.object(run_multiprocess, "_wait_for_pipeline_settle"),
            ):
                exit_code = run_multiprocess.run(config)

            self.assertEqual(exit_code, 0)
            names = [process.name for process in started]
            self.assertEqual(names.count("camera_ingest_cam_A"), 1)
            self.assertLess(names.index("camera_ingest_cam_A"), names.index("camera_cam_A"))
            self.assertLess(names.index("camera_preview_cam_A"), names.index("camera_cam_A"))
            self.assertLess(
                terminated.index("camera_ingest_cam_A"),
                terminated.index("camera_cam_A"),
            )
            self.assertLess(
                terminated.index("camera_cam_A"),
                terminated.index("camera_preview_cam_A"),
            )

    def test_two_file_cameras_finish_and_shared_inference_components_shutdown_in_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_a = root / "a.mp4"
            source_b = root / "b.mp4"
            source_a.touch()
            source_b.touch()

            config = {
                "output_dir": str(root / "output"),
                "cameras": [
                    {"camera_id": "cam_A", "source": str(source_a)},
                    {"camera_id": "cam_B", "source": str(source_b)},
                ],
                "runtime": {
                    "stage3_queue_size": 2,
                    "incident_queue_size": 2,
                    "report_queue_size": 128,
                    "person_request_queue_size": 2,
                    "person_result_queue_size": 2,
                    "person_camera_result_queue_size": 1,
                    "restart_camera_processes": True,
                    "loop_file_sources": False,
                },
            }

            started = []
            terminated = []

            def fake_start(name, target, args):
                process = _FakeProcess(name)
                started.append(process)
                return process

            def fake_terminate(process, timeout=5.0):
                terminated.append(process.name)

            with (
                patch.object(run_multiprocess, "_start_process", side_effect=fake_start),
                patch.object(run_multiprocess, "_terminate_process", side_effect=fake_terminate),
                patch.object(run_multiprocess, "_wait_for_pipeline_settle") as settle,
            ):
                exit_code = run_multiprocess.run(config)

            self.assertEqual(exit_code, 0)
            performance_summary_path = root / "output" / "performance_summary.json"
            self.assertTrue(performance_summary_path.is_file())
            performance_summary = json.loads(
                performance_summary_path.read_text(encoding="utf-8")
            )
            self.assertEqual(performance_summary["camera_count"], 2)
            self.assertEqual(performance_summary["total_frames_read"], 0)
            settle.assert_called_once()
            self.assertEqual(
                [process.name for process in started],
                [
                    "reporter",
                    "incident",
                    "stage3",
                    "person_result_router",
                    "person_inference",
                    "pose_result_router",
                    "pose_inference",
                    "camera_cam_A",
                    "camera_cam_B",
                ],
            )
            self.assertEqual(
                terminated,
                [
                    "camera_cam_A",
                    "camera_cam_B",
                    "person_inference",
                    "person_result_router",
                    "pose_inference",
                    "pose_result_router",
                    "stage3",
                    "incident",
                    "reporter",
                ],
            )

    def test_pose_disabled_starts_no_pose_worker_or_router(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "a.mp4"
            source.touch()
            config = {
                "output_dir": str(root / "output"),
                "cameras": [{"camera_id": "cam_A", "source": str(source)}],
                "runtime": {
                    "use_pose": False,
                    "stage3_queue_size": 2,
                    "incident_queue_size": 2,
                    "report_queue_size": 64,
                    "person_request_queue_size": 1,
                    "person_result_queue_size": 1,
                    "person_camera_result_queue_size": 1,
                    "restart_camera_processes": False,
                    "loop_file_sources": False,
                },
            }
            started = []
            terminated = []

            def fake_start(name, target, args):
                process = _FakeProcess(name)
                started.append(process)
                return process

            with (
                patch.object(run_multiprocess, "_start_process", side_effect=fake_start),
                patch.object(
                    run_multiprocess,
                    "_terminate_process",
                    side_effect=lambda process, timeout=5.0: terminated.append(process.name)
                    if process is not None
                    else None,
                ),
                patch.object(run_multiprocess, "_wait_for_pipeline_settle"),
            ):
                exit_code = run_multiprocess.run(config)

            self.assertEqual(exit_code, 0)
            started_names = [process.name for process in started]
            self.assertNotIn("pose_inference", started_names)
            self.assertNotIn("pose_result_router", started_names)
            self.assertNotIn("pose_inference", terminated)
            self.assertNotIn("pose_result_router", terminated)


if __name__ == "__main__":
    unittest.main()
