import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from fight.runtime_supervisor.client import (
    RuntimeSupervisorClient,
    SupervisorUnavailable,
)


@dataclass
class ActiveRun:
    process: subprocess.Popen | None
    run_name: str
    run_dir: Path
    sources: list[dict]
    stdout_path: Path
    stderr_path: Path
    started_at: float
    config_path: Path
    runtime_pid: int | None = None
    run_id: str = ""
    runtime_state: str = "STOPPED"
    supervisor_state: str = "UNKNOWN"
    control_mode: str = "direct"
    operation_result: str = ""


def _tail_file(path: Path, max_chars: int = 6000) -> str:
    try:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]
    except Exception as exc:
        return f"<log okunamadı: {exc}>"


def _normalize_sources(sources: list[dict]) -> list[dict]:
    out = []

    for idx, item in enumerate(sources, start=1):
        camera_id = str(item.get("camera_id") or f"cam_{idx:03d}").strip()
        source = str(item.get("source") or "").strip()

        if not camera_id:
            raise RuntimeError("Kamera camera_id boş olamaz")

        if not source:
            raise RuntimeError(f"{camera_id} için source boş olamaz")

        out.append(
            {
                "camera_id": camera_id,
                "source": source,
                "name": str(item.get("name") or camera_id),
                "description": str(item.get("description") or ""),
            }
        )

    return out


