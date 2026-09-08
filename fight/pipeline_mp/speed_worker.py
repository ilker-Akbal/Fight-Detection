"""Speed consumes CameraIngest frames; only the shared vehicle service owns YOLO."""
from __future__ import annotations

import queue
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from contextlib import ExitStack, nullcontext

from fight.pipeline.incident_outbox import IncidentOutboxEnvelope, append_envelope_durable, utc_iso_from_epoch
from fight.pipeline_mp.common import is_file_source, configure_process_runtime
from fight.pipeline_mp.generation import is_current_generation
from fight.pipeline_mp.health import HealthEmitter
from fight.pipeline_mp.messages import CameraFrame, CameraIngestSignal
from fight.pipeline_mp.scheduling import admit, AdmissionShed, AdmissionStopped, live_request_stale


@dataclass
class VehicleRequest:
    camera_id: str
    slot_id: int
    generation: int
    epoch: int
    request_id: int
    frame: object
    created_monotonic: float
    source_is_file: bool
    max_age_sec: float


@dataclass
class VehicleResult:
    request: VehicleRequest
    detections: list = field(default_factory=list)
    outcome: str = "accepted"


@dataclass(frozen=True)
class SpeedEnvelope(IncidentOutboxEnvelope):
    speed: dict = field(default_factory=dict)


class SpeedGenerationGuard:
    def __init__(self, generations, epochs, slot, generation, epoch):
        self.generations, self.epochs, self.slot = generations, epochs, slot
        self.generation, self.epoch = generation, epoch

    def __call__(self):
        return int(self.generations[self.slot]) == self.generation and int(self.epochs[self.slot]) == self.epoch

    def __enter__(self):
        self.stack = ExitStack()
        for values in (self.generations, self.epochs):
            lock = getattr(values, "get_lock", lambda: None)()
            if lock is not None:
                self.stack.enter_context(lock)
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


def speed_config(config, camera):
    from HizTespiti.speed_mp.config import SpeedMpCamera, SpeedMpConfig
    from HizTespiti.speed_mp.process_camera import _patch_cfg
    from HizTespiti.speed.src.speed_config import load_config
    values = dict(config.get("speed") or {})
    cam = SpeedMpCamera(camera_id=camera["camera_id"], name=camera.get("name", ""),
                        source=camera["source"], description="", faculty="",
                        **{key: camera.get("speed_config", {}).get(key, default) for key, default in {
                            "speed_limit_kmh": 50.0, "tolerance_kmh": 10.0,
                            "calibration_path": "", "roi_enabled": False, "roi_polygon": [],
                            "save_snapshot": True, "save_clip": True}.items()})
    mp_config = SpeedMpConfig(run_name=str(config.get("run_id", "")), output_dir=config["output_dir"],
                             base_config=values.get("base_config", "HizTespiti/speed/configs/speed.yaml"),
                             yolo_weights=values.get("yolo_weights", "yolo11s.pt"), cameras=[],
                             **{key: values.get(key, {}) for key in ("runtime", "motion", "yolo", "tracker", "speed", "evidence")})
    return _patch_cfg(load_config(mp_config.base_config), mp_config, cam, Path(config["output_dir"])), cam


def vehicle_service_main(config, requests, results, stop, generations, epochs, health_queue=None, detector_factory=None):
    configure_process_runtime(cv2_threads=1, enable_cuda_tuning=False)
    health = HealthEmitter(health_queue, component="vehicle", component_type="shared_worker")
    health.emit("process_started", force=True)
    detector = None
    count = 0
    while not stop.is_set():
        try:
            request = requests.get(timeout=0.25)
        except queue.Empty:
            health.heartbeat(progress=count)
            continue
        try:
            if request is None:
                return
            if not is_current_generation(request, generations) or request.epoch != epochs[request.slot_id]:
                requests.observe(request.slot_id, "stale_generation")
                continue
            health.emit("request_received", force=True, progress=count + 1)
            outcome, detections = "accepted", []
            try:
                if live_request_stale(request):
                    outcome = "stale"
                    requests.observe(request.slot_id, "dropped_live")
                else:
                    if detector is None:
                        if detector_factory is None:
                            from HizTespiti.yolo.src.vehicle_detector import VehicleDetector
                            detector_factory = VehicleDetector
                        cfg, _ = speed_config(config, {"camera_id": "shared", "source": "0"})
                        detector = detector_factory(cfg.yolo)
                    detections = detector.detect(request.frame)
            except Exception:
                outcome = "inference_failed"  # No source/config exception text in status.
            count += 1
            health.emit("inference_completed", force=True, progress=count)
            # Do not send frame pixels back through the result queue.
            request.frame = None
            result = VehicleResult(request, detections, outcome)
            while (not stop.is_set() and is_current_generation(request, generations)
                   and request.epoch == epochs[request.slot_id]):
                try:
                    results[request.slot_id].put(result, timeout=0.1)
                    break
                except queue.Full:
                    health.heartbeat(progress=count)
        finally:
            requests.task_done()


class VehicleClient:
    def __init__(self, requests, results, stop, health, camera, slot, generation, epoch, runtime):
        self.requests, self.results, self.stop, self.health = requests, results, stop, health
        self.camera, self.slot, self.generation, self.epoch = camera, slot, generation, epoch
        self.max_age = float(runtime.get("live_inference_max_age_sec", 2))
        self.watchdog_enabled = bool(runtime.get("health_enabled", True))
        self.fail_after = max(float(runtime.get("inference_stall_fail_sec", 120)),
                              float(runtime.get("health_startup_grace_sec", 120)))

    def detect(self, frame, envelope):
        request = VehicleRequest(self.camera["camera_id"], self.slot, self.generation, self.epoch,
                                 envelope.frame_seq, frame, envelope.captured_monotonic,
                                 is_file_source(self.camera["source"]), self.max_age)
        admit(self.requests, request, self.stop, timeout=0.1, ordered=request.source_is_file,
              health=self.health, stage="vehicle")
        started = time.monotonic()
        while not self.stop.is_set():
            self.health.emit("capacity_wait", detail="vehicle")
            try:
                result = self.results.get(timeout=0.25)
            except queue.Empty:
                if not self.watchdog_enabled and time.monotonic() - started >= self.fail_after:
                    raise RuntimeError("vehicle_inference_stall")
                continue
            identity = result.request
            if (identity.generation, identity.epoch, identity.request_id) != (self.generation, self.epoch, request.request_id):
                continue
            if result.outcome == "stale" or live_request_stale(request):
                raise AdmissionShed("stale vehicle inference")
            if result.outcome != "accepted":
                raise RuntimeError("vehicle_inference_failed")
            return result.detections
        raise AdmissionStopped("Speed consumer stopping")


def persist_speed_event(config, camera, event, generation, epoch, valid=lambda: True):
    if not valid():
        raise AdmissionStopped("stale speed generation")
    event_id = str(uuid.uuid4())
    output = Path(config["output_dir"])
    spool_root = output.parent.parent if output.parent.name == "pipeline_runs" else output.parent
    outbox = config.get("runtime", {}).get("incident_outbox_path") or spool_root / "runtime_spool" / "incidents_outbox.jsonl"
    timestamp = float(event.created_at)
    envelope = SpeedEnvelope.create(event_id=event_id, run_id=str(config["run_id"]),
        external_incident_id=f"speed-{camera['camera_id']}-{generation}-{epoch}-{event_id}",
        camera_id=camera["camera_id"], incident_type="SPEED", detected_at=utc_iso_from_epoch(timestamp),
        finalized_at=utc_iso_from_epoch(time.time()), label="speeding", decision_score=1.0,
        max_score=1.0, mean_score=1.0, confidence=1.0, part_count=1,
        evidence_path=event.clip_path or event.snapshot_path or "",
        speed={**asdict(event), "generation": generation, "consumer_epoch": epoch})
    # Linearize publication against camera removal and consumer epoch changes.
    with valid if hasattr(valid, "__enter__") else nullcontext():
        if not valid():
            raise AdmissionStopped("stale speed generation")
        append_envelope_durable(outbox, envelope)


class SpeedProcessor:
    def __init__(self, config, camera, first_frame, client, generation, epoch, valid):
        from HizTespiti.speed_mp.process_camera import _override_calibration
        from HizTespiti.speed.src.calibration_loader import load_calibration
        from HizTespiti.speed.src.evidence_writer import EvidenceWriter, FrameBuffer
        from HizTespiti.speed.src.speed_estimator import SpeedEstimator
        from HizTespiti.speed.src.violation_decider import ViolationDecider
        from HizTespiti.speed.src.visualizer import SpeedVisualizer
        from HizTespiti.motion.src.roi_mask import RoiMask
        from HizTespiti.motion.src.bg_subtractor import BackgroundMotionDetector
        from HizTespiti.motion.src.motion_gate import MotionGate
        from HizTespiti.yolo.src.simple_tracker import SimpleIoUTracker
        self.cfg, cam = speed_config(config, camera)
        self.calibration = _override_calibration(load_calibration(self.cfg.calibration.path), cam)
        if not self.calibration.ready:
            raise RuntimeError("speed_calibration_not_ready")
        cfg, cal = self.cfg, self.calibration
        self.fps = max(float(first_frame.source_fps or 25), 1)
        self.client = client
        self.roi = RoiMask(True, cal.road_roi_polygon) if cal.road_roi_enabled and cal.road_roi_polygon else RoiMask(cfg.roi.enabled, cfg.roi.polygon)
        self.motion = BackgroundMotionDetector(cfg.motion) if cfg.motion.enabled else None
        self.gate = MotionGate(cfg.motion) if cfg.motion.enabled else None
        self.tracker = SimpleIoUTracker(cfg.tracker.iou_threshold, cfg.tracker.max_age, cfg.tracker.min_hits)
        self.estimator = SpeedEstimator(cfg.speed, cal, self.fps)
        self.decider = ViolationDecider(cal.speed_limit_kmh, cal.tolerance_kmh, cfg.speed.confirm_frames, cfg.speed.cooldown_sec)
        self.visualizer = SpeedVisualizer()
        self.buffer = FrameBuffer(max(30, int((cfg.evidence.clip_pre_sec + cfg.evidence.clip_post_sec + 2) * self.fps)))
        evidence_dir = Path(config["output_dir"]) / "incidents" / "speed" / camera["camera_id"] / f"{generation}-{epoch}"
        self.writer = EvidenceWriter(evidence_dir, camera["camera_id"], cfg.evidence, self.fps,
            persist=lambda event: persist_speed_event(config, camera, event, generation, epoch, valid))
        self.tracks = []

    def process(self, envelope):
        from HizTespiti.speed.src.utils import resize_keep_aspect
        from HizTespiti.speed_mp.process_camera import _draw_calibration_overlay
        cfg, cal = self.cfg, self.calibration
        index = envelope.frame_seq  # Never compress time when live frames are shed.
        frame = resize_keep_aspect(envelope.frame, cfg.runtime.resize_width)
        gate_result = None
        if self.motion is not None:
            motion = self.motion.detect(frame, self.roi.get_mask(frame.shape))
            gate_result = self.gate.update(index, motion["motion_score"], len(motion["boxes"]))
        active = gate_result is None or gate_result.active
        ran = active and index % cfg.yolo.stride == 0
        if ran:
            self.tracks = self.tracker.update(self.client.detect(frame, envelope), index)
        else:
            self.tracks = self.tracker.active_tracks() if active else []
        ids = {track.track_id for track in self.tracks}
        self.decider.cleanup(ids)
        self.estimator.cleanup(ids)
        speeds = {track.track_id: self.estimator.estimate(track) for track in self.tracks}
        decisions = {key: self.decider.update(value) for key, value in speeds.items()}
        vis = self.visualizer.draw(frame=frame, tracks=self.tracks, speed_results=speeds,
            decisions=decisions, motion_gate=gate_result, frame_idx=index, camera_id=cfg.camera.camera_id,
            yolo_ran=ran, speed_limit_kmh=cal.speed_limit_kmh,
            threshold_kmh=cal.speed_limit_kmh + cal.tolerance_kmh, meter_per_pixel=cal.meter_per_pixel)
        vis = _draw_calibration_overlay(self.roi.draw(vis), cal)
        self.buffer.add(index, vis)
        for track in self.tracks:
            decision = decisions[track.track_id]
            if decision.should_report and decision.speed_kmh is not None:
                self.writer.save_event(index, index / self.fps, vis, track, decision.speed_kmh,
                    cal.speed_limit_kmh, cal.tolerance_kmh, decision.threshold_kmh, self.buffer)


def speed_process_main(config, camera, frames, requests, results, stop, generation, slot,
                       epochs, epoch, generations, health_queue=None, processor_factory=SpeedProcessor):
    configure_process_runtime(cv2_threads=1, enable_cuda_tuning=False)
    health = HealthEmitter(health_queue, component="speed_worker", component_type="camera",
                           camera_id=camera["camera_id"], slot_id=slot, generation=generation, consumer_epoch=epoch)
    health.emit("process_started", force=True)
    client = VehicleClient(requests, results, stop, health, camera, slot, generation, epoch, config.get("runtime", {}))
    processor = None
    progress = 0
    dropped = 0
    last_processed = 0.0
    valid = SpeedGenerationGuard(generations, epochs, slot, generation, epoch)
    try:
        while not stop.is_set() and valid():
            health.heartbeat(progress=progress)
            try:
                frame = frames.get(timeout=0.25)
            except queue.Empty:
                continue
            if not isinstance(frame, (CameraFrame, CameraIngestSignal)) or frame.generation != generation:
                continue
            if isinstance(frame, CameraIngestSignal):
                if frame.detail == "eof":
                    health.emit("eof", force=True, progress=progress)
                    return
                if frame.detail == "fatal_error":
                    raise RuntimeError("speed_source_failed")
                continue
            if not isinstance(frame, CameraFrame):
                continue
            if (not is_file_source(camera["source"]) and client.max_age > 0
                    and time.perf_counter() - frame.captured_monotonic >= client.max_age):
                health.emit("live_shed", progress=progress)
                dropped += 1
                health.heartbeat(progress=progress, dropped=dropped)
                continue
            if processor is None:
                processor = processor_factory(config, camera, frame, client, generation, epoch, valid)
            max_fps = getattr(getattr(getattr(processor, "cfg", None), "runtime", None), "max_fps", 0)
            if max_fps > 0:
                remaining = 1.0 / max_fps - (time.monotonic() - last_processed)
                while remaining > 0 and not stop.is_set():
                    health.heartbeat(progress=progress, dropped=dropped)
                    stop.wait(min(0.25, remaining))
                    remaining = 1.0 / max_fps - (time.monotonic() - last_processed)
                if stop.is_set():
                    return
            last_processed = time.monotonic()
            try:
                processor.process(frame)
            except AdmissionShed:
                dropped += 1
                health.emit("live_shed", progress=progress, dropped=dropped)
                continue
            progress = frame.frame_seq
            health.emit("frame_consumed", progress=progress)
    except AdmissionStopped:
        return
    except Exception:
        health.emit("process_error", force=True, detail="speed_consumer_failed")
        raise
