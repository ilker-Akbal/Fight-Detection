from __future__ import annotations

from fight.pipeline.adapters import YoloAdapter
from fight.pose.src.pose_adapter import PoseAdapter
from shared_inference.runtime import inference_worker_main


def person_handler(config: dict):
    models = config["models"]
    adapter = YoloAdapter(models["yolo_config"], models["yolo_weights"])
    return adapter.detect_persons


def pose_handler(config: dict):
    adapter = PoseAdapter(config["models"]["pose_config"])

    def infer(image):
        raw = adapter.evaluate(image)
        return {
            "score": float(raw.score),
            "ok": bool(raw.ok),
            "hist_positive": int(getattr(raw, "hist_positive", 0)),
        }
    return infer


def person_worker_main(config, job_queue, result_queue, stop_event, ready_queue):
    runtime = config.get("runtime", {})
    inference_worker_main(
        stage="person_detection", job_queue=job_queue, result_queue=result_queue,
        stop_event=stop_event, build_handler=lambda: person_handler(config),
        max_batch_size=runtime.get("inference_max_batch_size", 4),
        max_batch_wait_ms=runtime.get("inference_max_batch_wait_ms", 5),
        ready_queue=ready_queue,
    )


def pose_worker_main(config, job_queue, result_queue, stop_event, ready_queue):
    runtime = config.get("runtime", {})
    inference_worker_main(
        stage="pose", job_queue=job_queue, result_queue=result_queue,
        stop_event=stop_event, build_handler=lambda: pose_handler(config),
        max_batch_size=runtime.get("inference_max_batch_size", 4),
        max_batch_wait_ms=runtime.get("inference_max_batch_wait_ms", 5),
        ready_queue=ready_queue,
    )