def _build_run_config(sources: list[dict], run_name: str, run_dir: Path) -> dict:
    defaults = dict(settings.PIPELINE_DEFAULTS)

    models = {
        "motion_config": defaults["motion_config"],
        "yolo_config": defaults["yolo_config"],
        "yolo_weights": defaults["yolo_weights"],
        "pose_config": defaults["pose_config"],
        "stage3_config": defaults["stage3_config"],
    }

    runtime_keys = [
        "person_conf",
        "yolo_stride",
        "pose_stride",
        "fight_thr",
        "use_pose",
        "use_stage3",
        "roi_size",
        "roi_pad_value",
        "min_persons_for_pose",
        "pose_hold_frames",
        "event_close_grace_frames",
        "prebuffer_frames",
        "max_event_frames",
        "clip_fps",
        "reconnect_sec",
        "status_log_every",
        "min_queue_frames",
        "stage3_queue_size",
        "incident_queue_size",
        "report_queue_size",
        "stage3_enqueue_timeout_sec",
        "person_request_queue_size",
        "person_result_queue_size",
        "person_camera_result_queue_size",
        "person_request_enqueue_timeout_sec",
        "person_result_enqueue_timeout_sec",
        "person_route_timeout_sec",
        "person_inference_timeout_sec",
        "person_batch_enabled",
        "person_batch_size",
        "person_batch_max_wait_ms",
        "pose_request_queue_size",
        "pose_result_queue_size",
        "pose_camera_result_queue_size",
        "pose_request_enqueue_timeout_sec",
        "pose_result_enqueue_timeout_sec",
        "pose_route_timeout_sec",
        "pose_inference_timeout_sec",
        "pose_batch_enabled",
        "pose_batch_size",
        "pose_batch_max_wait_ms",
        "performance_metrics_enabled",
        "performance_metrics_sample_every",
        "performance_metrics_max_samples",
        "performance_metrics_warmup_requests",
        "camera_cv2_threads",
        "person_worker_cv2_threads",
        "pose_worker_cv2_threads",
        "stage3_cv2_threads",
        "incident_cv2_threads",
        "camera_ingest_mode",
        "camera_ingest_fight_queue_size",
        "camera_ingest_preview_queue_size",
        "camera_ingest_publish_timeout_sec",
        "camera_ingest_file_fight_policy",
        "camera_ingest_cv2_threads",
        "camera_preview_cv2_threads",
        "camera_reconnect_enabled",
        "camera_reconnect_initial_delay_sec",
        "camera_reconnect_max_delay_sec",
        "cv2_threads",
        "camera_cuda_tuning",
        "restart_camera_processes",
        "camera_restart_backoff_sec",
        "loop_file_sources",
        "stop_when_file_camera_done",
        "person_track_max_age",
        "person_track_min_hits",
        "person_track_iou_match_thr",
        "person_track_conf_alpha",
        "person_track_max_tracks",
        "pair_enter_score",
        "pair_keep_score",
        "pair_keep_frames",
        "pair_min_hits_to_activate",
        "pair_candidate_confirm_frames",
        "pair_identity_iou_thr",
        "pair_switch_margin",
        "pair_roi_expand_x",
        "pair_roi_expand_y",
        "pair_debug",
        "stage3_event_min_positive_hits",
        "stage3_event_min_pose_mean",
        "stage3_event_min_pose_max",
        "stage3_event_min_duration_sec",
        "stage3_drop_close_reasons",
        "incident_enter_thr",
        "incident_keep_thr",
        "incident_vote_window",
        "incident_vote_enter_needed",
        "incident_vote_keep_needed",
        "incident_merge_gap_sec",
        "incident_max_bridge_nonfight",
        "incident_min_segments",
        "incident_single_strong_fight_thr",
        "incident_confirm_min_duration_sec",
        "incident_cooldown_sec",
        "incident_clip_ready_wait_sec",
        "incident_stale_finalize_sec",
        "incident_temporal_iou_merge_thr",
        "incident_write_nonfight",
        "incident_keep_temp_parts",
        "preview_every_frames",
        "preview_write_interval_sec",
        "preview_jpeg_quality",
        "report_flush_interval_sec",
        "pair_driven_event_start",
        "pair_event_start_score",
        "pair_event_min_2p_frames",

        "pair_hold_sec",
        "direct_roi_hold_sec",
        "clip_soft_hold_frames",
        "roi_invalid_drop_frames",
        "roi_person_min_count",
        "roi_person_min_iou",
        "two_p_grace_frames",

        "pose_hold_sec",
        "pose_trigger_hold_sec",
        "stop_run_when_all_file_cameras_done",
"file_run_finalize_wait_sec",
"file_run_queue_empty_settle_sec",
    ]

    runtime = {}

    for key in runtime_keys:
        if key in defaults:
            runtime[key] = defaults[key]

    runtime.setdefault("use_pose", True)
    runtime.setdefault("use_stage3", True)
    runtime.setdefault("stage3_queue_size", 64)
    runtime.setdefault("incident_queue_size", 256)
    runtime.setdefault("report_queue_size", 8192)
    runtime.setdefault("stage3_enqueue_timeout_sec", 0.15)
    runtime.setdefault("person_request_queue_size", 64)
    runtime.setdefault("person_result_queue_size", 64)
    runtime.setdefault("person_camera_result_queue_size", 2)
    runtime.setdefault("person_request_enqueue_timeout_sec", 0.5)
    runtime.setdefault("person_result_enqueue_timeout_sec", 1.0)
    runtime.setdefault("person_route_timeout_sec", 0.5)
    runtime.setdefault("person_inference_timeout_sec", 30.0)
    runtime.setdefault("person_batch_enabled", False)
    runtime.setdefault("person_batch_size", 1)
    runtime.setdefault("person_batch_max_wait_ms", 0.0)
    runtime.setdefault("pose_request_queue_size", 64)
    runtime.setdefault("pose_result_queue_size", 64)
    runtime.setdefault("pose_camera_result_queue_size", 2)
    runtime.setdefault("pose_request_enqueue_timeout_sec", 0.5)
    runtime.setdefault("pose_result_enqueue_timeout_sec", 1.0)
    runtime.setdefault("pose_route_timeout_sec", 0.5)
    runtime.setdefault("pose_inference_timeout_sec", 30.0)
    runtime.setdefault("pose_batch_enabled", False)
    runtime.setdefault("pose_batch_size", 1)
    runtime.setdefault("pose_batch_max_wait_ms", 0.0)
    runtime.setdefault("performance_metrics_enabled", False)
    runtime.setdefault("performance_metrics_sample_every", 1)
    runtime.setdefault("performance_metrics_max_samples", 2048)
    runtime.setdefault("performance_metrics_warmup_requests", 2)

    runtime.setdefault("camera_cv2_threads", 1)
    runtime.setdefault("person_worker_cv2_threads", 1)
    runtime.setdefault("pose_worker_cv2_threads", 1)
    runtime.setdefault("stage3_cv2_threads", 1)
    runtime.setdefault("incident_cv2_threads", 1)

    runtime.setdefault("camera_ingest_mode", "centralized")
    runtime.setdefault("camera_ingest_fight_queue_size", 8)
    runtime.setdefault("camera_ingest_preview_queue_size", 1)
    runtime.setdefault("camera_ingest_publish_timeout_sec", 0.2)
    runtime.setdefault("camera_ingest_file_fight_policy", "ordered")
    runtime.setdefault("camera_ingest_cv2_threads", 1)
    runtime.setdefault("camera_preview_cv2_threads", 1)
    runtime.setdefault("camera_reconnect_enabled", True)
    runtime.setdefault("camera_reconnect_initial_delay_sec", 0.5)
    runtime.setdefault("camera_reconnect_max_delay_sec", 8.0)

    runtime.setdefault("restart_camera_processes", True)
    runtime.setdefault("camera_restart_backoff_sec", 3.0)

    runtime.setdefault("roi_pad_value", 114)
    runtime.setdefault("loop_file_sources", False)
    runtime.setdefault("stop_when_file_camera_done", False)

    return {
        "schema_version": 1,
        "run_id": uuid.uuid4().hex,
        "run_name": run_name,
        "output_dir": str(run_dir),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cameras": _normalize_sources(sources),
        "models": models,
        "runtime": runtime,
    }


