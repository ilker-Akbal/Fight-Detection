from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import queue
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import cv2
import yaml

from fight.pipeline.adapters import MotionAdapter, Stage3Adapter, YoloAdapter
from fight.pipeline.clip_buffer import save_clip_mp4
from fight.pipeline.incident_aggregator import IncidentAggregator, Stage3Result
from fight.pipeline.pair_selector import LivePairRoiController
from fight.pipeline.person_stabilizer import TemporalPersonStabilizer
from fight.pipeline.utils import crop_from_box, open_source, sanitize_box
from fight.pose.src.pose_adapter import PoseAdapter
from fight.pose.src.pose_gate import PoseGate

LOG = logging.getLogger("multi_live")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def ts_to_str(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def safe_cuda_optimize() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            try:
                torch.backends.cudnn.benchmark = True
            except Exception:
                pass
            try:
                torch.backends.cuda.matmul.allow_tf32 = True
            except Exception:
                pass
            try:
                torch.backends.cudnn.allow_tf32 = True
            except Exception:
                pass
            try:
                torch.set_float32_matmul_precision("high")
            except Exception:
                pass
    except Exception:
        pass


def configure_runtime(args) -> None:
    try:
        cv2.setNumThreads(max(1, int(args.cv2_threads)))
    except Exception:
        pass

    os.environ.setdefault("OMP_NUM_THREADS", str(max(1, int(args.cv2_threads))))
    os.environ.setdefault("MKL_NUM_THREADS", str(max(1, int(args.cv2_threads))))
    os.environ.setdefault("NUMEXPR_NUM_THREADS", str(max(1, int(args.cv2_threads))))
    os.environ.setdefault("OPENBLAS_NUM_THREADS", str(max(1, int(args.cv2_threads))))

    safe_cuda_optimize()


def is_file_source(source: str) -> bool:
    s = str(source).strip()
    if not s:
        return False

    if s.isdigit():
        return False

    low = s.lower()
    if low.startswith(("rtsp://", "rtmp://", "http://", "https://", "udp://", "tcp://")):
        return False

    return Path(s).exists()


@dataclass
class Stage3Job:
    camera_id: str
    source: str
    event_id: str
    event_start_ts: float
    event_end_ts: float
    pose_score_max: float
    pose_score_mean: float
    clip_path: str
    frames: List
    created_at: float = field(default_factory=time.time)


@dataclass
class ClipSaveJob:
    clip_path: str
    frames: List
    fps: float


@dataclass
class ActiveEvent:
    event_id: str
    camera_id: str
    source: str
    start_ts: float
    last_ts: float
    last_positive_frame_idx: int
    frames: List = field(default_factory=list)
    pose_scores: List[float] = field(default_factory=list)
    positive_hits: int = 0


class BufferedJsonlWriter:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, row: dict) -> None:
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.flush()
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


class BufferedCsvWriter:
    def __init__(self, path: Path, fieldnames: List[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fieldnames = list(fieldnames)
        exists = self.path.exists()
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.fieldnames)
        if not exists:
            self._writer.writeheader()

    def write(self, row: dict) -> None:
        data = {k: row.get(k, "") for k in self.fieldnames}
        self._writer.writerow(data)

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.flush()
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


class ReportHub:
    def __init__(self, out_dir: Path, flush_interval_sec: float = 0.25):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.events_jsonl = BufferedJsonlWriter(self.out_dir / "events.jsonl")
        self.stage3_jsonl = BufferedJsonlWriter(self.out_dir / "stage3_results.jsonl")
        self.status_jsonl = BufferedJsonlWriter(self.out_dir / "camera_status.jsonl")

        self.events_csv = BufferedCsvWriter(
            self.out_dir / "events.csv",
            [
                "camera_id",
                "ip",
                "event_id",
                "event_start",
                "event_end",
                "duration_sec",
                "status",
                "pose_score_max",
                "pose_score_mean",
                "positive_hits",
                "clip_path",
                "queue_status",
                "queue_reason",
            ],
        )
        self.stage3_csv = BufferedCsvWriter(
            self.out_dir / "stage3_results.csv",
            [
                "camera_id",
                "ip",
                "event_id",
                "event_start",
                "event_end",
                "clip_path",
                "fight_prob",
                "fight_label",
                "pose_score_max",
                "pose_score_mean",
            ],
        )

        self.flush_interval_sec = float(flush_interval_sec)
        self.q: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=4096)
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self._run, name="report_hub", daemon=True)
        self.worker.start()

    def _run(self):
        last_flush = time.time()
        while not self.stop_event.is_set() or not self.q.empty():
            try:
                kind, row = self.q.get(timeout=0.1)
            except queue.Empty:
                if (time.time() - last_flush) >= self.flush_interval_sec:
                    self._flush_all()
                    last_flush = time.time()
                continue

            try:
                if kind == "status":
                    self.status_jsonl.write(row)
                elif kind == "event":
                    self.events_jsonl.write(row)
                    self.events_csv.write(row)
                elif kind == "stage3":
                    self.stage3_jsonl.write(row)
                    self.stage3_csv.write(row)
            finally:
                self.q.task_done()

            if (time.time() - last_flush) >= self.flush_interval_sec:
                self._flush_all()
                last_flush = time.time()

        self._flush_all()

    def _flush_all(self):
        self.status_jsonl.flush()
        self.events_jsonl.flush()
        self.stage3_jsonl.flush()
        self.events_csv.flush()
        self.stage3_csv.flush()

    def write_camera_status(self, row: dict):
        self._put("status", row)

    def write_event(self, row: dict):
        self._put("event", row)

    def write_stage3(self, row: dict):
        self._put("stage3", row)

    def _put(self, kind: str, row: dict):
        try:
            self.q.put_nowait((kind, row))
        except queue.Full:
            if kind == "status":
                try:
                    self.q.get_nowait()
                    self.q.task_done()
                except Exception:
                    pass
                try:
                    self.q.put_nowait((kind, row))
                except Exception:
                    pass
            else:
                self.q.put((kind, row))

    def close(self):
        self.stop_event.set()
        self.worker.join(timeout=5.0)
        self._flush_all()
        self.status_jsonl.close()
        self.events_jsonl.close()
        self.stage3_jsonl.close()
        self.events_csv.close()
        self.stage3_csv.close()


