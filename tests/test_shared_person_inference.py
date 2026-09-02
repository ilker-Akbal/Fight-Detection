from __future__ import annotations

import queue
import multiprocessing as mp
import threading
import time
import unittest

from fight.pipeline_mp.messages import PersonInferenceRequest, PersonInferenceResult
from fight.pipeline_mp.person_worker import (
    PersonInferenceClient,
    PersonInferenceTimeout,
    PersonResultValidator,
    person_result_router_main,
    route_person_result,
    run_person_inference_loop,
    should_request_person_inference,
)


def _result(camera_id: str, generation: int, frame_idx: int, request_id: int, marker: float = 0.9):
    return PersonInferenceResult(
        camera_id=camera_id,
        generation=generation,
        frame_idx=frame_idx,
        request_id=request_id,
        detections=[(marker, (1, 2, 30, 40))],
    )


class SharedPersonRoutingTests(unittest.TestCase):
    def test_two_cameras_route_to_camera_specific_channels(self):
        channels = {"cam_A": queue.Queue(), "cam_B": queue.Queue()}
        result_a = _result("cam_A", 1, 10, 1, 0.81)
        result_b = _result("cam_B", 1, 10, 1, 0.92)

        self.assertEqual(route_person_result(result_a, channels, 0.01), (True, "routed"))
        self.assertEqual(route_person_result(result_b, channels, 0.01), (True, "routed"))

        self.assertIs(channels["cam_A"].get_nowait(), result_a)
        self.assertIs(channels["cam_B"].get_nowait(), result_b)

    def test_reordered_cross_camera_results_stay_isolated(self):
        channels = {"cam_A": queue.Queue(), "cam_B": queue.Queue()}
        result_a = _result("cam_A", 3, 10, 7)
        result_b = _result("cam_B", 4, 10, 2)

        route_person_result(result_b, channels, 0.01)
        route_person_result(result_a, channels, 0.01)

        self.assertEqual(channels["cam_A"].get_nowait().camera_id, "cam_A")
        self.assertEqual(channels["cam_B"].get_nowait().camera_id, "cam_B")

    def test_router_channels_are_spawn_process_safe(self):
        ctx = mp.get_context("spawn")
        shared_results = ctx.JoinableQueue(maxsize=4)
        channels = {"cam_A": ctx.Queue(maxsize=1), "cam_B": ctx.Queue(maxsize=1)}
        reports = ctx.Queue(maxsize=8)
        stop_event = ctx.Event()
        process = ctx.Process(
            target=person_result_router_main,
            args=(
                {"runtime": {"person_route_timeout_sec": 0.1}},
                shared_results,
                channels,
                reports,
                stop_event,
            ),
        )

        try:
            process.start()
            shared_results.put(_result("cam_B", 1, 10, 1), timeout=1.0)
            shared_results.put(_result("cam_A", 1, 10, 1), timeout=1.0)
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


class PersonResultValidationTests(unittest.TestCase):
    def test_old_generation_is_rejected(self):
        validator = PersonResultValidator("cam_A", generation=2)
        accepted, reason = validator.validate(
            _result("cam_A", 1, 10, 1),
            expected_frame_idx=10,
            expected_request_id=1,
        )
        self.assertFalse(accepted)
        self.assertEqual(reason, "stale_generation")

    def test_duplicate_is_not_applied_twice(self):
        validator = PersonResultValidator("cam_A", generation=2)
        result = _result("cam_A", 2, 10, 1)

        self.assertEqual(
            validator.validate(result, expected_frame_idx=10, expected_request_id=1),
            (True, "accepted"),
        )
        validator.mark_completed(1)
        self.assertEqual(
            validator.validate(result, expected_frame_idx=10, expected_request_id=1),
            (False, "duplicate_result"),
        )

    def test_wrong_camera_request_and_frame_are_rejected(self):
        validator = PersonResultValidator("cam_A", generation=1)
        cases = [
            (_result("cam_B", 1, 10, 1), "wrong_camera_id"),
            (_result("cam_A", 1, 10, 9), "unexpected_request_id"),
            (_result("cam_A", 1, 11, 1), "unexpected_frame_idx"),
        ]
        for result, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                self.assertEqual(
                    validator.validate(result, expected_frame_idx=10, expected_request_id=1),
                    (False, expected_reason),
                )


