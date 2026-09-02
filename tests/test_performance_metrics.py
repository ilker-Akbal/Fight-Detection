from __future__ import annotations

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fight.pipeline_mp.camera_worker import CameraProcessRunner
from fight.pipeline_mp.messages import PersonInferenceResult, PoseInferenceResult
from fight.pipeline_mp.performance import (
    BoundedMetricCollector,
    build_performance_summary,
    compute_inference_timings,
    percentile,
)
from fight.pipeline_mp.person_worker import PersonInferenceClient
from fight.pipeline_mp.pose_worker import PoseInferenceClient


class _Payload:
    shape = (480, 640, 3)
    nbytes = 480 * 640 * 3


class TimingAndCollectorTests(unittest.TestCase):
    def test_timing_fields_are_non_negative(self):
        timings = compute_inference_timings(
            request_created=5.0,
            request_put_started=4.0,
            request_put_done=3.0,
            worker_received=2.0,
            inference_started=2.0,
            inference_ended=1.0,
            result_received=0.0,
        )
        self.assertTrue(all(value >= 0.0 for value in timings.values()))

    def test_timing_breakdown_formula(self):
        timings = compute_inference_timings(
            request_created=1.0,
            request_put_started=1.1,
            request_put_done=1.2,
            worker_received=1.5,
            inference_started=1.6,
            inference_ended=1.9,
            result_received=2.1,
        )
        self.assertAlmostEqual(timings["enqueue_ms"], 100.0)
        self.assertAlmostEqual(timings["queue_wait_ms"], 300.0)
        self.assertAlmostEqual(timings["inference_ms"], 300.0)
        self.assertAlmostEqual(timings["result_delivery_ms"], 200.0)
        self.assertAlmostEqual(timings["round_trip_ms"], 1100.0)

    def test_collector_is_bounded(self):
        collector = BoundedMetricCollector(enabled=True, max_samples=3)
        for value in range(6):
            collector.observe(value)
        self.assertEqual(collector.values(), [3.0, 4.0, 5.0])
        self.assertEqual(collector.sample_count, 3)

    def test_percentile_fixture_is_deterministic(self):
        values = [0.0, 10.0, 20.0, 30.0]
        self.assertEqual(percentile(values, 0.50), 15.0)
        self.assertAlmostEqual(percentile(values, 0.95), 28.5)

    def test_disabled_collector_keeps_no_samples(self):
        collector = BoundedMetricCollector(enabled=False, max_samples=3)
        for value in range(10):
            collector.observe(value)
        self.assertEqual(collector.values(), [])
        self.assertEqual(collector.observations, 0)


