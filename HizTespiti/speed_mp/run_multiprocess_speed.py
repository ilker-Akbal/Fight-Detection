from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

import cv2

from .config import SpeedMpCamera, SpeedMpConfig, load_mp_config
from .messages import status_message, stop_message
from .process_camera import camera_process_main
from .process_reporter import reporter_main


_STOP = False


def _handle_stop(signum, frame):
    global _STOP
    _STOP = True


def _install_signal_handlers() -> None:
    try:
        signal.signal(signal.SIGTERM, _handle_stop)
    except Exception:
        pass

    try:
        signal.signal(signal.SIGINT, _handle_stop)
    except Exception:
        pass

    if os.name == "nt":
        try:
            signal.signal(signal.SIGBREAK, _handle_stop)
        except Exception:
            pass


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    return parser.parse_args()


def _configure_runtime(cfg: SpeedMpConfig) -> None:
    cv2_threads = int(cfg.runtime.get("cv2_threads", 1) or 1)

    try:
        cv2.setNumThreads(max(1, cv2_threads))
    except Exception:
        pass

    os.environ.setdefault("OMP_NUM_THREADS", str(max(1, cv2_threads)))
    os.environ.setdefault("MKL_NUM_THREADS", str(max(1, cv2_threads)))
    os.environ.setdefault("NUMEXPR_NUM_THREADS", str(max(1, cv2_threads)))
    os.environ.setdefault("OPENBLAS_NUM_THREADS", str(max(1, cv2_threads)))

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


def _mp_config_to_process_dict(cfg: SpeedMpConfig) -> dict:
    return {
        "run_name": cfg.run_name,
        "output_dir": cfg.output_dir,
        "base_config": cfg.base_config,
        "yolo_weights": cfg.yolo_weights,
        "runtime": cfg.runtime,
        "motion": cfg.motion,
        "yolo": cfg.yolo,
        "tracker": cfg.tracker,
        "speed": cfg.speed,
        "evidence": cfg.evidence,
    }


def _camera_to_dict(cam: SpeedMpCamera) -> dict:
    return asdict(cam)


def _terminate_process(proc: mp.Process, timeout: float = 5.0) -> None:
    if proc is None:
        return

    if not proc.is_alive():
        return

    try:
        proc.terminate()
        proc.join(timeout=timeout)
    except Exception:
        pass

    if proc.is_alive():
        try:
            proc.kill()
        except Exception:
            pass

        try:
            proc.join(timeout=2.0)
        except Exception:
            pass


def main():
    global _STOP

    args = parse_args()
    cfg = load_mp_config(args.config)

    _install_signal_handlers()
    _configure_runtime(cfg)

    output_dir = Path(cfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "previews").mkdir(parents=True, exist_ok=True)
    (output_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (output_dir / "clips").mkdir(parents=True, exist_ok=True)
    (output_dir / "events").mkdir(parents=True, exist_ok=True)

    print("=" * 90, flush=True)
    print("HIZ TESPITI MULTIPROCESS ORCHESTRATOR", flush=True)
    print(f"run_name      : {cfg.run_name}", flush=True)
    print(f"output_dir    : {cfg.output_dir}", flush=True)
    print(f"base_config   : {cfg.base_config}", flush=True)
    print(f"yolo_weights  : {cfg.yolo_weights}", flush=True)
    print(f"camera_count  : {len(cfg.cameras)}", flush=True)
    print("=" * 90, flush=True)

    try:
        ctx = mp.get_context("spawn")
    except Exception:
        ctx = mp

    report_queue = ctx.Queue(maxsize=4096)

    flush_interval = float(cfg.runtime.get("report_flush_interval_sec", 0.25) or 0.25)

    reporter = ctx.Process(
        target=reporter_main,
        name="speed_reporter",
        args=(report_queue, cfg.output_dir, flush_interval),
        daemon=False,
    )

    reporter.start()

    mp_cfg_dict = _mp_config_to_process_dict(cfg)

    camera_processes: list[mp.Process] = []

    for cam in cfg.cameras:
        proc = ctx.Process(
            target=camera_process_main,
            name=f"speed_cam_{cam.camera_id}",
            args=(_camera_to_dict(cam), mp_cfg_dict, report_queue),
            daemon=False,
        )

        proc.start()
        camera_processes.append(proc)

        try:
            report_queue.put_nowait(
                status_message(
                    camera_id=cam.camera_id,
                    stage="orchestrator",
                    detail="camera_process_started",
                )
            )
        except Exception:
            pass

    exit_code = 0

    try:
        while not _STOP:
            alive_count = sum(1 for p in camera_processes if p.is_alive())

            for proc, cam in zip(camera_processes, cfg.cameras):
                if proc.exitcode is not None and proc.exitcode != 0:
                    msg = (
                        f"Camera process failed: camera_id={cam.camera_id} "
                        f"exitcode={proc.exitcode}"
                    )
                    print("[ERROR]", msg, flush=True)

                    try:
                        report_queue.put_nowait(
                            status_message(
                                camera_id=cam.camera_id,
                                stage="error",
                                detail="camera_process_exit_nonzero",
                                error=msg,
                            )
                        )
                    except Exception:
                        pass

                    exit_code = 1
                    _STOP = True
                    break

            if alive_count == 0:
                break

            time.sleep(0.5)

    except KeyboardInterrupt:
        _STOP = True

    finally:
        print("[ORCH] stopping camera processes...", flush=True)

        for proc in camera_processes:
            _terminate_process(proc)

        for proc in camera_processes:
            try:
                proc.join(timeout=1.0)
            except Exception:
                pass

        print("[ORCH] stopping reporter...", flush=True)

        try:
            report_queue.put(stop_message(), timeout=2.0)
        except Exception:
            pass

        try:
            reporter.join(timeout=5.0)
        except Exception:
            pass

        if reporter.is_alive():
            _terminate_process(reporter)

        print("[ORCH] stopped.", flush=True)

    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        mp.freeze_support()
    except Exception:
        pass

    main()