class PersonInferenceClientTests(unittest.TestCase):
    def test_stale_result_is_dropped_before_matching_result(self):
        requests = queue.Queue(maxsize=1)
        results = queue.Queue(maxsize=2)
        reports = queue.Queue()
        results.put(_result("cam_A", 1, 10, 1))
        results.put(_result("cam_A", 2, 10, 1, 0.77))

        client = PersonInferenceClient(
            camera_id="cam_A",
            generation=2,
            request_queue=requests,
            result_queue=results,
            report_queue=reports,
            stop_event=threading.Event(),
            inference_timeout_sec=0.2,
            enqueue_timeout_sec=0.01,
        )

        detections = client.infer(frame="frame", frame_idx=10)
        self.assertEqual(detections[0][0], 0.77)
        self.assertEqual(requests.get_nowait().request_id, 1)
        drop_report = reports.get_nowait()
        self.assertEqual(drop_report.row["drop_reason"], "stale_generation")

    def test_timeout_returns_without_deadlock(self):
        client = PersonInferenceClient(
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
        with self.assertRaises(PersonInferenceTimeout):
            client.infer(frame="frame", frame_idx=10)
        self.assertLess(time.monotonic() - started, 0.5)


class PersonWorkerTests(unittest.TestCase):
    def test_model_is_initialized_once_for_multiple_cameras(self):
        class FakeAdapter:
            initialization_count = 0

            def __init__(self, cfg_path, weights_path):
                FakeAdapter.initialization_count += 1

            def detect_persons(self, frame):
                return [(0.88, (0, 0, 20, 30))]

        requests = queue.Queue()
        results = queue.Queue()
        reports = queue.Queue()
        for camera_id in ("cam_A", "cam_B"):
            requests.put(
                PersonInferenceRequest(
                    camera_id=camera_id,
                    generation=1,
                    frame_idx=10,
                    request_id=1,
                    frame=camera_id,
                )
            )
        requests.put(None)

        config = {
            "models": {"yolo_config": "same.yaml", "yolo_weights": "same.pt"},
            "runtime": {"person_result_enqueue_timeout_sec": 0.01},
        }
        run_person_inference_loop(
            config,
            requests,
            results,
            reports,
            threading.Event(),
            adapter_factory=FakeAdapter,
        )

        self.assertEqual(FakeAdapter.initialization_count, 1)
        self.assertEqual({results.get_nowait().camera_id, results.get_nowait().camera_id}, {"cam_A", "cam_B"})
        model_reports = []
        while not reports.empty():
            row = reports.get_nowait().row
            if row.get("detail") == "model_initialized":
                model_reports.append(row)
        self.assertEqual(len(model_reports), 1)
        self.assertEqual(model_reports[0]["model_instance_count"], 1)

    def test_shutdown_sentinel_stops_worker_loop(self):
        class FakeAdapter:
            def __init__(self, cfg_path, weights_path):
                pass

            def detect_persons(self, frame):
                raise AssertionError("sentinel must not run inference")

        requests = queue.Queue()
        requests.put(None)
        run_person_inference_loop(
            {"models": {"yolo_config": "x", "yolo_weights": "y"}},
            requests,
            queue.Queue(),
            queue.Queue(),
            threading.Event(),
            adapter_factory=FakeAdapter,
        )
        self.assertTrue(requests.empty())


class PersonInferenceGatingTests(unittest.TestCase):
    def test_motion_inactive_never_requests_person_inference(self):
        for frame_idx in range(10):
            self.assertFalse(should_request_person_inference(False, frame_idx, 2))

    def test_yolo_stride_is_preserved(self):
        requested = [
            frame_idx
            for frame_idx in range(8)
            if should_request_person_inference(True, frame_idx, 3)
        ]
        self.assertEqual(requested, [0, 3, 6])


if __name__ == "__main__":
    unittest.main()
