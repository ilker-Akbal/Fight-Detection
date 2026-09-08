from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

from services.speed_bridge.calibration_writer import resolve_speed_calibration_path


@dataclass
class ActiveSpeedRun:
    process: subprocess.Popen
    run_name: str
    run_dir: Path
    config_path: Path
    cameras: list[dict[str, Any]]
    stdout_path: Path
    stderr_path: Path
    started_at: float


def _speed_runs_root() -> Path:
    root = Path(settings.MEDIA_ROOT) / "speed_runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _repo_root() -> Path:
    return Path(getattr(settings, "REPO_ROOT", Path(settings.BASE_DIR).parent.parent))


def _default_speed_config_path() -> str:
    return str(
        getattr(
            settings,
            "SPEED_PIPELINE_BASE_CONFIG",
            "HizTespiti/speed/configs/speed.yaml",
        )
    )


def _default_speed_entry_module() -> str:
    return str(
        getattr(
            settings,
            "SPEED_PIPELINE_ENTRY_MODULE",
            "HizTespiti.speed_mp.run_multiprocess_speed",
        )
    )


def _default_yolo_weights() -> str:
    return str(
        getattr(
            settings,
            "SPEED_YOLO_WEIGHTS",
            "yolo11s.pt",
        )
    )


def _tail_file(path: Path, max_chars: int = 8000) -> str:
    try:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]
    except Exception as exc:
        return f"<log okunamadı: {exc}>"