def _write_run_config(run_dir: Path, config: dict) -> Path:
    config_path = run_dir / "run_config.json"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return config_path


def build_command(config_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "fight.pipeline_mp.run_multiprocess",
        "--config",
        str(config_path),
    ]


def _write_command_debug(run_dir: Path, cmd: list[str], env: dict, config_path: Path):
    try:
        lines = []
        lines.append("COMMAND:")
        lines.append(" ".join(f'"{x}"' if " " in str(x) else str(x) for x in cmd))
        lines.append("")
        lines.append(f"CWD: {settings.REPO_ROOT}")
        lines.append(f"PYTHON: {sys.executable}")
        lines.append(f"PYTHONPATH: {env.get('PYTHONPATH', '')}")
        lines.append(f"CONFIG: {config_path}")
        lines.append("")
        lines.append("ARGS:")
        for x in cmd:
            lines.append(str(x))

        (run_dir / "command.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


def _prepare_run(sources: list[dict]) -> tuple[str, Path, Path, Path, Path, dict]:
    run_name = f"{time.strftime('ui_run_%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    run_dir = Path(settings.PIPELINE_OUTPUT_BASE) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "previews").mkdir(parents=True, exist_ok=True)
    (run_dir / "temp_segments").mkdir(parents=True, exist_ok=True)
    (run_dir / "incidents").mkdir(parents=True, exist_ok=True)

    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"

    config = _build_run_config(sources=sources, run_name=run_name, run_dir=run_dir)
    config_path = _write_run_config(run_dir, config)
    return run_name, run_dir, stdout_path, stderr_path, config_path, config


def _start_pipeline_direct(sources: list[dict]) -> ActiveRun:
    run_name, run_dir, stdout_path, stderr_path, config_path, config = _prepare_run(
        sources
    )

    cmd = build_command(config_path)

    env = os.environ.copy()
    repo_root = str(settings.REPO_ROOT)

    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    if existing_pythonpath:
        env["PYTHONPATH"] = repo_root + os.pathsep + existing_pythonpath
    else:
        env["PYTHONPATH"] = repo_root

    env.setdefault("PYTHONUNBUFFERED", "1")

    _write_command_debug(run_dir, cmd, env, config_path)

    stdout_handle = open(stdout_path, "a", encoding="utf-8", buffering=1)
    stderr_handle = open(stderr_path, "a", encoding="utf-8", buffering=1)

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(
        cmd,
        cwd=repo_root,
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
        creationflags=creationflags,
    )

    time.sleep(2.0)
    rc = process.poll()

    if rc is not None:
        try:
            stdout_handle.close()
        except Exception:
            pass
        try:
            stderr_handle.close()
        except Exception:
            pass

        stdout_tail = _tail_file(stdout_path)
        stderr_tail = _tail_file(stderr_path)
        cmd_text = " ".join(cmd)

        raise RuntimeError(
            "Pipeline başladıktan hemen sonra kapandı.\n\n"
            f"return_code={rc}\n"
            f"run_dir={run_dir}\n"
            f"config_path={config_path}\n\n"
            f"COMMAND:\n{cmd_text}\n\n"
            f"STDOUT tail:\n{stdout_tail}\n\n"
            f"STDERR tail:\n{stderr_tail}\n"
        )

    return ActiveRun(
        process=process,
        run_name=run_name,
        run_dir=run_dir,
        sources=config["cameras"],
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        started_at=time.time(),
        config_path=config_path,
        runtime_pid=process.pid,
        run_id=str(config.get("run_id") or ""),
        runtime_state="RUNNING",
        supervisor_state="DIRECT",
        control_mode="direct",
    )


def _stop_pipeline_direct(active: ActiveRun | None) -> None:
    if active is None:
        return

    process = active.process
    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=8)
        else:
            process.terminate()
            process.wait(timeout=8)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _control_mode() -> str:
    return str(getattr(settings, "RUNTIME_CONTROL_MODE", "supervisor")).strip().lower()


