from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import time
import unittest
from types import SimpleNamespace

from fight.pipeline_mp.messages import PoseInferenceRequest, PoseInferenceResult
from fight.pipeline_mp.pose_worker import (
    PoseInferenceClient,
    PoseInferenceTimeout,
    PoseResultValidator,
    pose_result_router_main,
    route_pose_result,
    run_pose_inference_loop,
    should_request_pose_inference,
)
from fight.pose.src.pose_gate import PoseGate


def _raw_pose(score: float = 0.8, ok: bool = True):
    return SimpleNamespace(score=score, ok=ok, hist_positive=0, debug_frame=None)


def _result(
    camera_id: str,
    generation: int,
    frame_idx: int,
    request_id: int,
    score: float = 0.8,
):
    return PoseInferenceResult(
        camera_id=camera_id,
        generation=generation,
        frame_idx=frame_idx,
        request_id=request_id,
        pose_result=_raw_pose(score=score),
    )


class SharedPoseRoutingTests(unittest.TestCase):
    def test_two_cameras_route_to_camera_specific_channels(self):
        channels = {"cam_A": queue.Queue(), "cam_B": queue.Queue()}
        result_a = _result("cam_A", 1, 100, 1, 0.71)
        result_b = _result("cam_B", 1, 100, 1, 0.82)

        self.assertEqual(route_pose_result(result_a, channels, 0.01), (True, "routed"))
        self.assertEqual(route_pose_result(result_b, channels, 0.01), (True, "routed"))
        self.assertIs(channels["cam_A"].get_nowait(), result_a)
        self.assertIs(channels["cam_B"].get_nowait(), result_b)

    def test_reordered_cross_camera_results_stay_isolated(self):
        channels = {"cam_A": queue.Queue(), "cam_B": queue.Queue()}
        route_pose_result(_result("cam_B", 2, 100, 4), channels, 0.01)
        route_pose_result(_result("cam_A", 3, 100, 7), channels, 0.01)

        self.assertEqual(channels["cam_A"].get_nowait().camera_id, "cam_A")
        self.assertEqual(channels["cam_B"].get_nowait().camera_id, "cam_B")

    def test_router_channels_are_spawn_process_safe(self):
        ctx = mp.get_context("spawn")
        shared_results = ctx.JoinableQueue(maxsize=4)
        channels = {"cam_A": ctx.Queue(maxsize=1), "cam_B": ctx.Queue(maxsize=1)}
        reports = ctx.Queue(maxsize=8)
        stop_event = ctx.Event()
        process = ctx.Process(
            target=pose_result_router_main,
            args=(
                {"runtime": {"pose_route_timeout_sec": 0.1}},
                shared_results,
                channels,
                reports,
                stop_event,
            ),
        )

        try:
            process.start()
            shared_results.put(_result("cam_B", 1, 100, 1), timeout=1.0)
            shared_results.put(_result("cam_A", 1, 100, 1), timeout=1.0)
            shared_results.put(None, timeout=1.0)

            self.assertEqual(channels["cam_A"].get(timeout=5.0).camera_id, "cam_A")
            self.assertEqual(channels["cam_B"].get(timeout=5.0).camera_id, "cam_B")
            process.join(timeout=5.0)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
            for channel in channels.values():
                channel.close()
                channel.join_thread()
            shared_results.close()
            shared_results.join_thread()
            reports.close()
            reports.join_thread()


class PoseResultValidationTests(unittest.TestCase):
    def test_stale_generation_is_rejected(self):
        validator = PoseResultValidator("cam_A", generation=2)
        self.assertEqual(
            validator.validate(
                _result("cam_A", 1, 100, 1),
                expected_frame_idx=100,
                expected_request_id=1,
            ),
            (False, "stale_generation"),
        )

    def test_duplicate_wrong_camera_request_and_frame_are_rejected(self):
        validator = PoseResultValidator("cam_A", generation=2)
        accepted = _result("cam_A", 2, 100, 1)
        self.assertEqual(
            validator.validate(accepted, expected_frame_idx=100, expected_request_id=1),
            (True, "accepted"),
        )
        validator.mark_completed(1)

        cases = [
            (accepted, "duplicate_result"),
            (_result("cam_B", 2, 100, 2), "wrong_camera_id"),
            (_result("cam_A", 2, 100, 9), "unexpected_request_id"),
            (_result("cam_A", 2, 101, 2), "unexpected_frame_idx"),
            ({"not": "a pose result"}, "invalid_message_type"),
        ]
        for result, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                self.assertEqual(
                    validator.validate(result, expected_frame_idx=100, expected_request_id=2),
                    (False, expected_reason),
                )


