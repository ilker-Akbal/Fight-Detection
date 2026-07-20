from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import signal
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import cv2

from .config import SpeedMpCamera, SpeedMpConfig, load_mp_config
from .messages import status_message, stop_message
from .process_camera import camera_process_main
from .process_reporter import reporter_main
from .inference_worker import vehicle_worker_main
from shared_inference.protocol import RouteCommand
from shared_inference.runtime import result_router_main


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
    stop_event = ctx.Event()
    inference_queue = ctx.Queue(maxsize=int(cfg.runtime.get("inference_queue_size", 64)))
    result_queue = ctx.Queue(maxsize=int(cfg.runtime.get("inference_result_queue_size", 128)))
    route_commands = ctx.Queue(maxsize=max(32, len(cfg.cameras) * 4))
    ready_queue = ctx.Queue(maxsize=16)
    result_channels = {("speed", cam.camera_id): ctx.Queue(
        maxsize=int(cfg.runtime.get("camera_result_queue_size", 4))) for cam in cfg.cameras}

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
    generations: dict[str, tuple[str, str]] = {}
    router = ctx.Process(target=result_router_main, name="speed_inference_router",
                         args=(result_queue, route_commands, stop_event, result_channels))
    router.start()
    inference_workers: list[mp.Process] = []
    for idx in range(max(1, int(cfg.runtime.get("vehicle_worker_count", 1)))):
        worker = ctx.Process(target=vehicle_worker_main, name=f"vehicle_inference_{idx}",
                             args=(mp_cfg_dict, inference_queue, result_queue, stop_event, ready_queue))
        worker.start()
        inference_workers.append(worker)

    for cam in cfg.cameras:
        session_id, generation_id = uuid.uuid4().hex, uuid.uuid4().hex
        generations[cam.camera_id] = (session_id, generation_id)
        route_commands.put(RouteCommand("register", "speed", cam.camera_id,
                                        session_id, generation_id))
        proc = ctx.Process(
            target=camera_process_main,
            name=f"speed_cam_{cam.camera_id}",
            args=(_camera_to_dict(cam), mp_cfg_dict, report_queue, inference_queue,
                  result_channels[("speed", cam.camera_id)], session_id, generation_id),
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
            while True:
                try:
                    worker_status = ready_queue.get_nowait()
                except Exception:
                    break
                try:
                    report_queue.put_nowait(status_message(
                        camera_id="__system__", stage="inference_worker",
                        detail="ready" if worker_status.get("ready") else "model_load_failed",
                        error=str(worker_status.get("error_code", "")),
                    ))
                except Exception:
                    pass
            if not router.is_alive() or any(not p.is_alive() for p in inference_workers):
                exit_code = 2
                _STOP = True
                break
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
        stop_event.set()
        print("[ORCH] stopping camera processes...", flush=True)

        for proc in camera_processes:
            _terminate_process(proc)

        for proc in camera_processes:
            try:
                proc.join(timeout=1.0)
            except Exception:
                pass

        for cam in cfg.cameras:
            generation = generations.get(cam.camera_id)
            if generation:
                try: route_commands.put(RouteCommand("unregister", "speed", cam.camera_id,
                                                       generation[0], generation[1]), timeout=.1)
                except Exception: pass
        for _ in inference_workers:
            try: inference_queue.put(None, timeout=.1)
            except Exception: pass
        for worker in inference_workers:
            _terminate_process(worker, timeout=8.0)
        try: result_queue.put(None, timeout=.1)
        except Exception: pass
        _terminate_process(router, timeout=5.0)

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
