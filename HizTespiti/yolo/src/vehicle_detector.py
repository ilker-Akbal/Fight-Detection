from __future__ import annotations

from dataclasses import dataclass

from ultralytics import YOLO

from .yolo_config import YoloConfig


@dataclass
class VehicleDetection:
    box: tuple[float, float, float, float]
    conf: float
    cls_id: int
    cls_name: str


class VehicleDetector:
    def __init__(self, cfg: YoloConfig):
        self.cfg = cfg
        self.model = YOLO(cfg.weights)
        self.names = self.model.names

        self.vehicle_class_ids = set()

        for cls_id, name in self.names.items():
            if str(name).lower() in [x.lower() for x in cfg.vehicle_classes]:
                self.vehicle_class_ids.add(int(cls_id))

        if not self.vehicle_class_ids:
            raise RuntimeError(
                f"YOLO modelinde vehicle class bulunamadı. "
                f"Model classes={self.names}, istenen={cfg.vehicle_classes}"
            )

        print(f"[YOLO] weights={cfg.weights}")
        print(f"[YOLO] vehicle_class_ids={self.vehicle_class_ids}")

    def detect(self, frame_bgr) -> list[VehicleDetection]:
        results = self.model.predict(
            source=frame_bgr,
            imgsz=self.cfg.imgsz,
            conf=self.cfg.conf,
            iou=self.cfg.iou,
            device=self.cfg.device,
            verbose=False,
        )

        detections: list[VehicleDetection] = []

        if not results:
            return detections

        r = results[0]
        if r.boxes is None:
            return detections

        boxes = r.boxes

        for b in boxes:
            cls_id = int(b.cls.item())
            conf = float(b.conf.item())

            if cls_id not in self.vehicle_class_ids:
                continue

            x1, y1, x2, y2 = b.xyxy[0].detach().cpu().tolist()
            cls_name = str(self.names.get(cls_id, cls_id))

            detections.append(
                VehicleDetection(
                    box=(float(x1), float(y1), float(x2), float(y2)),
                    conf=conf,
                    cls_id=cls_id,
                    cls_name=cls_name,
                )
            )

        return detections