def _supervisor_client() -> RuntimeSupervisorClient:
    return RuntimeSupervisorClient(
        getattr(settings, "RUNTIME_SUPERVISOR_URL", "http://127.0.0.1:8765"),
        getattr(settings, "RUNTIME_SUPERVISOR_TOKEN", ""),
        timeout=float(getattr(settings, "RUNTIME_SUPERVISOR_TIMEOUT_SEC", 3.0)),
    )


def _parse_active_from_status(status: dict, fallback_sources=None) -> ActiveRun | None:
    runtime_state = str(status.get("runtime_state") or "STOPPED")
    config_value = status.get("config_path")
    if not config_value:
        return None
    config_path = Path(config_value)
    config = {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    run_dir = Path(config.get("output_dir") or config_path.parent)
    sources = config.get("cameras") or list(fallback_sources or [])
    return ActiveRun(
        process=None,
        run_name=str(config.get("run_name") or run_dir.name),
        run_dir=run_dir,
        sources=sources,
        stdout_path=run_dir / "stdout.log",
        stderr_path=run_dir / "stderr.log",
        started_at=time.time(),
        config_path=config_path,
        runtime_pid=status.get("runtime_pid"),
        run_id=str(status.get("run_id") or config.get("run_id") or ""),
        runtime_state=runtime_state,
        supervisor_state=str(status.get("supervisor_state") or "UNKNOWN"),
        control_mode="supervisor",
        operation_result=str(status.get("result") or ""),
    )


def get_pipeline_status() -> dict:
    if _control_mode() == "direct":
        from .runtime_state import runtime

        active = runtime.get()
        process = None if active is None else active.process
        return {
            "ok": True,
            "available": True,
            "supervisor_state": "DIRECT",
            "runtime_state": (
                "STOPPED"
                if process is None
                else ("RUNNING" if process.poll() is None else "FAILED")
            ),
            "runtime_pid": None if process is None else process.pid,
            "runtime_exit_code": None if process is None else process.poll(),
            "config_path": None if active is None else str(active.config_path),
            "run_id": "" if active is None else active.run_id,
        }
    try:
        status = _supervisor_client().status()
        status["available"] = True
        return status
    except SupervisorUnavailable:
        return {
            "ok": False,
            "available": False,
            "supervisor_state": "UNAVAILABLE",
            "runtime_state": "UNKNOWN",
            "runtime_pid": None,
            "runtime_exit_code": None,
            "config_path": None,
            "run_id": None,
            "last_failure": "supervisor_unavailable",
        }


def get_active_run() -> ActiveRun | None:
    if _control_mode() == "direct":
        from .runtime_state import runtime

        return runtime.get()
    status = get_pipeline_status()
    if not status.get("available"):
        return None
    return _parse_active_from_status(status)


def start_pipeline(sources: list[dict]) -> ActiveRun:
    if _control_mode() == "direct":
        active = _start_pipeline_direct(sources)
        from .runtime_state import runtime

        runtime.set(active)
        return active

    _, _, _, _, config_path, _ = _prepare_run(sources)
    status = _supervisor_client().start(str(config_path))
    active = _parse_active_from_status(status, fallback_sources=sources)
    if active is None:
        raise RuntimeError("Supervisor runtime metadata döndürmedi")
    return active


def stop_pipeline(active: ActiveRun | None = None) -> dict:
    if _control_mode() == "direct":
        _stop_pipeline_direct(active)
        from .runtime_state import runtime

        runtime.set(None)
        return {"ok": True, "result": "stopped", "runtime_state": "STOPPED"}
    return _supervisor_client().stop()


def restart_pipeline(config_path: str | Path | None = None) -> ActiveRun:
    if _control_mode() == "direct":
        raise RuntimeError("Direct rollback mode does not expose restart without sources")
    status = _supervisor_client().restart(
        None if config_path is None else str(config_path)
    )
    active = _parse_active_from_status(status)
    if active is None:
        raise RuntimeError("Supervisor runtime metadata döndürmedi")
    return active
