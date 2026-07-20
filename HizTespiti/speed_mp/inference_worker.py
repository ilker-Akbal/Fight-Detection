from __future__ import annotations

from HizTespiti.yolo.src.vehicle_detector import VehicleDetector
from HizTespiti.yolo.src.yolo_config import YoloConfig
from shared_inference.runtime import inference_worker_main


def vehicle_worker_main(config: dict, job_queue, result_queue, stop_event, ready_queue):
    runtime = config.get("runtime", {})

    def build():
        yolo = config.get("yolo", {})
        cfg = YoloConfig(
            weights=str(config.get("yolo_weights", "yolo11s.pt")),
            device=str(yolo.get("device", 0)), imgsz=int(yolo.get("imgsz", 640)),
            conf=float(yolo.get("conf", .30)), iou=float(yolo.get("iou", .50)),
            stride=max(1, int(yolo.get("stride", 3))),
            vehicle_classes=list(yolo.get("vehicle_classes", ["car", "motorcycle", "bus", "truck"])),
        )
        return VehicleDetector(cfg).detect

    inference_worker_main(
        stage="vehicle_detection", job_queue=job_queue, result_queue=result_queue,
        stop_event=stop_event, build_handler=build,
        max_batch_size=runtime.get("inference_max_batch_size", 4),
        max_batch_wait_ms=runtime.get("inference_max_batch_wait_ms", 5),
        ready_queue=ready_queue,
    )