def _write_command_debug(run_dir: Path, cmd: list[str], env: dict[str, str]) -> None:
    try:
        lines = []
        lines.append("COMMAND:")
        lines.append(" ".join(f'"{x}"' if " " in str(x) else str(x) for x in cmd))
        lines.append("")
        lines.append(f"CWD: {_repo_root()}")
        lines.append(f"PYTHON: {sys.executable}")
        lines.append(f"PYTHONPATH: {env.get('PYTHONPATH', '')}")
        lines.append("")
        lines.append("ARGS:")
        for item in cmd:
            lines.append(str(item))

        (run_dir / "command.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _build_camera_items() -> list[dict[str, Any]]:
    from speed_detection.models import SpeedCameraConfig
    qs = (
    SpeedCameraConfig.objects
    .select_related("camera")
    .filter(
        enabled=True,
        camera__is_active=True,
        camera__use_speed_detection=True,
    )
    .order_by("camera__camera_id")
)

    cameras: list[dict[str, Any]] = []

    for item in qs:
        camera = item.camera
        calibration_path = resolve_speed_calibration_path(
            item.calibration_path,
            camera_id=camera.camera_id,
        )

        cameras.append(
            {
                "camera_id": str(camera.camera_id),
                "name": str(camera.name),
                "source": str(camera.source),
                "description": str(camera.description or ""),
                "faculty": str(camera.faculty or ""),

                "speed_limit_kmh": _safe_float(item.speed_limit_kmh, 50.0),
                "tolerance_kmh": _safe_float(item.tolerance_kmh, 10.0),

                # Pipeline subprocess REPO_ROOT altında çalışır. Bu yüzden run_config'e
                # çözümlenmiş mutlak path yazıyoruz; eski göreli path karmaşası bitiyor.
                "calibration_path": str(calibration_path),

                "roi_enabled": bool(item.roi_enabled),
                "roi_polygon": item.roi_polygon or [],

                "save_snapshot": bool(item.save_snapshot),
                "save_clip": bool(item.save_clip),
            }
        )

    return cameras


def build_run_config(run_name: str, run_dir: Path, cameras: list[dict[str, Any]]) -> dict[str, Any]:
    speed_defaults = getattr(settings, "SPEED_PIPELINE_DEFAULTS", {})

    return {
        "run_name": run_name,
        "output_dir": str(run_dir),
        "base_config": speed_defaults.get("base_config", _default_speed_config_path()),
        "yolo_weights": speed_defaults.get("yolo_weights", _default_yolo_weights()),

        "runtime": {
            "resize_width": speed_defaults.get("resize_width", 960),
            "max_fps": speed_defaults.get("max_fps", 0),
            "show": False,
            "save_debug_video": speed_defaults.get("save_debug_video", False),
            "preview_every_frames": speed_defaults.get("preview_every_frames", 3),
            "preview_jpeg_quality": speed_defaults.get("preview_jpeg_quality", 80),
            "status_every_frames": speed_defaults.get("status_every_frames", 15),
            "report_flush_interval_sec": speed_defaults.get("report_flush_interval_sec", 0.25),
            "reconnect_sec": speed_defaults.get("reconnect_sec", 1.0),
            "cv2_threads": speed_defaults.get("cv2_threads", 1),
        },

        "motion": {
            "enabled": speed_defaults.get("motion_enabled", True),
        },

        "yolo": {
            "stride": speed_defaults.get("yolo_stride", 3),
            "conf": speed_defaults.get("yolo_conf", 0.30),
            "iou": speed_defaults.get("yolo_iou", 0.50),
            "imgsz": speed_defaults.get("yolo_imgsz", 640),
            "device": speed_defaults.get("yolo_device", 0),
            "vehicle_classes": speed_defaults.get(
                "vehicle_classes",
                ["car", "motorcycle", "bus", "truck"],
            ),
        },

        "tracker": {
            "iou_threshold": speed_defaults.get("tracker_iou_threshold", 0.25),
            "max_age": speed_defaults.get("tracker_max_age", 20),
            "min_hits": speed_defaults.get("tracker_min_hits", 2),
        },

        "speed": {
            "min_track_points": speed_defaults.get("min_track_points", 6),
            "min_time_delta_sec": speed_defaults.get("min_time_delta_sec", 0.25),
            "max_time_delta_sec": speed_defaults.get("max_time_delta_sec", 3.0),
            "smooth_window": speed_defaults.get("smooth_window", 6),
            "min_valid_speed_kmh": speed_defaults.get("min_valid_speed_kmh", 3),
            "max_valid_speed_kmh": speed_defaults.get("max_valid_speed_kmh", 140),
            "confirm_frames": speed_defaults.get("confirm_frames", 2),
            "cooldown_sec": speed_defaults.get("cooldown_sec", 8),
        },

        "evidence": {
            "clip_pre_sec": speed_defaults.get("clip_pre_sec", 3),
            "clip_post_sec": speed_defaults.get("clip_post_sec", 3),
            "jpeg_quality": speed_defaults.get("jpeg_quality", 92),
        },

        "cameras": cameras,
    }


def build_command(config_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        _default_speed_entry_module(),
        "--config",
        str(config_path),
    ]




class CommonRuntimeProcess:
    """Read-only process facade for existing backend callers; never opens a source."""
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        from services.pipeline_bridge.fight_runner import get_pipeline_status
        status = get_pipeline_status()
        if status.get("runtime_state") == "UNKNOWN":
            raise RuntimeError("Common runtime ownership is unavailable")
        if status.get("speed_paused"):
            return 0
        if status.get("runtime_state") in {"RUNNING", "STARTING"} and status.get("runtime_pid") == self.pid:
            return None
        return status.get("runtime_exit_code") or 0


def _set_speed_paused(paused):
    from services.pipeline_bridge.camera_registry import supervisor_client
    client = supervisor_client()
    current = client.desired_cameras()
    cameras = list(current.get("cameras") or [])
    if paused:
        cameras = [{**item, "use_speed_detection": False} for item in cameras]
    client.update_desired_cameras({"schema_version": 1, "revision": int(current.get("revision", 0)) + 1,
                                   "cameras": cameras, "speed_paused": bool(paused)})


def start_speed_pipeline() -> ActiveSpeedRun:
    from services.pipeline_bridge.camera_registry import CameraRegistryReconciler, desired_camera_snapshot
    from services.pipeline_bridge.fight_runner import start_pipeline, _control_mode
    if _control_mode() != "supervisor":
        raise RuntimeError("Integrated Speed requires the common Runtime Supervisor")
    cameras = desired_camera_snapshot()
    if not any(item["use_speed_detection"] for item in cameras):
        raise RuntimeError("No configured Speed cameras are enabled")
    _set_speed_paused(False)
    CameraRegistryReconciler().tick()
    active = start_pipeline(cameras)
    return ActiveSpeedRun(CommonRuntimeProcess(active.runtime_pid), active.run_name, active.run_dir,
                          active.config_path, [item for item in cameras if item["use_speed_detection"]],
                          active.stdout_path, active.stderr_path, active.started_at)


def stop_speed_pipeline(active: ActiveSpeedRun | None = None) -> None:
    # Persist service intent, not a duplicate camera configuration. Reconciler
    # retains this pause until an explicit Speed start; Fight remains enabled.
    _set_speed_paused(True)


def get_active_speed_run() -> ActiveSpeedRun | None:
    from services.pipeline_bridge.fight_runner import get_pipeline_status, get_active_run
    from services.pipeline_bridge.camera_registry import supervisor_client
    status = get_pipeline_status()
    if status.get("runtime_state") == "UNKNOWN":
        raise RuntimeError("Common runtime ownership is unavailable")
    if status.get("runtime_state") not in {"STARTING", "RUNNING"} or status.get("speed_paused"):
        return None
    desired = supervisor_client().desired_cameras()
    cameras = [item for item in desired.get("cameras", []) if item.get("enabled", True) and item.get("use_speed_detection")]
    if not cameras or desired.get("speed_paused"):
        return None
    active = get_active_run(status)
    if active is None:
        raise RuntimeError("Common runtime metadata is unavailable")
    return ActiveSpeedRun(CommonRuntimeProcess(active.runtime_pid), active.run_name, active.run_dir,
                          active.config_path, cameras, active.stdout_path, active.stderr_path, active.started_at)