class ClientTimingTests(unittest.TestCase):
    def test_person_timing_and_payload_metadata(self):
        result_queue = queue.Queue()
        result_queue.put(
            PersonInferenceResult(
                camera_id="cam_A",
                generation=1,
                frame_idx=10,
                request_id=1,
                detections=[],
                worker_received_monotonic=1.5,
                inference_started_monotonic=1.6,
                inference_ended_monotonic=1.9,
            )
        )
        request_queue = queue.Queue()
        client = PersonInferenceClient(
            camera_id="cam_A",
            generation=1,
            request_queue=request_queue,
            result_queue=result_queue,
            report_queue=queue.Queue(),
            stop_event=threading.Event(),
            inference_timeout_sec=1.0,
            enqueue_timeout_sec=0.1,
            performance_metrics_enabled=True,
        )

        with patch("fight.pipeline_mp.person_worker.time.perf_counter", side_effect=[1.0, 1.1, 1.2, 2.1]):
            client.infer(_Payload(), frame_idx=10)

        summary = client.performance_summary()
        self.assertAlmostEqual(summary["_samples"]["queue_wait_ms"][0], 300.0)
        self.assertAlmostEqual(summary["_samples"]["inference_ms"][0], 300.0)
        request = request_queue.get_nowait()
        self.assertEqual(request.payload_width, 640)
        self.assertEqual(request.payload_height, 480)
        self.assertEqual(request.payload_channels, 3)
        self.assertEqual(request.payload_bytes, _Payload.nbytes)

    def test_pose_timing_is_collected(self):
        result_queue = queue.Queue()
        result_queue.put(
            PoseInferenceResult(
                camera_id="cam_A",
                generation=1,
                frame_idx=20,
                request_id=1,
                pose_result=SimpleNamespace(score=0.8, ok=True),
                worker_received_monotonic=1.5,
                inference_started_monotonic=1.6,
                inference_ended_monotonic=1.9,
            )
        )
        client = PoseInferenceClient(
            camera_id="cam_A",
            generation=1,
            request_queue=queue.Queue(),
            result_queue=result_queue,
            report_queue=queue.Queue(),
            stop_event=threading.Event(),
            inference_timeout_sec=1.0,
            enqueue_timeout_sec=0.1,
            performance_metrics_enabled=True,
        )

        with patch("fight.pipeline_mp.pose_worker.time.perf_counter", side_effect=[1.0, 1.1, 1.2, 2.1]):
            client.infer(_Payload(), frame_idx=20)

        summary = client.performance_summary()
        self.assertAlmostEqual(summary["_samples"]["queue_wait_ms"][0], 300.0)
        self.assertAlmostEqual(summary["_samples"]["round_trip_ms"][0], 1100.0)

    def test_dropped_result_increments_metric_without_changing_accepted_result(self):
        results = queue.Queue()
        results.put(
            PersonInferenceResult(
                camera_id="cam_A",
                generation=0,
                frame_idx=10,
                request_id=1,
                detections=[("stale", ())],
            )
        )
        results.put(
            PersonInferenceResult(
                camera_id="cam_A",
                generation=1,
                frame_idx=10,
                request_id=1,
                detections=[("accepted", ())],
            )
        )
        client = PersonInferenceClient(
            camera_id="cam_A",
            generation=1,
            request_queue=queue.Queue(),
            result_queue=results,
            report_queue=queue.Queue(),
            stop_event=threading.Event(),
            inference_timeout_sec=1.0,
            enqueue_timeout_sec=0.1,
        )
        detections = client.infer(_Payload(), frame_idx=10)
        self.assertEqual(detections, [("accepted", ())])
        summary = client.performance_summary()
        self.assertEqual(summary["result_drops"], 1)
        self.assertTrue(all(not values for values in summary["_samples"].values()))
        self.assertTrue(
            all(not values for values in summary["_steady_state_samples"].values())
        )