class PreviewWriter(threading.Thread):
    def __init__(self, stop_event: threading.Event, write_interval_sec: float, jpeg_quality: int):
        super().__init__(name="preview_writer", daemon=True)
        self.stop_event = stop_event
        self.write_interval_sec = float(write_interval_sec)
        self.jpeg_quality = int(jpeg_quality)
        self.lock = threading.Lock()
        self.latest: Dict[str, Tuple[Path, object]] = {}
        self.last_written: Dict[str, float] = {}

    def submit(self, camera_id: str, path: Path, frame_bgr):
        with self.lock:
            self.latest[camera_id] = (Path(path), frame_bgr.copy())

    def run(self):
        while not self.stop_event.is_set():
            batch: Dict[str, Tuple[Path, object]] = {}
            with self.lock:
                if self.latest:
                    batch = dict(self.latest)
                    self.latest.clear()

            now_ts = time.time()
            for camera_id, (path, frame) in batch.items():
                last_ts = self.last_written.get(camera_id, 0.0)
                if (now_ts - last_ts) < self.write_interval_sec:
                    with self.lock:
                        self.latest[camera_id] = (path, frame)
                    continue

                try:
                    ok, buf = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
                    )
                    if ok:
                        tmp_path = str(path) + ".tmp.jpg"
                        with open(tmp_path, "wb") as f:
                            f.write(buf.tobytes())
                        os.replace(tmp_path, str(path))
                        self.last_written[camera_id] = now_ts
                except Exception:
                    pass

            time.sleep(0.03)


class ClipWriter(threading.Thread):
    def __init__(self, stop_event: threading.Event, q: "queue.Queue[ClipSaveJob]"):
        super().__init__(name="clip_writer", daemon=True)
        self.stop_event = stop_event
        self.q = q

    def run(self):
        LOG.info("[CLIP] writer started")
        while not self.stop_event.is_set() or not self.q.empty():
            try:
                job = self.q.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                save_clip_mp4(job.frames, job.clip_path, fps=float(job.fps))
            except Exception as exc:
                LOG.exception("[CLIP] failed for %s: %s", job.clip_path, exc)
            finally:
                self.q.task_done()
        LOG.info("[CLIP] writer stopped")


class SharedModels:
    def __init__(
        self,
        yolo_config: str,
        yolo_weights: str,
        pose_config: str,
        stage3_config: str,
        use_pose: bool,
        use_stage3: bool,
    ):
        self.yolo = YoloAdapter(yolo_config, yolo_weights)
        self.yolo_lock = threading.Lock()

        self.pose = PoseAdapter(pose_config) if use_pose else None
        self.pose_lock = threading.Lock()

        self.stage3 = Stage3Adapter(stage3_config) if use_stage3 else None
        self.stage3_lock = threading.Lock()

    @property
    def stage3_clip_len(self) -> int:
        if self.stage3 is None:
            return 0
        return int(getattr(self.stage3, "clip_len", 32))

    def detect_persons(self, frame_bgr, person_conf: float):
        with self.yolo_lock:
            dets = self.yolo.detect_persons(frame_bgr)
        dets = [(c, box) for (c, box) in dets if float(c) >= float(person_conf)]
        dets.sort(key=lambda x: x[0], reverse=True)
        return dets

    def pose_check(self, roi_bgr):
        if self.pose is None:
            return None
        with self.pose_lock:
            return self.pose.evaluate(roi_bgr)

    def stage3_infer(self, frames) -> float:
        if self.stage3 is None:
            return 0.0
        with self.stage3_lock:
            return float(self.stage3.infer(frames))


class RuntimeStateHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.by_camera: Dict[str, dict] = {}

    def update(self, camera_id: str, **kwargs):
        with self.lock:
            row = self.by_camera.get(camera_id, {}).copy()
            row.update(kwargs)
            self.by_camera[camera_id] = row

    def get(self, camera_id: str) -> dict:
        with self.lock:
            return self.by_camera.get(camera_id, {}).copy()


class Stage3Worker(threading.Thread):
    def __init__(
        self,
        stop_event: threading.Event,
        shared: SharedModels,
        report_hub: ReportHub,
        runtime_state: RuntimeStateHub,
        q: "queue.Queue[Stage3Job]",
        fight_thr: float,
        incident_aggregator: IncidentAggregator,
    ):
        super().__init__(name="stage3_worker", daemon=True)
        self.stop_event = stop_event
        self.shared = shared
        self.report_hub = report_hub
        self.runtime_state = runtime_state
        self.q = q
        self.fight_thr = float(fight_thr)
        self.incident_aggregator = incident_aggregator

    def run(self):
        LOG.info("[STAGE3] worker started")
        while not self.stop_event.is_set() or not self.q.empty():
            try:
                job = self.q.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self.runtime_state.update(
                    job.camera_id,
                    stage="stage3",
                    detail="processing",
                    queue_status="processing",
                    queue_reason="stage3_running",
                    queue_size=self.q.qsize(),
                    queue_capacity=self.q.maxsize,
                )

                prob = self.shared.stage3_infer(job.frames)
                label = "fight" if prob >= self.fight_thr else "non_fight"

                self.incident_aggregator.submit(
                    Stage3Result(
                        camera_id=job.camera_id,
                        source=job.source,
                        event_id=job.event_id,
                        event_start_ts=job.event_start_ts,
                        event_end_ts=job.event_end_ts,
                        clip_path=job.clip_path,
                        fight_prob=float(prob),
                        fight_label=label,
                        pose_score_max=float(job.pose_score_max),
                        pose_score_mean=float(job.pose_score_mean),
                    )
                )

                row = {
                    "camera_id": job.camera_id,
                    "ip": job.source,
                    "event_id": job.event_id,
                    "event_start": ts_to_str(job.event_start_ts),
                    "event_end": ts_to_str(job.event_end_ts),
                    "clip_path": job.clip_path,
                    "fight_prob": round(float(prob), 6),
                    "fight_label": label,
                    "pose_score_max": round(float(job.pose_score_max), 6),
                    "pose_score_mean": round(float(job.pose_score_mean), 6),
                    "queued_at": ts_to_str(job.created_at),
                    "processed_at": now_str(),
                }
                self.report_hub.write_stage3(row)

                self.runtime_state.update(
                    job.camera_id,
                    stage="stage3",
                    detail="completed",
                    latest_event_status=label,
                    latest_stage3_prob=round(float(prob), 6),
                    latest_stage3_label=label,
                    queue_status="processed",
                    queue_reason="stage3_done",
                    queue_size=self.q.qsize(),
                    queue_capacity=self.q.maxsize,
                )

                self.report_hub.write_camera_status(
                    {
                        "ts": now_str(),
                        "camera_id": job.camera_id,
                        "ip": job.source,
                        "stage": "stage3",
                        "detail": "completed",
                        "event_id": job.event_id,
                        "latest_event_status": label,
                        "latest_stage3_prob": round(float(prob), 6),
                        "latest_stage3_label": label,
                        "fight_prob": round(float(prob), 6),
                        "fight_label": label,
                        "queue_status": "processed",
                        "queue_reason": "stage3_done",
                        "queue_size": self.q.qsize(),
                        "queue_capacity": self.q.maxsize,
                        "clip_path": job.clip_path,
                    }
                )

                LOG.info(
                    "[STAGE3][%s] event=%s prob=%.4f label=%s clip=%s",
                    job.camera_id,
                    job.event_id,
                    prob,
                    label,
                    job.clip_path,
                )
            except Exception as exc:
                LOG.exception("[STAGE3] failed for camera=%s event=%s: %s", job.camera_id, job.event_id, exc)
                self.runtime_state.update(
                    job.camera_id,
                    stage="stage3",
                    detail="failed",
                    queue_status="failed",
                    queue_reason=str(exc),
                    queue_size=self.q.qsize(),
                    queue_capacity=self.q.maxsize,
                )
            finally:
                self.q.task_done()

        LOG.info("[STAGE3] worker stopped")


