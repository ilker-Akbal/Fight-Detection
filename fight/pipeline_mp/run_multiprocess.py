from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from typing import Any

from fight.pipeline_mp.camera_worker import camera_process_main
from fight.pipeline_mp.common import (
    MpPaths,
    install_signal_handlers,
    is_file_source,
    load_json,
    now_str,
    write_json,
)
from fight.pipeline_mp.incident_worker import incident_process_main
from fight.pipeline_mp.messages import ReportMessage
from fight.pipeline_mp.reporter import reporter_process_main
from fight.pipeline_mp.stage3_worker import stage3_process_main


def _put_status(report_queue, row: dict) -> None:
    try:
        report_queue.put(ReportMessage(kind="status", row=row), timeout=0.5)
    except Exception:
        pass


def _safe_qsize(q) -> int:
    try:
        return int(q.qsize())
    except Exception:
        return -1


def _safe_empty(q) -> bool:
    try:
        return bool(q.empty())
    except Exception:
        return False


def _start_process(name: str, target, args: tuple) -> mp.Process:
    p = mp.Process(name=name, target=target, args=args, daemon=False)
    p.start()
    return p


def _terminate_process(p: mp.Process | None, timeout: float = 5.0) -> None:
    if p is None:
        return

    try:
        if not p.is_alive():
            p.join(timeout=0.2)
            return
    except Exception:
        return

    try:
        p.join(timeout=timeout)
    except Exception:
        pass

    if p.is_alive():
        try:
            p.terminate()
        except Exception:
            pass

    try:
        p.join(timeout=timeout)
    except Exception:
        pass

    if p.is_alive():
        try:
            p.kill()
        except Exception:
            pass

    try:
        p.join(timeout=1.0)
    except Exception:
        pass


def _camera_id(cam: dict[str, Any]) -> str:
    return str(cam.get("camera_id", "")).strip()


def _camera_source(cam: dict[str, Any]) -> str:
    return str(cam.get("source", "")).strip()


def _camera_is_file(cam: dict[str, Any]) -> bool:
    return is_file_source(_camera_source(cam))


def _should_not_restart_finished_camera(cam: dict[str, Any], runtime: dict, exitcode: int | None) -> bool:
    """
    Dosya kaynaklı testlerde kamera process'i video bitince exitcode=0 ile çıkar.
    Bu durumda tekrar başlatmak istemiyoruz; aksi halde aynı video loop gibi tekrar başlar
    ve incident finalize davranışı karışır.

    RTSP/canlı kaynaklarda ise restart_camera_processes=True ise restart devam eder.
    """
    if exitcode != 0:
        return False

    source_is_file = _camera_is_file(cam)
    loop_file_sources = bool(runtime.get("loop_file_sources", False))
    stop_when_file_camera_done = bool(runtime.get("stop_when_file_camera_done", False))

    if source_is_file and not loop_file_sources:
        return True

    if stop_when_file_camera_done:
        return True

    return False


def _all_file_cameras(cameras: list[dict[str, Any]]) -> bool:
    if not cameras:
        return False
    return all(_camera_is_file(cam) for cam in cameras)


def _wait_for_pipeline_settle(
    *,
    stage3_queue,
    incident_queue,
    report_queue,
    runtime: dict,
) -> None:
    """
    Dosya kaynaklı test bittiğinde kamera process'leri kapanır ama Stage3/incident tarafı
    hâlâ kuyruktaki işleri işliyor olabilir.

    Bu bekleme:
    - Stage3 queue boşalsın,
    - incident queue boşalsın,
    - IncidentAggregator sweeper stale_finalize_sec süresini görüp incidents.jsonl yazabilsin
    diye var.
    """
    stale_finalize_sec = float(runtime.get("incident_stale_finalize_sec", 3.0))
    clip_ready_wait_sec = float(runtime.get("incident_clip_ready_wait_sec", 8.0))

    settle_empty_sec = float(runtime.get("file_run_queue_empty_settle_sec", stale_finalize_sec + 1.0))
    max_wait_sec = float(
        runtime.get(
            "file_run_finalize_wait_sec",
            max(12.0, stale_finalize_sec + clip_ready_wait_sec + 5.0),
        )
    )

    deadline = time.time() + max_wait_sec
    empty_since: float | None = None
    last_log_ts = 0.0

    _put_status(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "orchestrator",
            "detail": "waiting_pipeline_settle",
            "stage3_queue_size": _safe_qsize(stage3_queue),
            "incident_queue_size": _safe_qsize(incident_queue),
            "max_wait_sec": round(float(max_wait_sec), 3),
            "settle_empty_sec": round(float(settle_empty_sec), 3),
        },
    )

    while time.time() < deadline:
        stage3_empty = _safe_empty(stage3_queue)
        incident_empty = _safe_empty(incident_queue)

        if stage3_empty and incident_empty:
            if empty_since is None:
                empty_since = time.time()

            if (time.time() - empty_since) >= settle_empty_sec:
                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "orchestrator",
                        "detail": "pipeline_settled",
                        "stage3_queue_size": _safe_qsize(stage3_queue),
                        "incident_queue_size": _safe_qsize(incident_queue),
                        "waited_empty_sec": round(float(time.time() - empty_since), 3),
                    },
                )
                return
        else:
            empty_since = None

        now = time.time()
        if now - last_log_ts >= 1.0:
            last_log_ts = now
            _put_status(
                report_queue,
                {
                    "ts": now_str(),
                    "camera_id": "__system__",
                    "stage": "orchestrator",
                    "detail": "pipeline_settle_progress",
                    "stage3_queue_size": _safe_qsize(stage3_queue),
                    "incident_queue_size": _safe_qsize(incident_queue),
                    "stage3_empty": int(stage3_empty),
                    "incident_empty": int(incident_empty),
                },
            )

        time.sleep(0.25)

    _put_status(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "orchestrator",
            "detail": "pipeline_settle_timeout",
            "stage3_queue_size": _safe_qsize(stage3_queue),
            "incident_queue_size": _safe_qsize(incident_queue),
            "max_wait_sec": round(float(max_wait_sec), 3),
        },
    )


def _start_camera(
    *,
    config: dict,
    cam: dict[str, Any],
    stage3_queue,
    report_queue,
    stop_event,
) -> mp.Process:
    cid = _camera_id(cam)

    return _start_process(
        f"camera_{cid}",
        camera_process_main,
        (config, cam, stage3_queue, report_queue, stop_event),
    )


def run(config: dict) -> int:
    output_dir = Path(config["output_dir"])
    paths = MpPaths.from_output_dir(output_dir)
    paths.mkdirs()

    runtime = config.setdefault("runtime", {})
    cameras = config.get("cameras", [])

    if not cameras:
        raise RuntimeError("run_config.json içinde cameras boş")

    ctx = mp.get_context("spawn")

    stage3_queue_size = int(runtime.get("stage3_queue_size", 64))
    incident_queue_size = int(runtime.get("incident_queue_size", 256))
    report_queue_size = int(runtime.get("report_queue_size", 8192))

    stop_event = ctx.Event()
    install_signal_handlers(stop_event)

    stage3_queue = ctx.JoinableQueue(maxsize=stage3_queue_size)
    incident_queue = ctx.JoinableQueue(maxsize=incident_queue_size)
    report_queue = ctx.JoinableQueue(maxsize=report_queue_size)

    write_json(output_dir / "run_config.effective.json", config)

    reporter = _start_process(
        "reporter",
        reporter_process_main,
        (config, report_queue, stop_event),
    )

    _put_status(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "orchestrator",
            "detail": "starting",
            "pid": os.getpid(),
            "camera_count": len(cameras),
            "all_file_cameras": int(_all_file_cameras(cameras)),
            "stage3_queue_size": stage3_queue_size,
            "incident_queue_size": incident_queue_size,
            "report_queue_size": report_queue_size,
        },
    )

    incident = _start_process(
        "incident",
        incident_process_main,
        (config, incident_queue, report_queue, stop_event),
    )

    stage3 = _start_process(
        "stage3",
        stage3_process_main,
        (config, stage3_queue, incident_queue, report_queue, stop_event),
    )

    camera_processes: dict[str, mp.Process] = {}
    finished_cameras: set[str] = set()

    for cam in cameras:
        cid = _camera_id(cam)
        if not cid:
            raise RuntimeError(f"Geçersiz kamera kaydı: {cam}")

        p = _start_camera(
            config=config,
            cam=cam,
            stage3_queue=stage3_queue,
            report_queue=report_queue,
            stop_event=stop_event,
        )
        camera_processes[cid] = p

    _put_status(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "orchestrator",
            "detail": "started",
            "pid": os.getpid(),
            "camera_pids": {cid: p.pid for cid, p in camera_processes.items()},
            "stage3_pid": stage3.pid,
            "incident_pid": incident.pid,
            "reporter_pid": reporter.pid,
        },
    )

    restart_cameras = bool(runtime.get("restart_camera_processes", True))
    camera_restart_backoff_sec = float(runtime.get("camera_restart_backoff_sec", 3.0))
    loop_file_sources = bool(runtime.get("loop_file_sources", False))

    # Dosya bazlı testlerde default olarak tüm file kameralar bitince run kapansın.
    # RTSP/canlı sistemde bu alan etkili olmaz.
    stop_run_when_all_file_cameras_done = bool(
        runtime.get(
            "stop_run_when_all_file_cameras_done",
            _all_file_cameras(cameras) and not loop_file_sources,
        )
    )

    last_restart: dict[str, float] = {}

    exit_code = 0
    graceful_file_finish = False

    try:
        while not stop_event.is_set():
            if not stage3.is_alive():
                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "orchestrator",
                        "detail": "stage3_process_dead",
                        "exitcode": stage3.exitcode,
                    },
                )
                stop_event.set()
                exit_code = 2
                break

            if not incident.is_alive():
                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "orchestrator",
                        "detail": "incident_process_dead",
                        "exitcode": incident.exitcode,
                    },
                )
                stop_event.set()
                exit_code = 3
                break

            for cam in cameras:
                cid = _camera_id(cam)
                p = camera_processes.get(cid)

                if cid in finished_cameras:
                    continue

                if p is not None and p.is_alive():
                    continue

                exitcode = None if p is None else p.exitcode

                if p is not None and _should_not_restart_finished_camera(cam, runtime, exitcode):
                    finished_cameras.add(cid)

                    _put_status(
                        report_queue,
                        {
                            "ts": now_str(),
                            "camera_id": cid,
                            "stage": "orchestrator",
                            "detail": "camera_finished_no_restart",
                            "exitcode": exitcode,
                            "source": _camera_source(cam),
                            "source_is_file": int(_camera_is_file(cam)),
                            "finished_count": len(finished_cameras),
                            "camera_count": len(cameras),
                        },
                    )
                    continue

                if not restart_cameras:
                    _put_status(
                        report_queue,
                        {
                            "ts": now_str(),
                            "camera_id": cid,
                            "stage": "orchestrator",
                            "detail": "camera_dead_restart_disabled",
                            "exitcode": exitcode,
                            "source": _camera_source(cam),
                        },
                    )
                    continue

                now = time.time()
                if now - last_restart.get(cid, 0.0) < camera_restart_backoff_sec:
                    continue

                last_restart[cid] = now

                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": cid,
                        "stage": "orchestrator",
                        "detail": "camera_process_restarting",
                        "old_exitcode": exitcode,
                        "source": _camera_source(cam),
                        "source_is_file": int(_camera_is_file(cam)),
                    },
                )

                np = _start_camera(
                    config=config,
                    cam=cam,
                    stage3_queue=stage3_queue,
                    report_queue=report_queue,
                    stop_event=stop_event,
                )
                camera_processes[cid] = np

            if (
                stop_run_when_all_file_cameras_done
                and _all_file_cameras(cameras)
                and len(finished_cameras) == len(cameras)
            ):
                graceful_file_finish = True

                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "orchestrator",
                        "detail": "all_file_cameras_finished",
                        "finished_count": len(finished_cameras),
                        "camera_count": len(cameras),
                    },
                )

                _wait_for_pipeline_settle(
                    stage3_queue=stage3_queue,
                    incident_queue=incident_queue,
                    report_queue=report_queue,
                    runtime=runtime,
                )

                exit_code = 0
                stop_event.set()
                break

            time.sleep(0.5)

    except KeyboardInterrupt:
        stop_event.set()
        exit_code = 130

    finally:
        _put_status(
            report_queue,
            {
                "ts": now_str(),
                "camera_id": "__system__",
                "stage": "orchestrator",
                "detail": "stopping",
                "graceful_file_finish": int(graceful_file_finish),
                "exit_code": exit_code,
            },
        )

        stop_event.set()

        for _, p in camera_processes.items():
            _terminate_process(p, timeout=4.0)

        try:
            stage3_queue.put(None, timeout=1.0)
        except Exception:
            pass

        _terminate_process(stage3, timeout=8.0)

        try:
            incident_queue.put(None, timeout=1.0)
        except Exception:
            pass

        _terminate_process(incident, timeout=8.0)

        try:
            report_queue.put(
                ReportMessage(
                    kind="status",
                    row={
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "orchestrator",
                        "detail": "stopped",
                        "exit_code": exit_code,
                        "graceful_file_finish": int(graceful_file_finish),
                    },
                ),
                timeout=1.0,
            )
        except Exception:
            pass

        try:
            report_queue.put(None, timeout=1.0)
        except Exception:
            pass

        _terminate_process(reporter, timeout=5.0)

    return int(exit_code)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to run_config.json")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    config = load_json(args.config)

    return run(config)


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        mp.freeze_support()

    raise SystemExit(main())