from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import cv2
import yaml

from fight.pipeline.adapters import MotionAdapter, YoloAdapter
from fight.pipeline.clip_buffer import save_clip_mp4
from fight.pipeline.pair_selector import LivePairRoiController
from fight.pipeline.person_stabilizer import TemporalPersonStabilizer
from fight.pipeline.utils import crop_from_box, open_source, sanitize_box, box_iou
from fight.pipeline_mp.common import (
    MpPaths,
    configure_process_runtime,
    is_file_source,
    now_str,
    ts_to_str,
)
from fight.pipeline_mp.messages import ActiveEvent, ReportMessage, Stage3Job
from fight.pose.src.pose_adapter import PoseAdapter
from fight.pose.src.pose_gate import PoseGate


def _report(report_queue, kind: str, row: dict, block: bool = False) -> None:
    try:
        if block:
            report_queue.put(ReportMessage(kind=kind, row=row), timeout=1.0)
        else:
            report_queue.put_nowait(ReportMessage(kind=kind, row=row))
    except Exception:
        pass


def _load_pose_temporal(cfg_path: str) -> dict:
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        t = cfg.get("temporal", {})
        return t if isinstance(t, dict) else {}
    except Exception:
        return {}


def _count_persons_in_roi(persons, roi_box, min_iou=0.08, min_center_inside=True) -> int:
    if roi_box is None:
        return 0

    rx1, ry1, rx2, ry2 = roi_box
    cnt = 0

    for _, box in persons:
        bx1, by1, bx2, by2 = box
        cx = 0.5 * (bx1 + bx2)
        cy = 0.5 * (by1 + by2)

        iou = box_iou(box, roi_box)
        center_inside = (rx1 <= cx <= rx2) and (ry1 <= cy <= ry2)

        if iou >= min_iou or (min_center_inside and center_inside):
            cnt += 1

    return cnt


def _render_preview(
    frame_bgr,
    camera_id: str,
    stage: str,
    detail: str,
    persons: int,
    pose_score: float,
    pair_ok: int,
    event_active: int,
    pair_score: float = 0.0,
    roi_ok: int = 0,
    clip_len: int = 0,
):
    try:
        vis = frame_bgr.copy()

        overlay = [
            f"cam: {camera_id}",
            f"stage: {stage}",
            f"detail: {detail}",
            f"persons: {persons}",
            f"pair_ok: {pair_ok}",
            f"pair_score: {pair_score:.3f}",
            f"roi_ok: {roi_ok}",
            f"pose_score: {pose_score:.3f}",
            f"event_active: {event_active}",
            f"clip_len: {clip_len}",
        ]

        y = 24
        for txt in overlay:
            cv2.putText(
                vis,
                txt,
                (12, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.62,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )
            y += 25

        return vis
    except Exception:
        return frame_bgr


def _write_preview_atomic(path: Path, frame_bgr, jpeg_quality: int = 75) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(
            ".jpg",
            frame_bgr,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
        )
        if not ok:
            return

        tmp = str(path) + ".tmp.jpg"
        with open(tmp, "wb") as f:
            f.write(buf.tobytes())

        Path(tmp).replace(path)
    except Exception:
        pass


def _safe_capture_fps(cap, default_fps: float = 16.0) -> float:
    """
    Kaynak FPS değerini güvenli şekilde okur.

    Dosya kaynaklarında temp/evidence clip mutlaka kaynak FPS ile yazılmalı.
    Aksi halde 25 FPS video 16 FPS olarak kaydedilirse 33 sn video 52 sn gibi görünür.
    """
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    except Exception:
        fps = 0.0

    if fps < 1.0 or fps > 120.0:
        return float(default_fps)

    return float(fps)


class CameraProcessRunner:
    """
    Kamera başına çalışan process runner.

    Üretim mantığı:
    - Motion/Yolo/Pair/Pose katmanları Stage3 için ROI/event üretir.
    - Pose, Stage3 öncesi kesin kapı değildir.
    - Pair ROI varsa ve stabil/hold mantığıyla geçerli crop üretebiliyorsak event açılır.
    - Kavgada pose ve pair kısa süre kaybolabilir; bu yüzden ROI/crop kısa süre hold edilir.
    - Son karar X3D-M + incident aggregation tarafındadır.
    """

    def __init__(self, config: dict, camera: dict, stage3_queue, report_queue, stop_event):
        self.config = config
        self.camera = camera
        self.stage3_queue = stage3_queue
        self.report_queue = report_queue
        self.stop_event = stop_event

        self.camera_id = str(camera["camera_id"])
        self.source = str(camera["source"])
        self.source_is_file = is_file_source(self.source)

        self.models = config.get("models", {})
        self.runtime = config.get("runtime", {})
        self.paths = MpPaths.from_output_dir(config["output_dir"])
        self.paths.mkdirs()

        self.args = SimpleNamespace(**self.runtime)
        self.args.output_dir = str(self.paths.output_dir)

        self.motion = MotionAdapter(self.models["motion_config"])
        self.yolo = YoloAdapter(self.models["yolo_config"], self.models["yolo_weights"])
        self.pose = PoseAdapter(self.models["pose_config"]) if bool(self.runtime.get("use_pose", True)) else None

        temporal_cfg = _load_pose_temporal(self.models["pose_config"])
        self.pose_gate = PoseGate(
            window_size=int(temporal_cfg.get("window_size", 6)),
            need_positive=int(temporal_cfg.get("need_positive", 2)),
            min_mean_score=float(temporal_cfg.get("min_mean_score", 0.30)),
            peak_score_thr=float(temporal_cfg.get("peak_score_thr", 0.54)),
            min_consecutive=int(temporal_cfg.get("min_consecutive", 2)),
        )

        self.person_stabilizer = TemporalPersonStabilizer(
            max_age=int(self.runtime.get("person_track_max_age", 8)),
            min_hits=int(self.runtime.get("person_track_min_hits", 1)),
            iou_match_thr=float(self.runtime.get("person_track_iou_match_thr", 0.22)),
            conf_alpha=float(self.runtime.get("person_track_conf_alpha", 0.65)),
            max_tracks=int(self.runtime.get("person_track_max_tracks", 12)),
        )

        self.pair_roi = LivePairRoiController(
            enter_score=float(self.runtime.get("pair_enter_score", 0.58)),
            keep_score=float(self.runtime.get("pair_keep_score", 0.42)),
            keep_frames=int(self.runtime.get("pair_keep_frames", 12)),
            min_hits_to_activate=int(self.runtime.get("pair_min_hits_to_activate", 2)),
            candidate_confirm_frames=int(self.runtime.get("pair_candidate_confirm_frames", 2)),
            pair_identity_iou_thr=float(self.runtime.get("pair_identity_iou_thr", 0.35)),
            switch_margin=float(self.runtime.get("pair_switch_margin", 0.06)),
            roi_expand_x=float(self.runtime.get("pair_roi_expand_x", 1.20)),
            roi_expand_y=float(self.runtime.get("pair_roi_expand_y", 1.14)),
            debug=bool(self.runtime.get("pair_debug", False)),
        )

        self.cap = None
        self.frame_idx = -1
        self.event_counter = 0
        self.active_event: ActiveEvent | None = None
        self.prebuffer = deque(maxlen=int(self.runtime.get("prebuffer_frames", 24)))

        self.default_clip_fps = float(self.runtime.get("clip_fps", 16.0))
        self.capture_fps = self.default_clip_fps
        self.clip_write_fps = self.default_clip_fps
        self.source_timeline_base_ts = time.time()

        # Event parçalanınca prebuffer tekrarını engellemek için tutulur.
        self.last_event_close_frame_idx = -10_000_000
        self.last_event_close_ts = 0.0

        self.last_pose_score = 0.0
        self.last_pose_hit_idx = -10_000
        self.last_pose_ok = False

        self.last_yolo_hit_idx = -10_000
        self.last_pair_hit_idx = -10_000

        self.last_pair_ok_ts = 0.0
        self.last_pose_ok_ts = 0.0
        self.last_pose_trigger_ts = 0.0
        self.pose_missing_since = None

        self.stable_roi_box = None
        self.last_direct_roi_box = None
        self.last_direct_roi_ts = 0.0
        self.last_valid_roi_frame = None
        self.roi_miss_frames = 0
        self.roi_invalid_ctr = 0

        self.two_p_ctr = 0
        self.two_p_miss_ctr = 0

        self.preview_path = self.paths.previews_dir / f"{self.camera_id}.jpg"
        self.last_preview_write_ts = 0.0

    def report_status(self, stage: str, detail: str, extra: dict | None = None) -> None:
        row = {
            "ts": now_str(),
            "camera_id": self.camera_id,
            "ip": self.source,
            "stage": stage,
            "detail": detail,
        }
        if extra:
            row.update(extra)
        _report(self.report_queue, "status", row)

    def open(self) -> None:
        self.close_capture()
        self.cap = open_source(self.source)

        if self.cap is None or not self.cap.isOpened():
            raise RuntimeError(f"Cannot open source: {self.source}")

        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        self.capture_fps = _safe_capture_fps(
            self.cap,
            default_fps=self.default_clip_fps,
        )

        if self.source_is_file:
            self.clip_write_fps = self.capture_fps
        else:
            self.clip_write_fps = self.default_clip_fps

        self.source_timeline_base_ts = time.time()

        self.report_status(
            "camera",
            "opened",
            {
                "source_is_file": int(self.source_is_file),
                "capture_fps": round(float(self.capture_fps), 3),
                "clip_write_fps": round(float(self.clip_write_fps), 3),
            },
        )

    def close_capture(self) -> None:
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None

    def timestamp_for_frame_idx(self, frame_idx: int) -> float:
        """
        Dosya kaynaklarında frame index -> video timeline timestamp dönüşümü.

        Bu gerçek duvar saati değildir; video zaman çizelgesidir.
        Event start/end ve incident aggregation gerçek video zamanıyla tutarlı kalır.
        """
        if self.source_is_file and self.capture_fps > 0:
            safe_frame_idx = max(0, int(frame_idx))
            return float(self.source_timeline_base_ts) + (
                safe_frame_idx / float(self.capture_fps)
            )

        return time.time()

    def event_timestamp(self) -> float:
        return self.timestamp_for_frame_idx(self.frame_idx)

    def reset_temporal(self, clear_prebuffer: bool = True) -> None:
        self.person_stabilizer.reset()
        self.pair_roi.reset()
        self.pose_gate.reset()

        self.last_pose_score = 0.0
        self.last_pose_hit_idx = -10_000
        self.last_pose_ok = False

        self.last_yolo_hit_idx = -10_000
        self.last_pair_hit_idx = -10_000

        self.last_pair_ok_ts = 0.0
        self.last_pose_ok_ts = 0.0
        self.last_pose_trigger_ts = 0.0
        self.pose_missing_since = None

        self.stable_roi_box = None
        self.last_direct_roi_box = None
        self.last_direct_roi_ts = 0.0
        self.last_valid_roi_frame = None
        self.roi_miss_frames = 0
        self.roi_invalid_ctr = 0

        self.two_p_ctr = 0
        self.two_p_miss_ctr = 0

        if clear_prebuffer:
            self.prebuffer.clear()

    def detect_persons(self, frame_bgr):
        dets = self.yolo.detect_persons(frame_bgr)
        person_conf = float(self.runtime.get("person_conf", 0.25))
        dets = [(c, box) for (c, box) in dets if float(c) >= person_conf]
        dets.sort(key=lambda x: x[0], reverse=True)
        return dets

    def pose_check(self, roi_bgr):
        if self.pose is None:
            return None

        raw = self.pose.evaluate(roi_bgr)
        dec = self.pose_gate.update(raw.score, raw.ok)

        raw.ok = dec.pose_ok
        raw.hist_positive = dec.hist_positive
        return raw

    def maybe_write_preview(
        self,
        frame_bgr,
        stage: str,
        detail: str,
        persons: int,
        pose_score: float,
        pair_ok: int,
        event_active: int,
        pair_score: float = 0.0,
        roi_ok: int = 0,
    ) -> None:
        preview_every = max(1, int(self.runtime.get("preview_every_frames", 5)))
        interval = float(self.runtime.get("preview_write_interval_sec", 0.25))
        quality = int(self.runtime.get("preview_jpeg_quality", 75))

        if self.frame_idx % preview_every != 0:
            return

        now_ts = time.time()
        if now_ts - self.last_preview_write_ts < interval:
            return

        clip_len = 0 if self.active_event is None else len(self.active_event.frames)

        vis = _render_preview(
            frame_bgr,
            camera_id=self.camera_id,
            stage=stage,
            detail=detail,
            persons=persons,
            pose_score=pose_score,
            pair_ok=pair_ok,
            event_active=event_active,
            pair_score=pair_score,
            roi_ok=roi_ok,
            clip_len=clip_len,
        )
        _write_preview_atomic(self.preview_path, vis, quality)
        self.last_preview_write_ts = now_ts

    def new_event(self, now_ts: float, seed_frames: list, pose_score: float, start_reason: str) -> None:
        self.event_counter += 1
        event_id = f"{self.camera_id}_{self.event_counter:06d}"

        prebuffer_frames = int(self.runtime.get("prebuffer_frames", 24))
        raw_seed_frames = list(seed_frames or [])

        # process_frame içinde current frame önce prebuffer'a giriyor,
        # sonra new_event çağrılıyor, sonra append_event current frame'i tekrar ekliyor.
        # Bu yüzden seed'in son elemanını çıkarıyoruz.
        if raw_seed_frames:
            raw_seed_frames = raw_seed_frames[:-1]

        frames_since_last_close = self.frame_idx - int(self.last_event_close_frame_idx)

        # Event max_event_frames yüzünden parçalandığında yeni segment önceki segmentin
        # son frame'lerini tekrar prebuffer olarak almasın.
        use_prebuffer = frames_since_last_close > prebuffer_frames

        if use_prebuffer:
            frames = [
                f.copy()
                for f in raw_seed_frames[-prebuffer_frames:]
                if f is not None
            ]
        else:
            frames = []

        if frames:
            first_frame_idx = max(0, self.frame_idx - len(frames))
            event_start_ts = self.timestamp_for_frame_idx(first_frame_idx)
        else:
            event_start_ts = now_ts

        self.active_event = ActiveEvent(
            event_id=event_id,
            camera_id=self.camera_id,
            source=self.source,
            start_ts=event_start_ts,
            last_ts=now_ts,
            last_positive_frame_idx=self.frame_idx,
            frames=frames,
            pose_scores=[float(pose_score)],
            positive_hits=1,
        )

        self.report_status(
            "event",
            "started",
            {
                "event_id": event_id,
                "latest_event_status": "event_started",
                "start_reason": start_reason,
                "pose_score": round(float(pose_score), 6),
                "event_active": 1,
                "prebuffer_frames": len(frames),
                "prebuffer_used": int(use_prebuffer),
                "frames_since_last_close": int(frames_since_last_close),
                "event_start_ts": ts_to_str(event_start_ts),
                "now_ts": ts_to_str(now_ts),
                "capture_fps": round(float(self.capture_fps), 3),
                "clip_write_fps": round(float(self.clip_write_fps), 3),
            },
        )

    def append_event(self, roi_bgr, now_ts: float, pose_score: float, positive_hit: bool) -> None:
        if self.active_event is None:
            return

        ev = self.active_event
        ev.last_ts = now_ts
        ev.frames.append(roi_bgr.copy())
        ev.pose_scores.append(float(pose_score))

        if positive_hit:
            ev.positive_hits += 1
            ev.last_positive_frame_idx = self.frame_idx

        max_frames = int(self.runtime.get("max_event_frames", 128))
        if max_frames > 0 and len(ev.frames) >= max_frames:
            self.close_event("max_event_frames")

    def save_clip(self, frames: list, clip_path: Path) -> None:
        save_clip_mp4(
            frames,
            str(clip_path),
            fps=float(self.clip_write_fps),
        )

    def _should_queue_stage3(
        self,
        *,
        reason: str,
        frame_count: int,
        duration: float,
        positive_hits: int,
        pose_mean: float,
        pose_max: float,
    ) -> tuple[bool, str]:
        """
        Stage3 kuyruğuna gönderme kararı.

        Pose skoru burada drop sebebi değildir.
        pair_roi_missing de drop sebebi değildir; kavga sırasında pair/pose kısa süre kaybolabilir.
        Sadece gerçekten crop alınamayan veya çok kısa eventleri düşürüyoruz.
        """

        min_queue_frames = int(self.runtime.get("min_queue_frames", 16))
        min_duration_sec = float(self.runtime.get("stage3_event_min_duration_sec", 0.25))

        raw_drop_reasons = str(
            self.runtime.get(
                "stage3_drop_close_reasons",
                "roi_crop_failed",
            )
        )
        drop_reasons = {x.strip() for x in raw_drop_reasons.split(",") if x.strip()}

        drop_reasons.discard("pair_roi_missing")
        drop_reasons.discard("not_enough_persons")
        drop_reasons.discard("pose_grace_expired")
        drop_reasons.discard("pose_pair_grace_expired")
        drop_reasons.discard("motion_missing")

        quality_reasons: list[str] = []

        if str(reason) in drop_reasons:
            quality_reasons.append(f"bad_close_reason_{reason}")

        if int(frame_count) < min_queue_frames:
            quality_reasons.append(f"short_frames_{frame_count}<{min_queue_frames}")

        if float(duration) < min_duration_sec:
            quality_reasons.append(f"short_duration_{duration:.2f}<{min_duration_sec:.2f}")

        if quality_reasons:
            return False, "low_event_quality:" + ",".join(quality_reasons)

        return True, "stage3_queue"

    def close_event(self, reason: str) -> None:
        ev = self.active_event
        self.active_event = None

        if ev is None:
            return

        event_end_ts = ev.last_ts
        duration = max(0.0, float(event_end_ts) - float(ev.start_ts))

        self.last_event_close_frame_idx = int(self.frame_idx)
        self.last_event_close_ts = float(event_end_ts)

        pose_max = max(ev.pose_scores) if ev.pose_scores else 0.0
        pose_mean = sum(ev.pose_scores) / max(1, len(ev.pose_scores)) if ev.pose_scores else 0.0

        clip_name = (
            f"cam_{self.camera_id}__evt_{ev.event_id}__"
            f"{datetime.fromtimestamp(ev.start_ts).strftime('%Y%m%d_%H%M%S_%f')[:-3]}.mp4"
        )
        clip_path = self.paths.temp_segments_dir / clip_name

        if ev.frames:
            try:
                self.save_clip(ev.frames, clip_path)
            except Exception as exc:
                self.report_status(
                    "clip",
                    "save_failed",
                    {
                        "event_id": ev.event_id,
                        "clip_path": str(clip_path),
                        "error": str(exc),
                    },
                )

        queue_status = "not_requested"
        queue_reason = reason

        if bool(self.runtime.get("use_stage3", True)):
            ok_to_queue, q_reason = self._should_queue_stage3(
                reason=reason,
                frame_count=len(ev.frames),
                duration=duration,
                positive_hits=int(ev.positive_hits),
                pose_mean=float(pose_mean),
                pose_max=float(pose_max),
            )

            if not ok_to_queue:
                queue_status = "dropped"
                queue_reason = q_reason
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
                    positive_hits=int(ev.positive_hits),
                    frame_count=int(len(ev.frames)),
                )

                try:
                    self.stage3_queue.put(
                        job,
                        timeout=float(self.runtime.get("stage3_enqueue_timeout_sec", 0.35)),
                    )
                    queue_status = "queued"
                    queue_reason = "stage3_queue"
                except Exception:
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
            "duration_sec": round(float(duration), 3),
            "status": reason,
            "pose_score_max": round(float(pose_max), 6),
            "pose_score_mean": round(float(pose_mean), 6),
            "positive_hits": int(ev.positive_hits),
            "clip_path": str(clip_path),
            "queue_status": queue_status,
            "queue_reason": queue_reason,
            "frames": int(len(ev.frames)),
            "capture_fps": round(float(self.capture_fps), 3),
            "clip_write_fps": round(float(self.clip_write_fps), 3),
            "source_is_file": int(self.source_is_file),
            "closed_at": now_str(),
        }

        _report(self.report_queue, "event", row, block=True)

        # Kapatılan event'in son frame'leri yeni event'in başına tekrar prebuffer olarak girmesin.
        # Aksi halde final incident clip içinde aynı sahneler tekrar eder.
        if reason in {
            "max_event_frames",
            "not_enough_persons",
            "motion_missing",
            "pair_roi_missing",
            "pose_pair_grace_expired",
            "source_eof",
            "shutdown",
        }:
            self.prebuffer.clear()

        self.report_status(
            "event_close",
            reason,
            {
                "event_id": ev.event_id,
                "latest_event_status": reason,
                "queue_status": queue_status,
                "queue_reason": queue_reason,
                "frames": int(len(ev.frames)),
                "duration_sec": round(float(duration), 3),
                "pose_score": round(float(pose_mean), 6),
                "pose_score_max": round(float(pose_max), 6),
                "positive_hits": int(ev.positive_hits),
                "clip_path": str(clip_path),
                "capture_fps": round(float(self.capture_fps), 3),
                "clip_write_fps": round(float(self.clip_write_fps), 3),
                "source_is_file": int(self.source_is_file),
                "prebuffer_cleared": 1,
                "event_active": 0,
            },
        )

    def _close_active_if_grace_expired(self, reason: str) -> bool:
        if self.active_event is None:
            return False

        gap = self.frame_idx - int(self.active_event.last_positive_frame_idx)
        grace = int(self.runtime.get("event_close_grace_frames", 12))

        if gap >= grace:
            self.close_event(reason)
            return True

        return False

    def _update_roi_state(self, *, active: bool, persons: list, base_frame, pair_state: dict, now_ts: float):
        pair_score = float(pair_state.get("pair_score", 0.0))
        raw_pair_ok = bool(pair_state.get("pair_ok", 0))
        roi_hint_box = pair_state.get("roi_box") if int(pair_state.get("roi_ok", 0)) else None

        pair_hold_sec = float(self.runtime.get("pair_hold_sec", 1.0))
        direct_roi_hold_sec = float(self.runtime.get("direct_roi_hold_sec", 1.0))
        roi_invalid_drop_frames = int(self.runtime.get("roi_invalid_drop_frames", 4))
        roi_person_min_count = int(self.runtime.get("roi_person_min_count", 2))

        if active and raw_pair_ok:
            self.last_pair_ok_ts = now_ts
            self.last_pair_hit_idx = self.frame_idx
            self.two_p_ctr += 1
            self.two_p_miss_ctr = 0
        else:
            if self.two_p_ctr > 0:
                self.two_p_miss_ctr += 1
                two_p_grace_frames = int(self.runtime.get("two_p_grace_frames", 20))
                if self.two_p_miss_ctr >= two_p_grace_frames:
                    self.two_p_ctr = 0
                    self.two_p_miss_ctr = 0

        effective_pair_ok = raw_pair_ok
        if (
            not effective_pair_ok
            and self.last_pair_ok_ts > 0
            and (now_ts - self.last_pair_ok_ts) <= pair_hold_sec
        ):
            effective_pair_ok = True

        if active and roi_hint_box is not None:
            self.stable_roi_box = sanitize_box(roi_hint_box, base_frame.shape)
            self.last_direct_roi_box = self.stable_roi_box
            self.last_direct_roi_ts = now_ts
        else:
            if (
                self.last_direct_roi_box is not None
                and (now_ts - self.last_direct_roi_ts) <= direct_roi_hold_sec
            ):
                self.stable_roi_box = self.last_direct_roi_box
            else:
                self.stable_roi_box = None
                self.last_direct_roi_box = None

        roi_person_count = _count_persons_in_roi(
            persons,
            self.stable_roi_box,
            min_iou=float(self.runtime.get("roi_person_min_iou", 0.08)),
            min_center_inside=True,
        )

        if self.stable_roi_box is not None:
            if raw_pair_ok or roi_person_count >= roi_person_min_count:
                self.roi_invalid_ctr = 0
            else:
                self.roi_invalid_ctr += 1
        else:
            self.roi_invalid_ctr += 1

        if self.roi_invalid_ctr >= roi_invalid_drop_frames:
            self.stable_roi_box = None
            self.last_direct_roi_box = None

        if self.stable_roi_box is not None:
            self.roi_miss_frames = 0
        else:
            self.roi_miss_frames += 1

        view_s3 = None
        live_crop = False

        if self.stable_roi_box is not None:
            self.stable_roi_box = sanitize_box(self.stable_roi_box, base_frame.shape)
            view_s3 = crop_from_box(
                base_frame,
                self.stable_roi_box,
                out_size=int(self.runtime.get("roi_size", 320)),
                pad_value=int(self.runtime.get("roi_pad_value", 114)),
            )
            if view_s3 is not None:
                self.last_valid_roi_frame = view_s3.copy()
                live_crop = bool(effective_pair_ok)

        clip_soft_hold_frames = int(self.runtime.get("clip_soft_hold_frames", 10))

        allow_stale_crop = (
            view_s3 is None
            and self.last_valid_roi_frame is not None
            and self.roi_miss_frames <= clip_soft_hold_frames
            and self.roi_invalid_ctr < roi_invalid_drop_frames
            and (effective_pair_ok or roi_person_count >= 1)
        )

        if allow_stale_crop:
            view_s3 = self.last_valid_roi_frame.copy()
            live_crop = bool(effective_pair_ok)

        roi_available = (self.stable_roi_box is not None or allow_stale_crop) and view_s3 is not None

        return {
            "pair_score": pair_score,
            "raw_pair_ok": raw_pair_ok,
            "effective_pair_ok": bool(effective_pair_ok),
            "roi_person_count": int(roi_person_count),
            "roi_available": bool(roi_available),
            "view_s3": view_s3,
            "live_crop": bool(live_crop),
        }

    def process_frame(self, frame) -> None:
        self.frame_idx += 1
        now_ts = self.event_timestamp()

        persons_count = 0
        pair_ok = 0
        pair_score = 0.0
        pose_score = 0.0

        motion_score, motion_ok, motion_frame, _ = self.motion.step(now_ts, frame)
        if motion_frame is None:
            motion_frame = frame

        if not motion_ok:
            self._close_active_if_grace_expired("motion_missing")

            if self.active_event is None:
                self.reset_temporal(clear_prebuffer=True)

            self.maybe_write_preview(
                frame,
                "motion",
                "no_motion",
                0,
                0.0,
                0,
                int(self.active_event is not None),
                0.0,
                0,
            )

            if self.frame_idx % int(self.runtime.get("status_log_every", 20)) == 0:
                self.report_status(
                    "motion",
                    "no_motion",
                    {
                        "frame_idx": self.frame_idx,
                        "motion_score": round(float(motion_score), 6),
                        "event_active": int(self.active_event is not None),
                    },
                )
            return

        yolo_stride = max(1, int(self.runtime.get("yolo_stride", 2)))
        if self.frame_idx % yolo_stride == 0:
            dets = self.detect_persons(motion_frame)
            stable_persons = self.person_stabilizer.update(dets)
        else:
            stable_persons = self.person_stabilizer.predict_only()

        persons_count = len(stable_persons)

        if persons_count < int(self.runtime.get("min_persons_for_pose", 2)):
            self._close_active_if_grace_expired("not_enough_persons")

            self.maybe_write_preview(
                frame,
                "yolo",
                "not_enough_persons",
                persons_count,
                0.0,
                0,
                int(self.active_event is not None),
                0.0,
                0,
            )

            if self.frame_idx % int(self.runtime.get("status_log_every", 20)) == 0:
                self.report_status(
                    "yolo",
                    "not_enough_persons",
                    {
                        "frame_idx": self.frame_idx,
                        "persons": persons_count,
                        "event_active": int(self.active_event is not None),
                    },
                )
            return

        self.last_yolo_hit_idx = self.frame_idx

        pair_state = self.pair_roi.update(stable_persons, motion_frame.shape)
        pair_ok = int(pair_state.get("pair_ok", 0))
        pair_score = float(pair_state.get("pair_score", 0.0))

        roi_info = self._update_roi_state(
            active=bool(motion_ok),
            persons=stable_persons,
            base_frame=motion_frame,
            pair_state=pair_state,
            now_ts=now_ts,
        )

        view_s3 = roi_info["view_s3"]
        roi_available = bool(roi_info["roi_available"])
        effective_pair_ok = bool(roi_info["effective_pair_ok"])
        roi_person_count = int(roi_info["roi_person_count"])

        if not roi_available or view_s3 is None:
            self._close_active_if_grace_expired("pair_roi_missing")

            self.maybe_write_preview(
                frame,
                "pair",
                "roi_missing",
                persons_count,
                0.0,
                pair_ok,
                int(self.active_event is not None),
                pair_score,
                0,
            )
            return

        self.prebuffer.append(view_s3.copy())

        pose_stride = max(1, int(self.runtime.get("pose_stride", 2)))
        pose_positive = False
        raw_pose_ready = False

        if self.frame_idx % pose_stride == 0:
            pose_out = self.pose_check(view_s3)

            if pose_out is None:
                pose_positive = True
                pose_score = 1.0
            else:
                pose_score = float(getattr(pose_out, "score", 0.0))
                pose_positive = bool(getattr(pose_out, "ok", False))

            self.last_pose_score = pose_score
            self.last_pose_ok = bool(pose_positive)

            if pose_positive:
                self.last_pose_hit_idx = self.frame_idx
                self.last_pose_ok_ts = now_ts
                self.last_pose_trigger_ts = now_ts
                self.pose_missing_since = None
            else:
                if self.pose_missing_since is None:
                    self.pose_missing_since = now_ts
        else:
            pose_score = float(self.last_pose_score)
            pose_hold_sec = float(self.runtime.get("pose_hold_sec", 1.0))
            pose_frame_hold = int(self.runtime.get("pose_hold_frames", 8))

            pose_positive = (
                (self.frame_idx - self.last_pose_hit_idx) <= pose_frame_hold
                or (
                    self.last_pose_ok_ts > 0
                    and (now_ts - self.last_pose_ok_ts) <= pose_hold_sec
                )
            )

        raw_pose_ready = bool(self.last_pose_ok)

        pose_trigger_hold_sec = float(self.runtime.get("pose_trigger_hold_sec", 2.4))
        pose_capture_active = (
            self.pose is None
            or (
                self.last_pose_trigger_ts > 0
                and (now_ts - self.last_pose_trigger_ts) <= pose_trigger_hold_sec
            )
        )

        pair_driven_event_start = bool(self.runtime.get("pair_driven_event_start", True))
        pair_event_start_score = float(self.runtime.get("pair_event_start_score", 0.25))
        pair_event_min_2p_frames = int(self.runtime.get("pair_event_min_2p_frames", 2))

        can_start_by_pair = (
            pair_driven_event_start
            and effective_pair_ok
            and pair_score >= pair_event_start_score
            and self.two_p_ctr >= pair_event_min_2p_frames
            and roi_available
        )

        can_continue_by_roi = (
            self.active_event is not None
            and roi_available
            and (effective_pair_ok or roi_person_count >= 1)
        )

        event_hit = bool(pose_positive or can_start_by_pair or can_continue_by_roi)

        if event_hit:
            if self.active_event is None:
                start_reason = "pose" if pose_positive else "pair_roi"
                self.new_event(now_ts, list(self.prebuffer), pose_score, start_reason=start_reason)

            strong_continuity = bool(pose_positive or can_start_by_pair or effective_pair_ok)
            self.append_event(
                view_s3,
                now_ts,
                pose_score,
                positive_hit=strong_continuity,
            )

        elif self.active_event is not None:
            self.append_event(view_s3, now_ts, pose_score, positive_hit=False)
            self._close_active_if_grace_expired("pose_pair_grace_expired")

        self.maybe_write_preview(
            frame,
            "pose",
            "positive" if pose_positive else "negative",
            persons_count,
            pose_score,
            pair_ok,
            int(self.active_event is not None),
            pair_score,
            int(roi_available),
        )

        if self.frame_idx % int(self.runtime.get("status_log_every", 20)) == 0:
            self.report_status(
                "pose",
                "positive" if pose_positive else "negative",
                {
                    "frame_idx": self.frame_idx,
                    "persons": persons_count,
                    "pair_ok": pair_ok,
                    "pair_score": round(float(pair_score), 6),
                    "effective_pair_ok": int(effective_pair_ok),
                    "two_p_ctr": int(self.two_p_ctr),
                    "roi_available": int(roi_available),
                    "roi_person_count": int(roi_person_count),
                    "roi_invalid_ctr": int(self.roi_invalid_ctr),
                    "roi_miss_frames": int(self.roi_miss_frames),
                    "pose_score": round(float(pose_score), 6),
                    "pose_positive": int(pose_positive),
                    "pose_raw": int(raw_pose_ready),
                    "pose_capture_active": int(pose_capture_active),
                    "event_active": int(self.active_event is not None),
                    "event_frames": 0 if self.active_event is None else len(self.active_event.frames),
                    "motion_score": round(float(motion_score), 6),
                    "capture_fps": round(float(self.capture_fps), 3),
                    "clip_write_fps": round(float(self.clip_write_fps), 3),
                },
            )

    def run_loop(self) -> None:
        reconnect_sec = float(self.runtime.get("reconnect_sec", 2.0))
        file_loop = bool(self.runtime.get("loop_file_sources", False))

        while not self.stop_event.is_set():
            try:
                self.open()

                while not self.stop_event.is_set():
                    ok, frame = self.cap.read()

                    if not ok or frame is None:
                        if self.source_is_file:
                            if self.active_event is not None:
                                self.close_event("source_eof")

                            if file_loop:
                                self.open()
                                continue

                            self.report_status("camera", "eof")
                            return

                        self.report_status("camera", "read_failed")
                        time.sleep(reconnect_sec)
                        break

                    self.process_frame(frame)

            except Exception as exc:
                self.report_status(
                    "camera",
                    "error",
                    {
                        "error": str(exc),
                    },
                )
                time.sleep(reconnect_sec)

            finally:
                self.close_capture()

        if self.active_event is not None:
            self.close_event("shutdown")

        try:
            self.motion.close()
        except Exception:
            pass

        self.report_status("camera", "stopped")


def camera_process_main(config: dict, camera: dict, stage3_queue, report_queue, stop_event) -> None:
    runtime = config.get("runtime", {})

    configure_process_runtime(
        cv2_threads=int(runtime.get("camera_cv2_threads", runtime.get("cv2_threads", 1))),
        enable_cuda_tuning=bool(runtime.get("camera_cuda_tuning", False)),
    )

    runner = CameraProcessRunner(
        config=config,
        camera=camera,
        stage3_queue=stage3_queue,
        report_queue=report_queue,
        stop_event=stop_event,
    )
    runner.run_loop()