class CameraWorker(threading.Thread):
    def __init__(
        self,
        camera_id: str,
        source: str,
        stop_event: threading.Event,
        shared: SharedModels,
        report_hub: ReportHub,
        runtime_state: RuntimeStateHub,
        stage3_queue: "queue.Queue[Stage3Job]",
        clip_queue: "queue.Queue[ClipSaveJob]",
        preview_writer: PreviewWriter,
        args,
    ):
        super().__init__(name=f"cam_{camera_id}", daemon=True)

        self.camera_id = str(camera_id)
        self.source = str(source)
        self.source_is_file = is_file_source(self.source)
        self.stop_event = stop_event
        self.shared = shared
        self.report_hub = report_hub
        self.runtime_state = runtime_state
        self.stage3_queue = stage3_queue
        self.clip_queue = clip_queue
        self.preview_writer = preview_writer
        self.args = args

        self.motion = MotionAdapter(args.motion_config)
        self.person_stabilizer = TemporalPersonStabilizer(
            max_age=8,
            min_hits=1,
            iou_match_thr=0.22,
            conf_alpha=0.65,
            max_tracks=12,
        )
        self.pair_roi = LivePairRoiController(
            enter_score=0.58,
            keep_score=0.42,
            keep_frames=12,
            min_hits_to_activate=2,
            candidate_confirm_frames=2,
            pair_identity_iou_thr=0.35,
            switch_margin=0.06,
            roi_expand_x=1.20,
            roi_expand_y=1.14,
            debug=False,
        )

        temporal_cfg = self._load_pose_temporal(args.pose_config)
        self.pose_gate = PoseGate(
            window_size=int(temporal_cfg.get("window_size", 6)),
            need_positive=int(temporal_cfg.get("need_positive", 2)),
            min_mean_score=float(temporal_cfg.get("min_mean_score", 0.30)),
            peak_score_thr=float(temporal_cfg.get("peak_score_thr", 0.54)),
            min_consecutive=int(temporal_cfg.get("min_consecutive", 2)),
        )

        self.cap = None
        self.frame_idx = -1
        self.event_counter = 0
        self.last_pose_score = 0.0
        self.last_pose_positive = False
        self.last_pose_hit_idx = -10_000
        self.last_yolo_hit_idx = -10_000
        self.last_pair_hit_idx = -10_000
        self.active_event: Optional[ActiveEvent] = None
        self.prebuffer: Deque = deque(maxlen=int(args.prebuffer_frames))

        self.temp_segments_dir = Path(args.output_dir) / "temp_segments"
        self.temp_segments_dir.mkdir(parents=True, exist_ok=True)

        self.previews_dir = Path(args.output_dir) / "previews"
        self.previews_dir.mkdir(parents=True, exist_ok=True)

        self.preview_path = self.previews_dir / f"{self.camera_id}.jpg"
        self.preview_every = max(1, int(args.preview_every_frames))
        self.status_log_every = int(max(1, args.status_log_every))

        self.latest_stage3_prob = None
        self.latest_stage3_label = "-"
        self.latest_event_status = "idle"
        self.latest_queue_status = "-"
        self.latest_queue_reason = "-"

    def _load_pose_temporal(self, cfg_path: str) -> dict:
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            out = cfg.get("temporal", {})
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}

    def _open(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = open_source(self.source)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open source: {self.source}")
        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

    def _safe_reset_for_idle(self):
        self.person_stabilizer.reset()
        self.pair_roi.reset()
        self.pose_gate.reset()
        self.last_pose_positive = False
        self.last_pose_score = 0.0
        self.last_pose_hit_idx = -10_000
        self.last_yolo_hit_idx = -10_000
        self.last_pair_hit_idx = -10_000
        self.prebuffer.clear()

    def _new_event(self, now_ts: float, seed_frames: List, pose_score: float):
        self.event_counter += 1
        event_id = f"{self.camera_id}_{self.event_counter:06d}"
        frames = [f.copy() for f in seed_frames[-int(self.args.prebuffer_frames):]]
        self.active_event = ActiveEvent(
            event_id=event_id,
            camera_id=self.camera_id,
            source=self.source,
            start_ts=now_ts,
            last_ts=now_ts,
            last_positive_frame_idx=self.frame_idx,
            frames=frames,
            pose_scores=[float(pose_score)],
            positive_hits=1,
        )
        self.latest_event_status = "event_started"
        self.latest_queue_status = "-"
        self.latest_queue_reason = "-"

        self.runtime_state.update(
            self.camera_id,
            event_id=event_id,
            latest_event_status="event_started",
            queue_status="-",
            queue_reason="-",
            pose_score=round(float(pose_score), 6),
            event_active=1,
        )

        self._status(
            "event",
            "started",
            {
                "event_id": event_id,
                "latest_event_status": "event_started",
                "pose_score": round(float(pose_score), 6),
                "queue_status": "-",
                "queue_reason": "-",
            },
        )
        LOG.info("[CAM %s] event started -> %s", self.camera_id, event_id)

    def _append_event(self, roi_bgr, now_ts: float, pose_score: float, positive_hit: bool):
        if self.active_event is None:
            return

        self.active_event.last_ts = now_ts
        self.active_event.frames.append(roi_bgr.copy())
        self.active_event.pose_scores.append(float(pose_score))
        if positive_hit:
            self.active_event.positive_hits += 1
            self.active_event.last_positive_frame_idx = self.frame_idx

        max_frames = int(self.args.max_event_frames)
        if max_frames > 0 and len(self.active_event.frames) >= max_frames:
            self._close_event(reason="max_event_frames")

    def _close_event(self, reason: str):
        ev = self.active_event
        self.active_event = None
        if ev is None:
            return

        event_end_ts = ev.last_ts
        event_duration_sec = max(0.0, float(event_end_ts) - float(ev.start_ts))

        pose_max = max(ev.pose_scores) if ev.pose_scores else 0.0
        pose_mean = sum(ev.pose_scores) / max(1, len(ev.pose_scores)) if ev.pose_scores else 0.0

        clip_name = (
            f"cam_{self.camera_id}__evt_{ev.event_id}__"
            f"{datetime.fromtimestamp(ev.start_ts).strftime('%Y%m%d_%H%M%S_%f')[:-3]}.mp4"
        )
        clip_path = self.temp_segments_dir / clip_name

        # Temp segmenti kanıt/debug için yine kaydediyoruz.
        # Ancak Stage3'e göndermek için aşağıdaki kalite filtresinden geçmesi gerekir.
        if ev.frames:
            try:
                self.clip_queue.put_nowait(
                    ClipSaveJob(
                        clip_path=str(clip_path),
                        frames=list(ev.frames),
                        fps=float(self.args.clip_fps),
                    )
                )
            except queue.Full:
                LOG.warning("[CAM %s] clip queue full; saving synchronously %s", self.camera_id, clip_path)
                try:
                    save_clip_mp4(ev.frames, str(clip_path), fps=float(self.args.clip_fps))
                except Exception as exc:
                    LOG.exception("[CAM %s] sync clip save failed: %s", self.camera_id, exc)

        queue_status = "not_requested"
        queue_reason = reason

        min_queue_frames = int(self.args.min_queue_frames)

        min_positive_hits = int(getattr(self.args, "stage3_event_min_positive_hits", 12))
        min_pose_mean = float(getattr(self.args, "stage3_event_min_pose_mean", 0.28))
        min_pose_max = float(getattr(self.args, "stage3_event_min_pose_max", 0.58))
        min_duration_sec = float(getattr(self.args, "stage3_event_min_duration_sec", 0.70))

        raw_drop_reasons = str(
            getattr(
                self.args,
                "stage3_drop_close_reasons",
                "pair_roi_missing,roi_crop_failed",
            )
        )
        drop_reasons = {x.strip() for x in raw_drop_reasons.split(",") if x.strip()}

        event_quality_ok = True
        quality_reasons: List[str] = []

        # Bu kapanış sebepleri normal videolarda false positive üretmeye yatkın.
        if str(reason) in drop_reasons:
            event_quality_ok = False
            quality_reasons.append(f"bad_close_reason_{reason}")

        if int(len(ev.frames)) < min_queue_frames:
            event_quality_ok = False
            quality_reasons.append(f"short_frames_{len(ev.frames)}<{min_queue_frames}")

        if float(event_duration_sec) < min_duration_sec:
            event_quality_ok = False
            quality_reasons.append(f"short_duration_{event_duration_sec:.2f}<{min_duration_sec:.2f}")

        if int(ev.positive_hits) < min_positive_hits:
            event_quality_ok = False
            quality_reasons.append(f"low_positive_hits_{ev.positive_hits}<{min_positive_hits}")

        if float(pose_mean) < min_pose_mean:
            event_quality_ok = False
            quality_reasons.append(f"low_pose_mean_{pose_mean:.3f}<{min_pose_mean:.3f}")

        if float(pose_max) < min_pose_max:
            event_quality_ok = False
            quality_reasons.append(f"low_pose_max_{pose_max:.3f}<{min_pose_max:.3f}")

        if self.shared.stage3 is not None:
            if not event_quality_ok:
                queue_status = "dropped"
                queue_reason = "low_event_quality:" + ",".join(quality_reasons)
            else:
                job = Stage3Job(
                    camera_id=self.camera_id,
                    source=self.source,
                    event_id=ev.event_id,
                    event_start_ts=ev.start_ts,
                    event_end_ts=event_end_ts,
                    pose_score_max=float(pose_max),
                    pose_score_mean=float(pose_mean),
                    clip_path=str(clip_path),
                    frames=list(ev.frames),
                )
                try:
                    self.stage3_queue.put_nowait(job)
                    queue_status = "queued"
                    queue_reason = "stage3_queue"
                except queue.Full:
                    queue_status = "dropped"
                    queue_reason = "stage3_queue_full"
        else:
            queue_status = "skipped"
            queue_reason = "stage3_disabled"

        row = {
            "camera_id": self.camera_id,
            "ip": self.source,
            "event_id": ev.event_id,
            "event_start": ts_to_str(ev.start_ts),
            "event_end": ts_to_str(event_end_ts),
            "duration_sec": round(float(event_duration_sec), 3),
            "status": reason,
            "pose_score_max": round(float(pose_max), 6),
            "pose_score_mean": round(float(pose_mean), 6),
            "positive_hits": int(ev.positive_hits),
            "clip_path": str(clip_path),
            "queue_status": queue_status,
            "queue_reason": queue_reason,
            "frames": int(len(ev.frames)),
            "closed_at": now_str(),
        }
        self.report_hub.write_event(row)

        self.latest_event_status = reason
        self.latest_queue_status = queue_status
        self.latest_queue_reason = queue_reason

        self.runtime_state.update(
            self.camera_id,
            stage="event_close",
            detail=reason,
            event_active=0,
            latest_event_status=reason,
            queue_status=queue_status,
            queue_reason=queue_reason,
            queue_size=self.stage3_queue.qsize(),
            queue_capacity=self.stage3_queue.maxsize,
            pose_score=round(float(pose_mean), 6),
            clip_path=str(clip_path),
        )

        self._status(
            "event_close",
            reason,
            {
                "event_id": ev.event_id,
                "latest_event_status": reason,
                "queue_status": queue_status,
                "queue_reason": queue_reason,
                "latest_stage3_prob": self.latest_stage3_prob,
                "latest_stage3_label": self.latest_stage3_label,
                "frames": int(len(ev.frames)),
                "duration_sec": round(float(event_duration_sec), 3),
                "pose_score": round(float(pose_mean), 6),
                "pose_score_max": round(float(pose_max), 6),
                "positive_hits": int(ev.positive_hits),
                "queue_size": self.stage3_queue.qsize(),
                "queue_capacity": self.stage3_queue.maxsize,
                "clip_path": str(clip_path),
            },
        )

        LOG.info(
            "[CAM %s] event closed -> %s reason=%s frames=%d duration=%.2fs "
            "pose_mean=%.3f pose_max=%.3f hits=%d queue=%s/%s",
            self.camera_id,
            ev.event_id,
            reason,
            len(ev.frames),
            event_duration_sec,
            pose_mean,
            pose_max,
            ev.positive_hits,
            queue_status,
            queue_reason,
        )

    def _status(self, stage: str, detail: str, extra: Optional[dict] = None):
        row = {
            "ts": now_str(),
            "camera_id": self.camera_id,
            "ip": self.source,
            "stage": stage,
            "detail": detail,
        }
        if extra:
            row.update(extra)
        self.report_hub.write_camera_status(row)

    def _render_preview(
        self,
        frame_bgr,
        stage: str,
        detail: str,
        persons: int,
        pose_score: float,
        pair_ok: int,
        event_active: int,
    ):
        try:
            vis = frame_bgr.copy()
            h, w = vis.shape[:2]

            overlay_h = min(190, h - 20)
            cv2.rectangle(vis, (10, 10), (w - 10, overlay_h), (20, 20, 20), -1)
            cv2.rectangle(vis, (10, 10), (w - 10, overlay_h), (0, 255, 255), 2)

            snap = self.runtime_state.get(self.camera_id)

            lines = [
                f"cam: {self.camera_id}",
                f"stage: {stage}",
                f"detail: {detail}",
                f"persons: {persons}",
                f"pair_ok: {pair_ok}",
                f"pose: {pose_score:.3f}",
                f"event_active: {event_active}",
                f"latest_event: {snap.get('latest_event_status', self.latest_event_status)}",
                f"stage3: {snap.get('latest_stage3_label', self.latest_stage3_label)} / {snap.get('latest_stage3_prob', self.latest_stage3_prob)}",
                f"queue: {snap.get('queue_status', self.latest_queue_status)} ({snap.get('queue_reason', self.latest_queue_reason)})",
                f"qsize: {snap.get('queue_size', self.stage3_queue.qsize())} / {snap.get('queue_capacity', self.stage3_queue.maxsize)}",
            ]

            y = 32
            for line in lines:
                cv2.putText(
                    vis,
                    str(line),
                    (20, y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.60,
                    (255, 255, 255),
                    2,
                    cv2.LINE_AA,
                )
                y += 16
                if y > overlay_h - 10:
                    break

            return vis
        except Exception:
            return frame_bgr

    def _submit_preview(self, frame_bgr):
        self.preview_writer.submit(self.camera_id, self.preview_path, frame_bgr)

    def run(self):
        self._status("camera", "starting")
        self.runtime_state.update(
            self.camera_id,
            stage="camera",
            detail="starting",
            persons=0,
            pair_ok=0,
            pose_positive=0,
            pose_score=0.0,
            event_active=0,
            latest_event_status="idle",
            latest_stage3_prob=None,
            latest_stage3_label="-",
            queue_status="-",
            queue_reason="-",
            queue_size=0,
            queue_capacity=self.stage3_queue.maxsize,
            source=self.source,
        )

        try:
            self._open()
        except Exception as exc:
            self._status("camera", "open_failed", {"error": str(exc)})
            self.runtime_state.update(
                self.camera_id,
                stage="camera",
                detail="open_failed",
                error=str(exc),
            )
            LOG.exception("[CAM %s] open failed: %s", self.camera_id, exc)
            return

        yolo_ctr = 0
        pose_ctr = 0
        recent_persons: List[Tuple[float, Tuple[int, int, int, int]]] = []

        while not self.stop_event.is_set():
            ret, frame = self.cap.read()
            if not ret:
                if self.source_is_file:
                    self._status("camera", "eof_reached")
                    self.runtime_state.update(
                        self.camera_id,
                        stage="camera",
                        detail="eof_reached",
                        event_active=int(self.active_event is not None),
                    )

                    if self.active_event is not None:
                        self._close_event(reason="eof_reached")

                    LOG.info("[CAM %s] file source finished, stopping worker", self.camera_id)
                    break

                self._status("camera", "read_failed_reconnecting")
                self.runtime_state.update(
                    self.camera_id,
                    stage="camera",
                    detail="read_failed_reconnecting",
                )
                try:
                    self.cap.release()
                except Exception:
                    pass
                time.sleep(float(self.args.reconnect_sec))
                try:
                    self._open()
                    continue
                except Exception as exc:
                    self._status("camera", "reconnect_failed", {"error": str(exc)})
                    self.runtime_state.update(
                        self.camera_id,
                        stage="camera",
                        detail="reconnect_failed",
                        error=str(exc),
                    )
                    time.sleep(float(self.args.reconnect_sec))
                    continue

            self.frame_idx += 1
            now_ts = time.time()

            score, motion_active, frame_resized, _ = self.motion.step(now_ts, frame)
            base_frame = frame if frame_resized is None else frame_resized

            if not motion_active:
                if self.active_event is not None:
                    self._close_event(reason="motion_off")
                self._safe_reset_for_idle()

                self.runtime_state.update(
                    self.camera_id,
                    stage="motion",
                    detail="inactive",
                    motion_score=round(float(score), 6),
                    persons=0,
                    pair_ok=0,
                    pose_positive=0,
                    pose_score=round(float(self.last_pose_score), 6),
                    event_active=int(self.active_event is not None),
                    latest_event_status=self.latest_event_status,
                    latest_stage3_prob=self.runtime_state.get(self.camera_id).get("latest_stage3_prob"),
                    latest_stage3_label=self.runtime_state.get(self.camera_id).get("latest_stage3_label", "-"),
                    queue_status=self.latest_queue_status,
                    queue_reason=self.latest_queue_reason,
                    queue_size=self.stage3_queue.qsize(),
                    queue_capacity=self.stage3_queue.maxsize,
                )

                if self.frame_idx % self.preview_every == 0:
                    preview = self._render_preview(
                        base_frame,
                        stage="motion",
                        detail="inactive",
                        persons=0,
                        pose_score=float(self.last_pose_score),
                        pair_ok=0,
                        event_active=int(self.active_event is not None),
                    )
                    self._submit_preview(preview)

                if self.frame_idx % self.status_log_every == 0:
                    self._status(
                        "motion",
                        "inactive",
                        {
                            "motion_score": round(float(score), 6),
                            "persons": 0,
                            "pair_ok": 0,
                            "pose_positive": 0,
                            "pose_score": round(float(self.last_pose_score), 6),
                            "event_active": int(self.active_event is not None),
                            "latest_event_status": self.latest_event_status,
                            "queue_status": self.latest_queue_status,
                            "queue_reason": self.latest_queue_reason,
                        },
                    )
                continue

            yolo_ctr += 1
            if yolo_ctr % max(1, int(self.args.yolo_stride)) == 0:
                det_persons = self.shared.detect_persons(base_frame, person_conf=float(self.args.person_conf))
                recent_persons = self.person_stabilizer.update(det_persons)
            else:
                recent_persons = self.person_stabilizer.predict_only()

            if recent_persons:
                self.last_yolo_hit_idx = self.frame_idx

            if len(recent_persons) < int(self.args.min_persons_for_pose):
                self.runtime_state.update(
                    self.camera_id,
                    stage="yolo",
                    detail="not_enough_persons",
                    motion_score=round(float(score), 6),
                    persons=len(recent_persons),
                    pair_ok=0,
                    pose_positive=0,
                    pose_score=round(float(self.last_pose_score), 6),
                    event_active=int(self.active_event is not None),
                    latest_event_status=self.latest_event_status,
                    queue_status=self.latest_queue_status,
                    queue_reason=self.latest_queue_reason,
                    queue_size=self.stage3_queue.qsize(),
                    queue_capacity=self.stage3_queue.maxsize,
                )

                if self.frame_idx % self.preview_every == 0:
                    preview = self._render_preview(
                        base_frame,
                        stage="yolo",
                        detail="not_enough_persons",
                        persons=len(recent_persons),
                        pose_score=float(self.last_pose_score),
                        pair_ok=0,
                        event_active=int(self.active_event is not None),
                    )
                    self._submit_preview(preview)

                if self.active_event is not None:
                    if (self.frame_idx - self.active_event.last_positive_frame_idx) > int(self.args.event_close_grace_frames):
                        self._close_event(reason="no_persons_for_pose")
                if self.frame_idx % self.status_log_every == 0:
                    self._status(
                        "yolo",
                        "not_enough_persons",
                        {
                            "persons": len(recent_persons),
                            "motion_score": round(float(score), 6),
                            "pair_ok": 0,
                            "pose_positive": 0,
                            "pose_score": round(float(self.last_pose_score), 6),
                            "event_active": int(self.active_event is not None),
                            "latest_event_status": self.latest_event_status,
                            "queue_status": self.latest_queue_status,
                            "queue_reason": self.latest_queue_reason,
                        },
                    )
                continue

            pair_res = self.pair_roi.update(recent_persons, base_frame.shape)
            roi_box = pair_res.get("roi_box") if pair_res.get("roi_ok") else None
            roi_box = sanitize_box(roi_box, base_frame.shape) if roi_box is not None else None

            if roi_box is None:
                self.runtime_state.update(
                    self.camera_id,
                    stage="pair",
                    detail="roi_missing",
                    persons=len(recent_persons),
                    pair_ok=0,
                    pose_positive=0,
                    pose_score=round(float(self.last_pose_score), 6),
                    event_active=int(self.active_event is not None),
                    latest_event_status=self.latest_event_status,
                    queue_status=self.latest_queue_status,
                    queue_reason=self.latest_queue_reason,
                    queue_size=self.stage3_queue.qsize(),
                    queue_capacity=self.stage3_queue.maxsize,
                )

                if self.frame_idx % self.preview_every == 0:
                    preview = self._render_preview(
                        base_frame,
                        stage="pair",
                        detail="roi_missing",
                        persons=len(recent_persons),
                        pose_score=float(self.last_pose_score),
                        pair_ok=0,
                        event_active=int(self.active_event is not None),
                    )
                    self._submit_preview(preview)

                if self.active_event is not None:
                    if (self.frame_idx - self.active_event.last_positive_frame_idx) > int(self.args.event_close_grace_frames):
                        self._close_event(reason="pair_roi_missing")
                if self.frame_idx % self.status_log_every == 0:
                    self._status(
                        "pair",
                        "roi_missing",
                        {
                            "persons": len(recent_persons),
                            "pair_ok": 0,
                            "pose_positive": 0,
                            "pose_score": round(float(self.last_pose_score), 6),
                            "event_active": int(self.active_event is not None),
                            "latest_event_status": self.latest_event_status,
                            "queue_status": self.latest_queue_status,
                            "queue_reason": self.latest_queue_reason,
                        },
                    )
                continue

            self.last_pair_hit_idx = self.frame_idx
            roi_bgr = crop_from_box(base_frame, roi_box, out_size=int(self.args.roi_size))

            if roi_bgr is None:
                self.runtime_state.update(
                    self.camera_id,
                    stage="pair",
                    detail="roi_crop_failed",
                    persons=len(recent_persons),
                    pair_ok=int(pair_res.get("pair_ok", 0)),
                    pose_positive=0,
                    pose_score=round(float(self.last_pose_score), 6),
                    event_active=int(self.active_event is not None),
                    latest_event_status=self.latest_event_status,
                    queue_status=self.latest_queue_status,
                    queue_reason=self.latest_queue_reason,
                    queue_size=self.stage3_queue.qsize(),
                    queue_capacity=self.stage3_queue.maxsize,
                )

                if self.frame_idx % self.preview_every == 0:
                    preview = self._render_preview(
                        base_frame,
                        stage="pair",
                        detail="roi_crop_failed",
                        persons=len(recent_persons),
                        pose_score=float(self.last_pose_score),
                        pair_ok=int(pair_res.get("pair_ok", 0)),
                        event_active=int(self.active_event is not None),
                    )
                    self._submit_preview(preview)

                if self.active_event is not None:
                    if (self.frame_idx - self.active_event.last_positive_frame_idx) > int(self.args.event_close_grace_frames):
                        self._close_event(reason="roi_crop_failed")
                continue

            self.prebuffer.append(roi_bgr.copy())

            pose_positive = False
            pose_score = self.last_pose_score
            if self.shared.pose is not None:
                pose_ctr += 1
                if pose_ctr % max(1, int(self.args.pose_stride)) == 0:
                    pres = self.shared.pose_check(roi_bgr)
                    if pres is not None:
                        gate = self.pose_gate.update(float(pres.score), bool(pres.ok))
                        self.last_pose_score = float(pres.score)
                        self.last_pose_positive = bool(gate.pose_ok)
                        if self.last_pose_positive:
                            self.last_pose_hit_idx = self.frame_idx
                pose_positive = self.last_pose_positive or (
                    (self.frame_idx - self.last_pose_hit_idx) <= int(self.args.pose_hold_frames)
                )
                pose_score = self.last_pose_score
            else:
                pose_positive = True
                pose_score = 1.0

            if pose_positive:
                if self.active_event is None:
                    self._new_event(now_ts=now_ts, seed_frames=list(self.prebuffer), pose_score=pose_score)
                self._append_event(roi_bgr=roi_bgr, now_ts=now_ts, pose_score=pose_score, positive_hit=True)
            else:
                if self.active_event is not None:
                    gap = self.frame_idx - self.active_event.last_positive_frame_idx
                    if gap <= int(self.args.event_close_grace_frames):
                        self._append_event(roi_bgr=roi_bgr, now_ts=now_ts, pose_score=pose_score, positive_hit=False)
                    else:
                        self._close_event(reason="pose_timeout")

            self.runtime_state.update(
                self.camera_id,
                stage="pipeline",
                detail="tick",
                motion_score=round(float(score), 6),
                persons=len(recent_persons),
                pair_ok=int(pair_res.get("pair_ok", 0)),
                pose_positive=int(bool(pose_positive)),
                pose_score=round(float(pose_score), 6),
                event_active=int(self.active_event is not None),
                latest_event_status=self.latest_event_status,
                queue_status=self.latest_queue_status,
                queue_reason=self.latest_queue_reason,
                queue_size=self.stage3_queue.qsize(),
                queue_capacity=self.stage3_queue.maxsize,
                source=self.source,
            )

            if self.frame_idx % self.preview_every == 0:
                preview = self._render_preview(
                    roi_bgr,
                    stage="pipeline",
                    detail="tick",
                    persons=len(recent_persons),
                    pose_score=float(pose_score),
                    pair_ok=int(pair_res.get("pair_ok", 0)),
                    event_active=int(self.active_event is not None),
                )
                self._submit_preview(preview)

            if self.frame_idx % self.status_log_every == 0:
                self._status(
                    "pipeline",
                    "tick",
                    {
                        "motion_score": round(float(score), 6),
                        "persons": len(recent_persons),
                        "pair_ok": int(pair_res.get("pair_ok", 0)),
                        "pose_positive": int(bool(pose_positive)),
                        "pose_score": round(float(pose_score), 6),
                        "event_active": int(self.active_event is not None),
                        "latest_event_status": self.latest_event_status,
                        "latest_stage3_prob": self.runtime_state.get(self.camera_id).get("latest_stage3_prob"),
                        "latest_stage3_label": self.runtime_state.get(self.camera_id).get("latest_stage3_label", "-"),
                        "queue_status": self.latest_queue_status,
                        "queue_reason": self.latest_queue_reason,
                        "queue_size": self.stage3_queue.qsize(),
                        "queue_capacity": self.stage3_queue.maxsize,
                    },
                )

        if self.active_event is not None:
            self._close_event(reason="shutdown")

        try:
            self.motion.close()
        except Exception:
            pass
        try:
            if self.cap is not None:
                self.cap.release()
        except Exception:
            pass

        self.runtime_state.update(
            self.camera_id,
            stage="camera",
            detail="stopped",
            event_active=0,
        )
        self._status("camera", "stopped")


def parse_camera_specs(args) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []

    for raw in args.camera:
        raw = str(raw).strip()
        if not raw:
            continue
        if "=" in raw:
            cid, src = raw.split("=", 1)
        elif "," in raw:
            cid, src = raw.split(",", 1)
        else:
            raise ValueError(f"Invalid --camera format: {raw}. Use camera_id=source")
        items.append((cid.strip(), src.strip()))

    for i, src in enumerate(args.source):
        cid = f"cam_{len(items) + i + 1:03d}"
        items.append((cid, str(src).strip()))

    if args.sources_file:
        with open(args.sources_file, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f, 1):
                ln = line.strip()
                if not ln or ln.startswith("#"):
                    continue
                if "=" in ln:
                    cid, src = ln.split("=", 1)
                elif "," in ln:
                    cid, src = ln.split(",", 1)
                else:
                    cid, src = f"cam_file_{idx:03d}", ln
                items.append((cid.strip(), src.strip()))

    seen = set()
    deduped = []
    for cid, src in items:
        if not cid:
            cid = f"cam_{len(deduped)+1:03d}"
        if cid in seen:
            raise ValueError(f"Duplicate camera_id: {cid}")
        seen.add(cid)
        deduped.append((cid, src))

    return deduped


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", action="append", default=[], help="camera_id=source")
    ap.add_argument("--source", action="append", default=[], help="source without id; id auto-generated")
    ap.add_argument("--sources-file", type=str, default="", help="lines: camera_id=rtsp://... or camera_id,rtsp://...")

    ap.add_argument("--motion-config", type=str, default="fight/motion/configs/motion.yaml")
    ap.add_argument("--yolo-config", type=str, default="fight/yolo/configs/yolo.yaml")
    ap.add_argument("--yolo-weights", type=str, default="fight/yolo11n.pt")
    ap.add_argument("--pose-config", type=str, default="fight/pose/configs/pose.yaml")
    ap.add_argument("--stage3-config", type=str, default="fight/3D_CNN/configs/stage3.yaml")

    ap.add_argument("--person-conf", type=float, default=0.25)
    ap.add_argument("--yolo-stride", type=int, default=3)
    ap.add_argument("--pose-stride", type=int, default=2)
    ap.add_argument("--roi-size", type=int, default=320)
    ap.add_argument("--min-persons-for-pose", type=int, default=2)
    ap.add_argument("--pose-hold-frames", type=int, default=18)
    ap.add_argument("--event-close-grace-frames", type=int, default=42)
    ap.add_argument("--prebuffer-frames", type=int, default=24)
    ap.add_argument("--max-event-frames", type=int, default=360)
    ap.add_argument("--clip-fps", type=float, default=16.0)
    ap.add_argument("--reconnect-sec", type=float, default=1.0)
    ap.add_argument("--status-log-every", type=int, default=30)

    ap.add_argument("--use-pose", action="store_true", default=True)
    ap.add_argument("--use-stage3", action="store_true", default=True)
    ap.add_argument("--fight-thr", type=float, default=0.52)
    ap.add_argument("--min-queue-frames", type=int, default=32, help="minimum clip frames before sending to stage3")
    ap.add_argument("--stage3-queue-size", type=int, default=64)

    # Stage3'e göndermeden önce event kalite filtresi.
    # Normal videolarda düşük kaliteli pose eventleri 0.50 civarı X3D skoru üretip false alarm yapabiliyor.
    ap.add_argument("--stage3-event-min-positive-hits", type=int, default=8)
    ap.add_argument("--stage3-event-min-pose-mean", type=float, default=0.20)
    ap.add_argument("--stage3-event-min-pose-max", type=float, default=0.45)
    ap.add_argument("--stage3-event-min-duration-sec", type=float, default=0.45)
    ap.add_argument(
        "--stage3-drop-close-reasons",
        type=str,
        default="pair_roi_missing,roi_crop_failed",
    )

    ap.add_argument("--incident-enter-thr", type=float, default=0.52)
    ap.add_argument("--incident-keep-thr", type=float, default=0.48)
    ap.add_argument("--incident-vote-window", type=int, default=7)
    ap.add_argument("--incident-vote-enter-needed", type=int, default=2)
    ap.add_argument("--incident-vote-keep-needed", type=int, default=2)
    ap.add_argument("--incident-merge-gap-sec", type=float, default=20.0)
    ap.add_argument("--incident-max-bridge-nonfight", type=int, default=1)
    ap.add_argument("--incident-min-segments", type=int, default=2)
    ap.add_argument("--incident-single-strong-fight-thr", type=float, default=0.68)
    ap.add_argument("--incident-confirm-min-duration-sec", type=float, default=0.8)
    ap.add_argument("--incident-cooldown-sec", type=float, default=60.0)
    ap.add_argument("--incident-clip-ready-wait-sec", type=float, default=8.0)
    ap.add_argument("--incident-stale-finalize-sec", type=float, default=8.0)
    ap.add_argument("--incident-temporal-iou-merge-thr", type=float, default=0.30)
    ap.add_argument("--incident-write-nonfight", action="store_true", default=False)

    ap.add_argument("--preview-every-frames", type=int, default=4)
    ap.add_argument("--preview-write-interval-sec", type=float, default=0.50)
    ap.add_argument("--preview-jpeg-quality", type=int, default=80)
    ap.add_argument("--clip-writer-queue-size", type=int, default=32)
    ap.add_argument("--report-flush-interval-sec", type=float, default=0.25)
    ap.add_argument("--cv2-threads", type=int, default=1)

    ap.add_argument("--output-dir", type=str, default="fight/pipeline/outputs/multi_live")
    ap.add_argument("--run-name", type=str, default="auto")
    return ap


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )


def main():
    setup_logging()
    ap = build_argparser()
    args = ap.parse_args()

    configure_runtime(args)

    cameras = parse_camera_specs(args)
    if not cameras:
        raise SystemExit("No cameras provided. Use --camera camera_id=source or --sources-file list.txt")

    run_name = args.run_name
    if run_name == "auto":
        run_name = datetime.now().strftime("run_%Y%m%d_%H%M%S")

    output_dir = Path(args.output_dir) / run_name
    output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = str(output_dir)

    report_hub = ReportHub(output_dir, flush_interval_sec=float(args.report_flush_interval_sec))
    runtime_state = RuntimeStateHub()
    stop_event = threading.Event()
    incident_dir = Path(args.output_dir) / "incidents"
    incident_dir.mkdir(parents=True, exist_ok=True)

    incident_aggregator = IncidentAggregator(
        out_dir=str(incident_dir),
        merge_gap_sec=float(args.incident_merge_gap_sec),
        max_bridge_nonfight=int(args.incident_max_bridge_nonfight),
        enter_thr=float(args.incident_enter_thr),
        keep_thr=float(args.incident_keep_thr),
        vote_window=int(args.incident_vote_window),
        vote_enter_needed=int(args.incident_vote_enter_needed),
        vote_keep_needed=int(args.incident_vote_keep_needed),
        min_incident_segments=int(args.incident_min_segments),
        single_strong_fight_thr=float(args.incident_single_strong_fight_thr),
        confirm_min_duration_sec=float(args.incident_confirm_min_duration_sec),
        cooldown_sec=float(args.incident_cooldown_sec),
        keep_temp_parts=True,
        write_nonfight_incidents=bool(args.incident_write_nonfight),
        clip_ready_wait_sec=float(args.incident_clip_ready_wait_sec),
        stale_finalize_sec=float(args.incident_stale_finalize_sec),
        temporal_iou_merge_thr=float(args.incident_temporal_iou_merge_thr),
    )

    shared = SharedModels(
        yolo_config=args.yolo_config,
        yolo_weights=args.yolo_weights,
        pose_config=args.pose_config,
        stage3_config=args.stage3_config,
        use_pose=bool(args.use_pose),
        use_stage3=bool(args.use_stage3),
    )

    if shared.stage3 is not None:
        model_clip_len = int(shared.stage3_clip_len)
        args.min_queue_frames = max(int(args.min_queue_frames), min(model_clip_len, 20))

    stage3_queue: "queue.Queue[Stage3Job]" = queue.Queue(maxsize=int(args.stage3_queue_size))
    clip_queue: "queue.Queue[ClipSaveJob]" = queue.Queue(maxsize=int(args.clip_writer_queue_size))

    preview_writer = PreviewWriter(
        stop_event=stop_event,
        write_interval_sec=float(args.preview_write_interval_sec),
        jpeg_quality=int(args.preview_jpeg_quality),
    )
    clip_writer = ClipWriter(stop_event=stop_event, q=clip_queue)

    stage3_worker = Stage3Worker(
        stop_event=stop_event,
        shared=shared,
        report_hub=report_hub,
        runtime_state=runtime_state,
        q=stage3_queue,
        fight_thr=float(args.fight_thr),
        incident_aggregator=incident_aggregator,
    )

    workers = [
        CameraWorker(
            camera_id=camera_id,
            source=source,
            stop_event=stop_event,
            shared=shared,
            report_hub=report_hub,
            runtime_state=runtime_state,
            stage3_queue=stage3_queue,
            clip_queue=clip_queue,
            preview_writer=preview_writer,
            args=args,
        )
        for camera_id, source in cameras
    ]

    def _handle_stop(signum=None, frame=None):
        LOG.info("stop requested")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    LOG.info("run_dir=%s", output_dir)
    LOG.info("cameras=%d", len(cameras))
    for cid, src in cameras:
        LOG.info("camera=%s source=%s", cid, src)

    preview_writer.start()
    clip_writer.start()
    stage3_worker.start()
    for w in workers:
        w.start()

    try:
        while not stop_event.is_set():
            dead = [w.name for w in workers if not w.is_alive()]
            if dead:
                LOG.warning("dead camera workers detected: %s", dead)
            time.sleep(2.0)
    finally:
        stop_event.set()
        for w in workers:
            w.join(timeout=5.0)

        clip_queue.join()
        stage3_queue.join()

        clip_writer.join(timeout=10.0)
        stage3_worker.join(timeout=10.0)
        preview_writer.join(timeout=5.0)

        incident_aggregator.close_all()
        report_hub.close()

        LOG.info("shutdown complete")
        LOG.info("reports: %s", output_dir)


if __name__ == "__main__":
    main()