class CameraAndRunSummaryTests(unittest.TestCase):
    def test_camera_summary_writes_counters_and_duration(self):
        runner = CameraProcessRunner.__new__(CameraProcessRunner)
        runner.started_monotonic = 10.0
        runner.source_is_file = True
        runner.source_frame_count = 300
        runner.capture_fps = 30.0
        runner.camera_id = "cam_A"
        runner.source = "same.mp4"
        runner.generation = 2
        runner.report_queue = queue.Queue()
        runner.counters = {
            "frames_read": 300,
            "motion_pass_frames": 200,
            "frames_with_enough_persons": 100,
            "pair_candidates": 50,
            "pair_activated": 40,
            "pose_gate_positive_frames": 20,
            "events_opened": 3,
            "events_closed": 3,
            "stage3_jobs_submitted": 2,
        }
        client_summary = {
            "requests": 100,
            "results": 99,
            "timeouts": 1,
            "queue_full": 0,
            "result_drops": 0,
            "metrics_enabled": True,
            "queue_depth_high_water_best_effort": 2,
            "timings": {},
            "_samples": {},
        }
        runner.person_inference = SimpleNamespace(performance_summary=lambda: client_summary)
        runner.pose_inference = SimpleNamespace(performance_summary=lambda: client_summary)

        with patch("fight.pipeline_mp.camera_worker.time.perf_counter", return_value=20.0):
            runner.report_performance_summary()

        row = runner.report_queue.get_nowait().row
        self.assertEqual(row["frames_read"], 300)
        self.assertEqual(row["events_opened"], 3)
        self.assertEqual(row["person_requests"], 100)
        self.assertEqual(row["pose_results"], 99)
        self.assertEqual(row["source_duration_sec"], 10.0)
        self.assertEqual(row["camera_processing_fps"], 30.0)

    def test_run_summary_aggregates_cameras_and_source_duration(self):
        client_a = {
            "requests": 10,
            "results": 10,
            "timeouts": 0,
            "queue_full": 0,
            "result_drops": 0,
            "_samples": {
                "enqueue_ms": [1.0],
                "queue_wait_ms": [4.0],
                "round_trip_ms": [10.0],
                "result_delivery_ms": [2.0],
            },
        }
        client_b = {
            "requests": 20,
            "results": 19,
            "timeouts": 1,
            "queue_full": 0,
            "result_drops": 1,
            "_samples": {
                "enqueue_ms": [3.0],
                "queue_wait_ms": [8.0],
                "round_trip_ms": [30.0],
                "result_delivery_ms": [4.0],
            },
        }
        rows = [
            {
                "stage": "camera_summary",
                "detail": "completed",
                "camera_id": "cam_A",
                "source": "same.mp4",
                "frames_read": 100,
            "source_duration_sec": 10.0,
                "camera_elapsed_sec": 10.0,
                "camera_source_to_processing_ratio": 1.0,
                "person": client_a,
                "pose": client_a,
            },
            {
                "stage": "camera_summary",
                "detail": "completed",
                "camera_id": "cam_B",
                "source": "same.mp4",
                "frames_read": 200,
                "source_duration_sec": 10.0,
                "camera_elapsed_sec": 10.0,
                "camera_source_to_processing_ratio": 1.0,
                "person": client_b,
                "pose": client_b,
            },
            {
                "stage": "person_inference",
                "detail": "summary",
                "queue_wait_ms": {"samples": 2, "mean": 5.0, "p50": 5.0, "p95": 6.0, "max": 6.0},
                "inference_ms": {"samples": 2, "mean": 7.0, "p50": 7.0, "p95": 8.0, "max": 8.0},
                "payload_bytes_total": 1048576,
                "steady_state": {
                    "requests_excluded": 2,
                    "requests_included": 28,
                    "queue_wait_ms": {"samples": 2, "mean": 4.0, "p50": 4.0, "p95": 5.0, "max": 5.0},
                    "inference_ms": {"samples": 2, "mean": 6.0, "p50": 6.0, "p95": 7.0, "max": 7.0},
                },
                "batch": {"enabled": True, "batches_processed": 9},
            },
            {
                "stage": "stage3",
                "detail": "summary",
                "jobs_received": 2,
                "jobs_completed": 2,
                "errors": 0,
            },
        ]
        summary = build_performance_summary(
            {"run_name": "fixture", "cameras": [{}, {}], "runtime": {"performance_metrics_enabled": True}},
            rows,
            wall_processing_sec=10.0,
        )

        self.assertEqual(summary["total_frames_read"], 300)
        self.assertEqual(summary["aggregate_processing_fps"], 30.0)
        self.assertEqual(summary["unique_source_duration_sec"], 10.0)
        self.assertEqual(summary["aggregate_camera_source_duration_sec"], 20.0)
        self.assertEqual(summary["real_time_factor"], 2.0)
        self.assertEqual(summary["per_camera_realtime_factor"], 1.0)
        self.assertEqual(summary["mean_camera_realtime_factor"], 1.0)
        self.assertEqual(summary["min_camera_realtime_factor"], 1.0)
        self.assertEqual(summary["person"]["requests"], 30)
        self.assertEqual(summary["person"]["queue_wait_ms"]["p50"], 6.0)
        self.assertEqual(summary["person"]["steady_state"]["requests_included"], 28)
        self.assertTrue(summary["person"]["batch"]["enabled"])
        self.assertEqual(summary["person"]["round_trip_ms"]["p50"], 20.0)
        self.assertEqual(summary["person"]["payload_mb_total"], 1.0)
        self.assertEqual(summary["stage3"]["jobs_completed"], 2)


if __name__ == "__main__":
    unittest.main()
