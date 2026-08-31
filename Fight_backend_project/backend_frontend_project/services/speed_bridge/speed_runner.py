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

from speed_detection.models import SpeedCameraConfig
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


def start_speed_pipeline() -> ActiveSpeedRun:
    cameras = _build_camera_items()

    if not cameras:
        raise RuntimeError(
            "Hız tespiti için aktif kamera bulunamadı. "
            "Admin kamera düzenleme ekranından Hız Tespiti Durumu aktif edilmeli."
        )

    run_name = time.strftime("speed_run_%Y%m%d_%H%M%S")
    run_dir = _speed_runs_root() / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "previews").mkdir(parents=True, exist_ok=True)
    (run_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (run_dir / "clips").mkdir(parents=True, exist_ok=True)
    (run_dir / "events").mkdir(parents=True, exist_ok=True)

    config = build_run_config(run_name=run_name, run_dir=run_dir, cameras=cameras)
    config_path = run_dir / "run_config.json"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"

    cmd = build_command(config_path)

    env = os.environ.copy()
    repo_root = str(_repo_root())

    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    if existing_pythonpath:
        env["PYTHONPATH"] = repo_root + os.pathsep + existing_pythonpath
    else:
        env["PYTHONPATH"] = repo_root

    _write_command_debug(run_dir, cmd, env)

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
            "Hız tespiti pipeline başladıktan hemen sonra kapandı.\n\n"
            f"return_code={rc}\n"
            f"run_dir={run_dir}\n\n"
            f"COMMAND:\n{cmd_text}\n\n"
            f"STDOUT tail:\n{stdout_tail}\n\n"
            f"STDERR tail:\n{stderr_tail}\n"
        )

    return ActiveSpeedRun(
        process=process,
        run_name=run_name,
        run_dir=run_dir,
        config_path=config_path,
        cameras=cameras,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        started_at=time.time(),
    )


def stop_speed_pipeline(active: ActiveSpeedRun | None) -> None:
    if active is None:
        return

    process = active.process

    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=5)
        else:
            process.terminate()
            process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass