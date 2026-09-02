from __future__ import annotations

import queue
import importlib
import sys
import threading
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from fight.pipeline.adapters import YoloAdapter
from fight.pipeline_mp.batching import collect_request_batch
from fight.pipeline_mp.messages import PersonInferenceRequest, PoseInferenceRequest
from fight.pipeline_mp.person_worker import run_person_inference_loop
from fight.pipeline_mp.pose_worker import run_pose_inference_loop

_fake_ultralytics = types.ModuleType("ultralytics")
_fake_ultralytics.YOLO = object
with patch.dict(sys.modules, {"ultralytics": _fake_ultralytics}):
    PoseAdapter = importlib.import_module("fight.pose.src.pose_adapter").PoseAdapter
sys.modules.pop("fight.pose.src.pose_adapter", None)


def _person_request(index: int, frame=None):
    return PersonInferenceRequest(
        camera_id=f"cam_{index}",
        generation=index,
        frame_idx=100 + index,
        request_id=index,
        frame=index if frame is None else frame,
    )


def _pose_request(index: int, roi=None):
    return PoseInferenceRequest(
        camera_id=f"cam_{index}",
        generation=index,
        frame_idx=200 + index,
        request_id=index,
        roi=index if roi is None else roi,
    )


def _person_config(**runtime):
    return {
        "models": {"yolo_config": "fake.yaml", "yolo_weights": "fake.pt"},
        "runtime": {"person_result_enqueue_timeout_sec": 0.1, **runtime},
    }


def _pose_config(**runtime):
    return {
        "models": {"pose_config": "fake.yaml"},
        "runtime": {"pose_result_enqueue_timeout_sec": 0.1, **runtime},
    }


def _summary_rows(reports, stage: str):
    rows = []
    while not reports.empty():
        row = reports.get_nowait().row
        if row.get("stage") == stage and row.get("detail") == "summary":
            rows.append(row)
    return rows


class BatchCollectionTests(unittest.TestCase):
    def test_partial_batch_stops_at_deadline_without_qsize_or_empty(self):
        requests = queue.Queue()
        requests.put("second")
        started = time.perf_counter()
        batch = collect_request_batch(
            requests,
            "first",
            enabled=True,
            batch_size=4,
            max_wait_ms=5.0,
            first_received_monotonic=started,
        )
        self.assertEqual(batch.requests, ["first", "second"])
        self.assertFalse(batch.sentinel_received)
        self.assertGreaterEqual(batch.collect_wait_ms, 1.0)
        self.assertLess(batch.collect_wait_ms, 100.0)

    def test_low_traffic_produces_size_one_batch(self):
        batch = collect_request_batch(
            queue.Queue(),
            "only",
            enabled=True,
            batch_size=4,
            max_wait_ms=2.0,
        )
        self.assertEqual(batch.requests, ["only"])
        self.assertFalse(batch.sentinel_received)

    def test_sentinel_is_reported_but_not_added_to_batch(self):
        requests = queue.Queue()
        requests.put("second")
        requests.put(None)
        batch = collect_request_batch(
            requests,
            "first",
            enabled=True,
            batch_size=4,
            max_wait_ms=20.0,
        )
        self.assertEqual(batch.requests, ["first", "second"])
        self.assertTrue(batch.sentinel_received)


class AdapterBatchApiTests(unittest.TestCase):
    def test_person_adapter_batches_in_one_predict_call_and_preserves_order(self):
        class Model:
            def __init__(self):
                self.calls = []

            def predict(self, **kwargs):
                self.calls.append(kwargs)
                return [SimpleNamespace(boxes=None) for _ in kwargs["source"]]

        adapter = YoloAdapter.__new__(YoloAdapter)
        adapter.model = Model()
        adapter.imgsz = 416
        adapter.conf = 0.25
        adapter.iou = 0.45
        adapter.device = 0
        adapter.verbose = False
        first = np.zeros((8, 12, 3), dtype=np.uint8)
        second = np.ones((8, 12, 3), dtype=np.uint8)

        output = adapter.detect_persons_batch([first, second])

        self.assertEqual(output, [[], []])
        self.assertEqual(len(adapter.model.calls), 1)
        self.assertEqual(len(adapter.model.calls[0]["source"]), 2)
        self.assertEqual(adapter.model.calls[0]["imgsz"], 416)
        self.assertEqual(adapter.model.calls[0]["conf"], 0.25)
        self.assertEqual(adapter.model.calls[0]["iou"], 0.45)

    def test_person_adapter_separates_incompatible_shapes_without_losing_order(self):
        class Model:
            def __init__(self):
                self.calls = []

            def predict(self, **kwargs):
                self.calls.append(kwargs)
                source = kwargs["source"]
                count = len(source) if isinstance(source, list) else 1
                return [SimpleNamespace(boxes=None) for _ in range(count)]

        adapter = YoloAdapter.__new__(YoloAdapter)
        adapter.model = Model()
        adapter.imgsz = 416
        adapter.conf = 0.25
        adapter.iou = 0.45
        adapter.device = 0
        adapter.verbose = False
        frames = [
            np.zeros((8, 12, 3), dtype=np.uint8),
            np.ones((10, 14, 3), dtype=np.uint8),
            np.full((8, 12, 3), 2, dtype=np.uint8),
        ]

        output = adapter.detect_persons_batch(frames)

        self.assertEqual(output, [[], [], []])
        self.assertEqual(len(adapter.model.calls), 2)

    def test_pose_adapter_normalizes_rois_then_batches_in_one_predict_call(self):
        class Model:
            def __init__(self):
                self.calls = []

            def predict(self, **kwargs):
                self.calls.append(kwargs)
                return [SimpleNamespace(boxes=None) for _ in kwargs["source"]]

        adapter = PoseAdapter.__new__(PoseAdapter)
        adapter.model = Model()
        adapter.imgsz = 416
        adapter.conf = 0.18
        adapter.device = 0
        adapter.verbose = False
        adapter._prepare_roi = lambda roi: np.full((16, 16, 3), int(roi[0, 0, 0]), dtype=np.uint8)
        first = np.zeros((8, 12, 3), dtype=np.uint8)
        second = np.ones((10, 14, 3), dtype=np.uint8)

        output = adapter.evaluate_batch([first, second])

        self.assertEqual(len(output), 2)
        self.assertTrue(all(result.num_persons == 0 for result in output))
        self.assertEqual(len(adapter.model.calls), 1)
        self.assertEqual([image.shape for image in adapter.model.calls[0]["source"]], [(16, 16, 3), (16, 16, 3)])
        self.assertEqual(adapter.model.calls[0]["conf"], 0.18)


class PersonMicroBatchTests(unittest.TestCase):
    def test_batch_size_one_uses_original_single_semantics(self):
        class Adapter:
            def __init__(self, *_):
                self.single_calls = 0

            def detect_persons(self, frame):
                self.single_calls += 1
                return [(float(frame), (0, 0, 1, 1))]

            def detect_persons_batch(self, frames):
                raise AssertionError("batch API must not be used for size=1")

        requests = queue.Queue()
        results = queue.Queue()
        reports = queue.Queue()
        requests.put(_person_request(1))
        requests.put(None)
        run_person_inference_loop(
            _person_config(
                person_batch_enabled=True,
                person_batch_size=1,
                person_batch_max_wait_ms=3.0,
            ),
            requests,
            results,
            reports,
            threading.Event(),
            adapter_factory=Adapter,
        )
        self.assertEqual(results.get_nowait().detections[0][0], 1.0)
        summary = _summary_rows(reports, "person_inference")[0]
        self.assertEqual(summary["batch"]["batch_size"]["samples"], 0)
        self.assertEqual(summary["batch"]["batch_size"]["observations"], 0)

    def test_batch_of_four_preserves_identity_order_and_one_model_call(self):
        class Adapter:
            initialization_count = 0
            batch_calls = 0

            def __init__(self, *_):
                Adapter.initialization_count += 1

            def detect_persons(self, frame):
                raise AssertionError("single fallback was not expected")

            def detect_persons_batch(self, frames):
                Adapter.batch_calls += 1
                return [[(float(frame), (0, 0, 1, 1))] for frame in frames]

        requests = queue.Queue()
        results = queue.Queue()
        reports = queue.Queue()
        for index in range(1, 5):
            requests.put(_person_request(index))
        requests.put(None)

        run_person_inference_loop(
            _person_config(
                person_batch_enabled=True,
                person_batch_size=4,
                person_batch_max_wait_ms=3.0,
                performance_metrics_enabled=True,
                performance_metrics_warmup_requests=2,
            ),
            requests,
            results,
            reports,
            threading.Event(),
            adapter_factory=Adapter,
        )

        output = [results.get_nowait() for _ in range(4)]
        self.assertEqual(Adapter.initialization_count, 1)
        self.assertEqual(Adapter.batch_calls, 1)
        self.assertEqual([item.camera_id for item in output], [f"cam_{i}" for i in range(1, 5)])
        self.assertEqual([item.generation for item in output], [1, 2, 3, 4])
        self.assertEqual([item.request_id for item in output], [1, 2, 3, 4])
        self.assertEqual([item.detections[0][0] for item in output], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(requests.unfinished_tasks, 0)
        summary = _summary_rows(reports, "person_inference")[0]
        self.assertEqual(summary["batches_processed"], 1)
        self.assertEqual(summary["batch_size_mean"], 4.0)
        self.assertEqual(summary["all_requests"]["inference_ms"]["samples"], 4)
        self.assertEqual(summary["steady_state"]["requests_excluded"], 2)
        self.assertEqual(summary["steady_state"]["inference_ms"]["samples"], 2)
        self.assertEqual(summary["batch"]["steady_state"]["batch_size"]["samples"], 0)

    def test_one_invalid_member_isolated_by_single_request_fallback(self):
        class Adapter:
            def __init__(self, *_):
                pass

            def detect_persons_batch(self, frames):
                raise ValueError("invalid batch member")

            def detect_persons(self, frame):
                if frame == "bad":
                    raise ValueError("bad frame")
                return [(float(frame), (0, 0, 1, 1))]

        requests = queue.Queue()
        results = queue.Queue()
        for index, frame in enumerate((1, "bad", 3, 4), start=1):
            requests.put(_person_request(index, frame=frame))
        requests.put(None)
        run_person_inference_loop(
            _person_config(
                person_batch_enabled=True,
                person_batch_size=4,
                person_batch_max_wait_ms=3.0,
            ),
            requests,
            results,
            queue.Queue(),
            threading.Event(),
            adapter_factory=Adapter,
        )
        output = [results.get_nowait() for _ in range(4)]
        self.assertIsNone(output[0].error)
        self.assertIn("bad frame", output[1].error)
        self.assertIsNone(output[2].error)
        self.assertIsNone(output[3].error)

    def test_two_requests_are_inferred_after_partial_batch_deadline(self):
        class Adapter:
            batch_calls = []

            def __init__(self, *_):
                pass

            def detect_persons(self, frame):
                raise AssertionError("single fallback was not expected")

            def detect_persons_batch(self, frames):
                Adapter.batch_calls.append(list(frames))
                return [[(float(frame), (0, 0, 1, 1))] for frame in frames]

        requests = queue.Queue()
        results = queue.Queue()
        requests.put(_person_request(1))
        requests.put(_person_request(2))
        sentinel_timer = threading.Timer(0.02, lambda: requests.put(None))
        sentinel_timer.start()
        run_person_inference_loop(
            _person_config(
                person_batch_enabled=True,
                person_batch_size=4,
                person_batch_max_wait_ms=2.0,
            ),
            requests,
            results,
            queue.Queue(),
            threading.Event(),
            adapter_factory=Adapter,
        )
        sentinel_timer.join(timeout=1.0)
        self.assertEqual(Adapter.batch_calls, [[1, 2]])
        self.assertEqual([results.get_nowait().camera_id for _ in range(2)], ["cam_1", "cam_2"])
        self.assertEqual(requests.unfinished_tasks, 0)


class PoseMicroBatchTests(unittest.TestCase):
    def test_batch_size_one_uses_original_single_semantics(self):
        class Adapter:
            def __init__(self, *_):
                pass

            def evaluate(self, roi):
                return SimpleNamespace(score=float(roi), ok=True)

            def evaluate_batch(self, rois):
                raise AssertionError("batch API must not be used for size=1")

        requests = queue.Queue()
        results = queue.Queue()
        requests.put(_pose_request(1))
        requests.put(None)
        run_pose_inference_loop(
            _pose_config(
                pose_batch_enabled=True,
                pose_batch_size=1,
                pose_batch_max_wait_ms=3.0,
            ),
            requests,
            results,
            queue.Queue(),
            threading.Event(),
            adapter_factory=Adapter,
        )
        self.assertEqual(results.get_nowait().pose_result.score, 1.0)

    def test_batch_of_four_preserves_identity_order_and_one_model_call(self):
        class Adapter:
            initialization_count = 0
            batch_calls = 0
            weights = "fake.pt"

            def __init__(self, *_):
                Adapter.initialization_count += 1

            def evaluate(self, roi):
                raise AssertionError("single fallback was not expected")

            def evaluate_batch(self, rois):
                Adapter.batch_calls += 1
                return [SimpleNamespace(score=float(roi), ok=True) for roi in rois]

        requests = queue.Queue()
        results = queue.Queue()
        reports = queue.Queue()
        for index in range(1, 5):
            requests.put(_pose_request(index))
        requests.put(None)
        run_pose_inference_loop(
            _pose_config(
                pose_batch_enabled=True,
                pose_batch_size=4,
                pose_batch_max_wait_ms=3.0,
                performance_metrics_enabled=True,
                performance_metrics_warmup_requests=0,
            ),
            requests,
            results,
            reports,
            threading.Event(),
            adapter_factory=Adapter,
        )

        output = [results.get_nowait() for _ in range(4)]
        self.assertEqual(Adapter.initialization_count, 1)
        self.assertEqual(Adapter.batch_calls, 1)
        self.assertEqual([item.camera_id for item in output], [f"cam_{i}" for i in range(1, 5)])
        self.assertEqual([item.pose_result.score for item in output], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(requests.unfinished_tasks, 0)
        summary = _summary_rows(reports, "pose_inference")[0]
        self.assertEqual(summary["batches_processed"], 1)
        self.assertEqual(summary["batch_size_max"], 4.0)


if __name__ == "__main__":
    unittest.main()
