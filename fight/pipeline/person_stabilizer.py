from __future__ import annotations

from fight.pipeline.utils import box_iou, smooth_box


class TemporalPersonStabilizer:
    def __init__(
        self,
        max_age: int = 8,
        min_hits: int = 1,
        iou_match_thr: float = 0.25,
        conf_alpha: float = 0.65,
        max_tracks: int = 12,
    ):
        self.max_age = int(max_age)
        self.min_hits = int(min_hits)
        self.iou_match_thr = float(iou_match_thr)
        self.conf_alpha = float(conf_alpha)
        self.max_tracks = int(max_tracks)
        self.tracks = []
        self.next_id = 1

    def reset(self):
        self.tracks = []
        self.next_id = 1

    def _make_track(self, conf, box):
        return {
            "id": self.next_id,
            "conf": float(conf),
            "box": tuple(map(int, box)),
            "age": 0,
            "hits": 1,
            "matched": True,
        }

    def update(self, persons):
        for t in self.tracks:
            t["matched"] = False

        used_tracks = set()

        for conf, box in persons:
            best_idx = -1
            best_iou = 0.0
            for idx, tr in enumerate(self.tracks):
                if idx in used_tracks:
                    continue
                i = box_iou(box, tr["box"])
                if i > best_iou:
                    best_iou = i
                    best_idx = idx

            if best_idx >= 0 and best_iou >= self.iou_match_thr:
                tr = self.tracks[best_idx]
                tr["conf"] = self.conf_alpha * float(conf) + (1.0 - self.conf_alpha) * float(tr["conf"])
                tr["box"] = smooth_box(tr["box"], box, alpha=0.55)
                tr["age"] = 0
                tr["hits"] += 1
                tr["matched"] = True
                used_tracks.add(best_idx)
            else:
                tr = self._make_track(conf, box)
                self.next_id += 1
                self.tracks.append(tr)

        alive = []
        for tr in self.tracks:
            if not tr["matched"]:
                tr["age"] += 1
            if tr["age"] <= self.max_age:
                alive.append(tr)
        self.tracks = alive

        self.tracks.sort(key=lambda x: (x["conf"], x["hits"]), reverse=True)
        self.tracks = self.tracks[: self.max_tracks]
        return self.get_stable_persons()

    def predict_only(self):
        alive = []
        for tr in self.tracks:
            tr["age"] += 1
            if tr["age"] <= self.max_age:
                alive.append(tr)
        self.tracks = alive
        return self.get_stable_persons()

    def get_stable_persons(self):
        out = []
        for tr in self.tracks:
            if tr["hits"] >= self.min_hits:
                out.append((float(tr["conf"]), tuple(map(int, tr["box"]))))
        out.sort(key=lambda x: x[0], reverse=True)
        return out