class PoseInferenceClientTests(unittest.TestCase):
    def test_stale_result_is_dropped_before_matching_result(self):
        requests = queue.Queue(maxsize=1)
        results = queue.Queue(maxsize=2)
        reports = queue.Queue()
        results.put(_result("cam_A", 1, 100, 1))
        results.put(_result("cam_A", 2, 100, 1, 0.73))

        client = PoseInferenceClient(
            camera_id="cam_A",
            generation=2,
            request_queue=requests,
            result_queue=results,
            report_queue=reports,
            stop_event=threading.Event(),
            inference_timeout_sec=0.2,
            enqueue_timeout_sec=0.01,
        )
        raw = client.infer(roi="roi", frame_idx=100)

        self.assertEqual(raw.score, 0.73)
        self.assertEqual(requests.get_nowait().request_id, 1)
        self.assertEqual(reports.get_nowait().row["drop_reason"], "stale_generation")

    def test_duplicate_result_does_not_update_local_gate_twice(self):
        requests = queue.Queue(maxsize=2)
        results = queue.Queue(maxsize=3)
        reports = queue.Queue()
        first = _result("cam_A", 1, 100, 1, 0.8)
        results.put(first)
        results.put(first)
        results.put(_result("cam_A", 1, 102, 2, 0.7))
        client = PoseInferenceClient(
            camera_id="cam_A",
            generation=1,
            request_queue=requests,
            result_queue=results,
            report_queue=reports,
            stop_event=threading.Event(),
            inference_timeout_sec=0.2,
            enqueue_timeout_sec=0.01,
        )
        gate = PoseGate(window_size=6, need_positive=2)

        for frame_idx in (100, 102):
            raw = client.infer(roi="roi", frame_idx=frame_idx)
            gate.update(raw.score, raw.ok)

        self.assertEqual(len(gate.hist), 2)
        drops = []
        while not reports.empty():
            drops.append(reports.get_nowait().row.get("drop_reason"))
        self.assertIn("duplicate_result", drops)

    def test_timeout_returns_without_deadlock(self):
        client = PoseInferenceClient(
            camera_id="cam_A",
            generation=1,
            request_queue=queue.Queue(maxsize=1),
            result_queue=queue.Queue(maxsize=1),
            report_queue=queue.Queue(),
            stop_event=threading.Event(),
            inference_timeout_sec=0.03,
            enqueue_timeout_sec=0.01,
        )

        started = time.monotonic()
        with self.assertRaises(PoseInferenceTimeout):
            client.infer(roi="roi", frame_idx=100)
        self.assertLess(time.monotonic() - started, 0.5)


class PoseWorkerTests(unittest.TestCase):
    def test_model_is_initialized_once_for_multiple_cameras(self):
        class FakePoseAdapter:
            initialization_count = 0
            weights = "fake-pose.pt"

            def __init__(self, cfg_path):
                FakePoseAdapter.initialization_count += 1

            def evaluate(self, roi):
                return _raw_pose(score=0.88)

        requests = queue.Queue()
        results = queue.Queue()
        reports = queue.Queue()
        for camera_id in ("cam_A", "cam_B"):
            requests.put(
                PoseInferenceRequest(
                    camera_id=camera_id,
                    generation=1,
                    frame_idx=100,
                    request_id=1,
                    roi=camera_id,
                )
            )
        requests.put(None)

        run_pose_inference_loop(
            {
                "models": {"pose_config": "pose.yaml"},
                "runtime": {"pose_result_enqueue_timeout_sec": 0.01},
            },
            requests,
            results,
            reports,
            threading.Event(),
            adapter_factory=FakePoseAdapter,
        )

        self.assertEqual(FakePoseAdapter.initialization_count, 1)
        self.assertEqual(
            {results.get_nowait().camera_id, results.get_nowait().camera_id},
            {"cam_A", "cam_B"},
        )
        initialized = []
        while not reports.empty():
            row = reports.get_nowait().row
            if row.get("detail") == "model_initialized":
                initialized.append(row)
        self.assertEqual(len(initialized), 1)
        self.assertEqual(initialized[0]["model_instance_count"], 1)

    def test_shutdown_sentinel_stops_worker_loop(self):
        class FakePoseAdapter:
            def __init__(self, cfg_path):
                pass

            def evaluate(self, roi):
                raise AssertionError("sentinel must not run inference")

        requests = queue.Queue()
        requests.put(None)
        run_pose_inference_loop(
            {"models": {"pose_config": "pose.yaml"}},
            requests,
            queue.Queue(),
            queue.Queue(),
            threading.Event(),
            adapter_factory=FakePoseAdapter,
        )
        self.assertTrue(requests.empty())


class PoseGatingAndLocalStateTests(unittest.TestCase):
    def test_disabled_missing_roi_and_stride_skip_do_not_request(self):
        self.assertFalse(should_request_pose_inference(False, True, 100, 2))
        self.assertFalse(should_request_pose_inference(True, False, 100, 2))
        self.assertFalse(should_request_pose_inference(True, True, 101, 2))
        self.assertTrue(should_request_pose_inference(True, True, 100, 2))

    def test_pose_gate_history_is_camera_local(self):
        gate_a = PoseGate(window_size=6, need_positive=2)
        gate_b = PoseGate(window_size=6, need_positive=2)
        self.assertIsNot(gate_a, gate_b)

        gate_a.update(0.8, True)
        gate_b.update(0.8, True)
        gate_a.update(0.7, True)

        self.assertEqual(list(gate_a.hist), [True, True])
        self.assertEqual(list(gate_b.hist), [True])
        self.assertEqual(len(gate_a.score_hist), 2)
        self.assertEqual(len(gate_b.score_hist), 1)


if __name__ == "__main__":
    unittest.main()
