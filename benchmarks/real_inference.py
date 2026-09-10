"""Isolated Supervisor-owned file workloads using unchanged detection configuration."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from fight.operations import atomic_json
from fight.runtime_supervisor.core import RuntimeSupervisor, SupervisorConfig
from fight.runtime_supervisor.camera_state import normalize_cameras
from benchmarks.telemetry import SystemSampler, file_identity, psutil, redact
from fight.pipeline_mp.attribution import attribution_summary


def stage_latency_summary(metrics):
    """Export existing distributions verbatim, including worker/batch views."""
    return {key: value for key, value in metrics.items()
            if key.endswith("_ms") or key in
            ("all_requests", "steady_state", "warmup_requests", "batch", "worker_timings")}


@dataclass(frozen=True)
class Thresholds:
    pressure_drop_ratio: float = .01
    saturation_drop_ratio: float = .10
    pressure_rejection_ratio: float = .10
    saturation_rejection_ratio: float = .50
    queue_pressure_ratio: float = .90

    def validate(self):
        if not (0 <= self.pressure_drop_ratio < self.saturation_drop_ratio <= 1
                and 0 <= self.pressure_rejection_ratio < self.saturation_rejection_ratio <= 1
                and 0 < self.queue_pressure_ratio <= 1):
            raise ValueError("invalid classification thresholds")


def classify(*, complete, frames, capacity, system, thresholds=Thresholds(), health_failed=False):
    thresholds.validate()
    accepted = sum(row.get("accepted", 0) for row in capacity.values())
    rejected = sum(row.get("rejected_capacity", 0) for row in capacity.values())
    dropped = sum(row.get("dropped_live", 0) for row in capacity.values())
    # These are admission-attempt ratios, NOT camera/frame loss probabilities.
    # Ordered-file retry attempts legitimately contribute to rejection counts.
    rejection_ratio = rejected / max(1, accepted + rejected)
    drop_ratio = dropped / max(1, accepted + dropped)
    queue_ratio = max((row.get("observed_outstanding_peak", row.get("outstanding", 0)) /
                       max(1, row.get("capacity", 0)) for row in capacity.values()), default=0)
    pressure = rejection_ratio >= thresholds.pressure_rejection_ratio or queue_ratio >= thresholds.queue_pressure_ratio
    if not complete or frames <= 0 or health_failed:
        status = "INCOMPLETE"
    elif drop_ratio >= thresholds.saturation_drop_ratio:
        status = "SATURATED"
    elif rejection_ratio >= thresholds.saturation_rejection_ratio and dropped > 0:
        status = "SATURATED"
    elif pressure or drop_ratio >= thresholds.pressure_drop_ratio:
        status = "PRESSURED"
    else:
        status = "HEALTHY"
    gpu = max((row["utilization_pct"]["mean"] or 0 for row in system.get("gpu", {}).values()), default=0)
    cpu = system.get("cpu_pct", {}).get("mean") or 0
    hint = ("gpu_bound_candidate" if pressure and gpu >= 90 else
            "cpu_bound_candidate" if pressure and cpu >= 90 else "queue_pressure" if pressure else "unknown")
    return {"classification": status, "diagnostic_hint_not_proven": hint, "thresholds": asdict(thresholds),
            "admission_drop_ratio": drop_ratio, "rejection_attempt_ratio": rejection_ratio,
            "observed_queue_ratio_peak": queue_ratio}


def read_json(path, limit=32 * 1024 * 1024):
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def prepare_config(base, *, repo, directory, count, workload, source=None, max_samples=256):
    """Only workload identity, artifact destinations and telemetry are overridden."""
    config = {key: copy.deepcopy(base[key]) for key in ("models", "runtime", "speed") if key in base}
    runtime = config.setdefault("runtime", {})
    if runtime.get("loop_file_sources", False) or runtime.get("camera_ingest_file_fight_policy", "ordered") != "ordered":
        raise ValueError("benchmark requires non-looping ordered files")
    runtime.update(performance_metrics_enabled=True, performance_metrics_max_samples=max_samples,
                   health_enabled=True, stop_run_when_all_file_cameras_done=True,
                   incident_outbox_path=str(directory / "outbox" / "incidents_outbox.jsonl"))
    for key in ("run_id", "desired_camera_state_path", "health_snapshot_path"):
        runtime.pop(key, None)
    # Grow the existing slot reservation only when needed; preserve its usual
    # baseline overhead at smaller counts and expose the actual value in output.
    runtime["dynamic_camera_slot_count"] = max(count, int(runtime.get("dynamic_camera_slot_count", 32)))
    config.update(run_name=directory.name, output_dir=str(directory / "runtime"))
    if "speed" in config:
        config["speed"].update(output_dir=config["output_dir"], run_name=directory.name, cameras=[])
    templates = base.get("cameras", [])
    candidates = [c for c in templates if c.get("use_speed_detection", False)] if workload != "fight" else [
        c for c in templates if c.get("use_fight_detection", True)]
    if not candidates:
        raise ValueError("base config needs a matching camera template (Speed requires its existing calibration)")
    template = candidates[0]
    media = Path(source or template["source"])
    if not media.is_absolute():
        media = repo / media
    if not media.is_file():
        raise ValueError("benchmark source must be an existing local file")
    # Model and Speed paths remain relative to the repository, exactly as in the base.
    sources = directory / "sources"
    sources.mkdir(parents=True)
    cameras = []
    for index in range(count):
        cid = f"bench-{index:04d}"
        target = sources / (cid + media.suffix)
        try:
            os.link(media, target)
        except OSError:
            shutil.copyfile(media, target)
        cameras.append({"camera_id": cid, "name": cid, "source": str(target), "enabled": True,
                        "use_fight_detection": workload != "speed", "use_speed_detection": workload != "fight",
                        "speed_config": copy.deepcopy(template.get("speed_config", {})) if workload != "fight" else {}})
    config["cameras"] = normalize_cameras(cameras)
    # Reject protected endpoints/secrets rather than copying them into runtime logs
    # or altering model configuration. Local model/config paths are expected.
    def check_safe(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if any(word in key.lower() for word in ("password", "secret", "token", "authorization", "credential")):
                    raise ValueError("benchmark configs must not contain credentials")
                check_safe(item)
        elif isinstance(value, list):
            for item in value:
                check_safe(item)
        elif isinstance(value, str) and "://" in value:
            raise ValueError("benchmark configs must not contain remote endpoints")
    check_safe(config)
    return config, file_identity(media)


def required_cameras_complete(config, cameras):
    for camera in config["cameras"]:
        row = cameras[camera["camera_id"]]
        health = row["final_health"]
        if row["frames_decoded"] is None or health.get("lifecycle") == "FAILED" or health.get("health") == "FAILED":
            return False
        if camera["use_fight_detection"] and row["fight_frames_consumed"] is None:
            return False
        if camera["use_speed_detection"] and (health.get("speed", {}).get("failed") or row["speed_progress"] is None):
            return False
    return True


def assert_no_runtime():
    if psutil is None:
        raise ValueError("real mode requires lightweight psutil for process/RAM measurement and runtime exclusion")
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if "python" in (process.info["name"] or "").lower() and any(
                    value == "fight.pipeline_mp.run_multiprocess" for value in process.info["cmdline"] or []):
                raise ValueError("another runtime is active; stop it before benchmarking")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def run_real(args, count, output, repo):
    assert_no_runtime()
    directory = output.path / f"real-{args.workload}-{count}"
    directory.mkdir()
    base = read_json(args.config)
    config, identity = prepare_config(base, repo=repo, directory=directory, count=count,
        workload=args.workload, source=args.source, max_samples=args.max_samples)
    config_path = directory / "benchmark_config.json"
    atomic_json(config_path, config)
    sampler = SystemSampler(args.max_samples, args.gpu_device)
    supervisor = RuntimeSupervisor(SupervisorConfig(repo_root=repo, state_dir=directory / "supervisor",
        allowed_config_dirs=(directory,), auto_restart=False))
    start = time.monotonic()
    deadline_reached = False
    latest_health = {}
    peak_outstanding = {}
    health_failed = False
    recovered = False
    started = {}
    try:
        started = supervisor.start(config_path)
        run_id = started["run_id"]
        print(f"real_inference cameras={count} run_id={run_id}: started", flush=True)
        while True:
            status = supervisor.status()
            elapsed = time.monotonic() - start
            health = read_json(directory / "runtime" / "runtime_health.json")
            if health.get("run_id") == run_id:
                latest_health = health
                health_failed |= health.get("runtime_health") == "FAILED"
                recovered |= any(row.get("restart_count", 0) > 0 or row.get("speed", {}).get("restarts", 0) > 0
                                 for row in [*health.get("workers", {}).values(), *health.get("cameras", {}).values()])
                for name, row in health.get("workers", {}).items():
                    outstanding = row.get("capacity", {}).get("outstanding", 0)
                    peak_outstanding[name] = max(peak_outstanding.get(name, 0), outstanding)
            if elapsed >= args.warmup_sec:
                output.sample(run_id, sampler.sample(elapsed, status.get("runtime_pid")))
            if status.get("runtime_exit_code") is not None or status.get("runtime_state") == "FAILED":
                break
            if elapsed >= args.warmup_sec + args.duration_sec:
                deadline_reached = True
                break
            time.sleep(args.sample_interval_sec)
    finally:
        supervisor.close(stop_runtime=True)
    elapsed = time.monotonic() - start
    status = supervisor.status()
    final_health = read_json(directory / "runtime" / "runtime_health.json")
    if final_health.get("run_id") == started.get("run_id"):
        latest_health = final_health
    performance = read_json(directory / "runtime" / "performance_summary.json")
    attribution = performance.get("attribution") or attribution_summary(config, [])
    # No raw JSONL scan or new latency inference: consume existing bounded summaries.
    ingest = {r["camera_id"]: r for r in performance.get("camera_ingest", {}).get("cameras", [])}
    fight = {r["camera_id"]: r for r in performance.get("cameras", [])}
    cameras = {}
    wall = performance.get("wall_processing_sec", elapsed)
    for camera in config["cameras"]:
        cid = camera["camera_id"]
        decoded = ingest.get(cid, {}).get("frames_decoded")
        camera_health = latest_health.get("cameras", {}).get(cid, {})
        cameras[cid] = {"frames_decoded": decoded, "fight_frames_consumed": fight.get(cid, {}).get("frames_read"),
                       "decode_effective_fps_full_run": decoded / wall if decoded is not None and wall else None,
                       "fight_processing_fps": fight.get(cid, {}).get("camera_processing_fps"),
                       "speed_progress": camera_health.get("speed", {}).get("progress"),
                       "frames_dropped_fight": ingest.get(cid, {}).get("frames_dropped_fight"),
                       "frames_dropped_speed": ingest.get(cid, {}).get("frames_dropped_speed"),
                       "reconnects": ingest.get(cid, {}).get("reconnect_count"), "final_health": camera_health}
    frames = sum(row["frames_decoded"] or 0 for row in cameras.values())
    capacity = performance.get("capacity", {})
    stages = {}
    for name in ("person", "pose", "stage3", "vehicle"):
        worker = latest_health.get("workers", {}).get(name, {})
        counters = capacity.get(name) or worker.get("capacity", {})
        if counters:
            capacity[name] = {**counters, "observed_outstanding_peak": peak_outstanding.get(name, 0)}
        metrics = performance.get(name, {})
        completed = metrics.get("results") if name in ("person", "pose") else metrics.get("jobs_completed")
        latency = stage_latency_summary(metrics)
        stages[name] = {"accepted": counters.get("accepted"), "dispatches": counters.get("dispatches"),
                        "completed_or_client_results": completed, "health_progress": worker.get("progress"),
                        "latency": latency or {"available": False, "reason": "no_existing_latency_summary"}}
    vehicle = (attribution.get("vehicle") or {}).get("metrics", {})
    if vehicle:
        stages["vehicle"]["completed_or_client_results"] = vehicle.get("counters", {}).get("requests_completed")
        stages["vehicle"]["latency"] = vehicle.get("timings", {})
    stages["vehicle"]["client_round_trip_ms_by_camera"] = {
        cid: (row or {}).get("metrics", {}).get("timings", {}).get("vehicle_round_trip_ms")
        for cid, row in attribution.get("speed_local", {}).items()}
    system = sampler.summary()
    complete = (not deadline_reached and status.get("runtime_exit_code") == 0
                and len(ingest) == count and system["observations"] > 0 and not recovered
                and required_cameras_complete(config, cameras))
    result = {"mode": "real_inference", "run_id": started["run_id"], "camera_count": count,
              "workload": args.workload, "seed": args.seed, "duration_sec": elapsed,
              "runtime_wall_sec": wall, "requested_measurement_max_sec": args.duration_sec,
              "system_warmup_sec": args.warmup_sec, "deadline_reached": deadline_reached,
              "runtime_exit_code": status.get("runtime_exit_code"), "runtime_state": status.get("runtime_state"),
              "runtime_health": latest_health.get("runtime_health"), "source_identity": identity,
              "health_reason": latest_health.get("reason"), "observed_recovery": recovered,
              "configuration": {"runtime": redact({k: v for k, v in config["runtime"].items() if not k.endswith("_path")}),
                  "base_config_sha256": hashlib.sha256(Path(args.config).read_bytes()).hexdigest(),
                  "models": {key: file_identity(repo / value) for key, value in config.get("models", {}).items()
                             if isinstance(value, str) and (repo / value).is_file()},
                  "speed": redact(config.get("speed", {})),
                  "speed_camera_config": redact(config["cameras"][0].get("speed_config", {})),
                  "gpu_device_selector": args.gpu_device},
              "system": system, "cameras": cameras, "total_frames_decoded": frames,
              "aggregate_decode_effective_fps_full_run": frames / wall if wall else None,
              "aggregate_fight_processing_fps": performance.get("aggregate_processing_fps"),
              "stages": stages, "capacity": capacity, "attribution": attribution,
              "unavailable": ["end_to_end_incident_latency", "pure_ipc_copy_ms", "gpu_kernel_ms", "steady_window_camera_fps"],
              "measurement_scope": "ordered local files, EOF or deadline; frame rates include startup and drain; "
                                   "system samples exclude wall warmup; stage steady-state excludes configured first requests"}
    result.update(classify(complete=complete, frames=frames, capacity=capacity, system=system,
                           thresholds=args.thresholds, health_failed=health_failed))
    return result
