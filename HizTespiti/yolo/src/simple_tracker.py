from __future__ import annotations

from dataclasses import dataclass, field

from .vehicle_detector import VehicleDetection
from .utils import bbox_center_xyxy


def iou_xyxy(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih

    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)

    union = area_a + area_b - inter

    if union <= 0:
        return 0.0

    return inter / union


@dataclass
class Track:
    track_id: int
    box: tuple[float, float, float, float]
    cls_name: str
    conf: float
    age: int = 1
    hits: int = 1
    missed: int = 0
    history: list[tuple[int, float, float]] = field(default_factory=list)

    def update(self, det: VehicleDetection, frame_idx: int):
        self.box = det.box
        self.cls_name = det.cls_name
        self.conf = det.conf
        self.age += 1
        self.hits += 1
        self.missed = 0

        cx, cy = bbox_center_xyxy(det.box)
        self.history.append((frame_idx, cx, cy))

        if len(self.history) > 120:
            self.history = self.history[-120:]

    def mark_missed(self):
        self.age += 1
        self.missed += 1


class SimpleIoUTracker:
    def __init__(self, iou_threshold: float = 0.25, max_age: int = 20, min_hits: int = 2):
        self.iou_threshold = iou_threshold
        self.max_age = max_age
        self.min_hits = min_hits
        self.tracks: list[Track] = []
        self._next_id = 1

    def update(self, detections: list[VehicleDetection], frame_idx: int) -> list[Track]:
        matched_tracks = set()
        matched_dets = set()

        pairs = []

        for ti, tr in enumerate(self.tracks):
            for di, det in enumerate(detections):
                score = iou_xyxy(tr.box, det.box)
                if score >= self.iou_threshold:
                    pairs.append((score, ti, di))

        pairs.sort(reverse=True, key=lambda x: x[0])

        for score, ti, di in pairs:
            if ti in matched_tracks or di in matched_dets:
                continue

            self.tracks[ti].update(detections[di], frame_idx)
            matched_tracks.add(ti)
            matched_dets.add(di)

        for ti, tr in enumerate(self.tracks):
            if ti not in matched_tracks:
                tr.mark_missed()

        for di, det in enumerate(detections):
            if di in matched_dets:
                continue

            cx, cy = bbox_center_xyxy(det.box)
            tr = Track(
                track_id=self._next_id,
                box=det.box,
                cls_name=det.cls_name,
                conf=det.conf,
                history=[(frame_idx, cx, cy)],
            )
            self._next_id += 1
            self.tracks.append(tr)

        self.tracks = [t for t in self.tracks if t.missed <= self.max_age]

        return self.active_tracks()

    def active_tracks(self) -> list[Track]:
        return [
            t for t in self.tracks
            if t.hits >= self.min_hits and t.missed == 0
        ]