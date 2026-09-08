from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fight.pipeline_mp.camera_ingest import camera_ingest_process_main
from fight.pipeline_mp.camera_preview import camera_preview_process_main
from fight.pipeline_mp.camera_worker import camera_process_main
from fight.pipeline_mp.camera_lifecycle import CameraRuntimeManager
from fight.pipeline_mp.common import (
    MpPaths,
    install_signal_handlers,
    is_file_source,
    load_json,
    now_str,
    redact_source,
    write_json,
)
from fight.pipeline_mp.incident_worker import incident_process_main
from fight.pipeline_mp.health import (
    HealthPolicy,
    HealthRegistry,
    HealthSnapshotStore,
    RuntimeWatchdog,
)
from fight.pipeline_mp.messages import ReportMessage
from fight.pipeline_mp.person_worker import (
    person_inference_process_main,
    person_result_router_main,
)
from fight.pipeline_mp.performance import build_performance_summary, load_status_rows
from fight.pipeline_mp.pose_worker import (
    pose_inference_process_main,
    pose_result_router_main,
)
from fight.pipeline_mp.reporter import reporter_process_main
from fight.pipeline_mp.scheduling import FairRequestQueue
from fight.pipeline_mp.stage3_worker import stage3_process_main
from fight.runtime_supervisor.camera_state import (
    DesiredCameraStateStore,
    InvalidDesiredCameraState,
)


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
    p = mp.get_context("spawn").Process(name=name, target=target, args=args, daemon=False)
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


def _close_queue(q) -> None:
    try:
        q.close()
    except Exception:
        pass
    try:
        q.join_thread()
    except Exception:
        pass


def _put_sentinel(q, process: mp.Process | None, timeout: float = 5.0) -> bool:
    if q is None:
        return False
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        if process is not None and not process.is_alive():
            return False
        try:
            q.put(None, timeout=min(0.25, max(0.01, deadline - time.monotonic())))
            return True
        except Exception:
            continue
    return False


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
    person_request_queue,
    person_result_queue,
    pose_request_queue,
    pose_result_queue,
    generation: int,
    frame_queue=None,
) -> mp.Process:
    cid = _camera_id(cam)

    return _start_process(
        f"camera_{cid}",
        camera_process_main,
        (
            config,
            cam,
            stage3_queue,
            report_queue,
            stop_event,
            person_request_queue,
            person_result_queue,
            pose_request_queue,
            pose_result_queue,
            generation,
            frame_queue,
        ),
    )


def _run_static(config: dict) -> int:
    run_started_monotonic = time.perf_counter()
    run_id = str(config.get("run_id") or uuid.uuid4().hex)
    config["run_id"] = run_id
    output_dir = Path(config["output_dir"])
    paths = MpPaths.from_output_dir(output_dir)
    paths.mkdirs()
    status_path = output_dir / "camera_status.jsonl"
    status_start_offset = status_path.stat().st_size if status_path.exists() else 0

    runtime = config.setdefault("runtime", {})
    runtime["run_id"] = run_id
    cameras = config.get("cameras", [])

    camera_ids = [_camera_id(cam) for cam in cameras]
    if any(not camera_id for camera_id in camera_ids):
        raise RuntimeError("run_config.json contains an empty camera_id")
    if len(set(camera_ids)) != len(camera_ids):
        raise RuntimeError("run_config.json camera_id values must be unique")

    if not cameras:
        raise RuntimeError("run_config.json içinde cameras boş")

    ctx = mp.get_context("spawn")

    stage3_queue_size = int(runtime.get("stage3_queue_size", 64))
    incident_queue_size = int(runtime.get("incident_queue_size", 256))
    report_queue_size = int(runtime.get("report_queue_size", 8192))
    person_request_queue_size = int(runtime.get("person_request_queue_size", max(1, len(cameras))))
    person_result_queue_size = int(runtime.get("person_result_queue_size", max(1, len(cameras))))
    person_camera_result_queue_size = int(runtime.get("person_camera_result_queue_size", 2))
    use_pose = bool(runtime.get("use_pose", True))
    pose_request_queue_size = int(runtime.get("pose_request_queue_size", max(1, len(cameras))))
    pose_result_queue_size = int(runtime.get("pose_result_queue_size", max(1, len(cameras))))
    pose_camera_result_queue_size = int(runtime.get("pose_camera_result_queue_size", 2))
    camera_ingest_mode = str(runtime.get("camera_ingest_mode", "legacy")).strip().lower()
    centralized_ingest = camera_ingest_mode == "centralized"
    fight_frame_queue_size = max(
        1,
        int(runtime.get("camera_ingest_fight_queue_size", 8)),
    )
    preview_frame_queue_size = max(
        1,
        int(runtime.get("camera_ingest_preview_queue_size", 1)),
    )

    stop_event = ctx.Event()
    install_signal_handlers(stop_event)

    stage3_queue = ctx.JoinableQueue(maxsize=stage3_queue_size)
    incident_queue = ctx.JoinableQueue(maxsize=incident_queue_size)
    report_queue = ctx.JoinableQueue(maxsize=report_queue_size)
    person_request_queue = ctx.JoinableQueue(maxsize=max(1, person_request_queue_size))
    person_result_queue = ctx.JoinableQueue(maxsize=max(1, person_result_queue_size))
    person_result_channels = {
        camera_id: ctx.Queue(maxsize=max(1, person_camera_result_queue_size))
        for camera_id in camera_ids
    }
    pose_request_queue = None
    pose_result_queue = None
    pose_result_channels = {}
    if use_pose:
        pose_request_queue = ctx.JoinableQueue(maxsize=max(1, pose_request_queue_size))
        pose_result_queue = ctx.JoinableQueue(maxsize=max(1, pose_result_queue_size))
        pose_result_channels = {
            camera_id: ctx.Queue(maxsize=max(1, pose_camera_result_queue_size))
            for camera_id in camera_ids
        }
    fight_frame_channels = {}
    preview_frame_channels = {}
    if centralized_ingest:
        fight_frame_channels = {
            camera_id: ctx.Queue(maxsize=fight_frame_queue_size)
            for camera_id in camera_ids
        }
        preview_frame_channels = {
            camera_id: ctx.Queue(maxsize=preview_frame_queue_size)
            for camera_id in camera_ids
        }

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
            "person_request_queue_size": person_request_queue_size,
            "person_result_queue_size": person_result_queue_size,
            "person_camera_result_queue_size": person_camera_result_queue_size,
            "pose_enabled": int(use_pose),
            "pose_request_queue_size": pose_request_queue_size if use_pose else 0,
            "pose_result_queue_size": pose_result_queue_size if use_pose else 0,
            "pose_camera_result_queue_size": pose_camera_result_queue_size if use_pose else 0,
            "camera_ingest_mode": camera_ingest_mode,
            "camera_ingest_fight_queue_size": fight_frame_queue_size if centralized_ingest else 0,
            "camera_ingest_preview_queue_size": preview_frame_queue_size if centralized_ingest else 0,
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

    person_result_router = _start_process(
        "person_result_router",
        person_result_router_main,
        (config, person_result_queue, person_result_channels, report_queue, stop_event),
    )

    person_worker = _start_process(
        "person_inference",
        person_inference_process_main,
        (config, person_request_queue, person_result_queue, report_queue, stop_event),
    )

    pose_result_router = None
    pose_worker = None
    if use_pose:
        pose_result_router = _start_process(
            "pose_result_router",
            pose_result_router_main,
            (config, pose_result_queue, pose_result_channels, report_queue, stop_event),
        )
        pose_worker = _start_process(
            "pose_inference",
            pose_inference_process_main,
            (config, pose_request_queue, pose_result_queue, report_queue, stop_event),
        )

    camera_processes: dict[str, mp.Process] = {}
    ingest_processes: dict[str, mp.Process] = {}
    preview_processes: dict[str, mp.Process] = {}
    camera_generations: dict[str, int] = {camera_id: 1 for camera_id in camera_ids}
    finished_cameras: set[str] = set()

    if centralized_ingest:
        for cam in cameras:
            cid = _camera_id(cam)
            ingest_processes[cid] = _start_process(
                f"camera_ingest_{cid}",
                camera_ingest_process_main,
                (
                    config,
                    cam,
                    fight_frame_channels[cid],
                    preview_frame_channels[cid],
                    report_queue,
                    stop_event,
                    camera_generations[cid],
                ),
            )
            preview_processes[cid] = _start_process(
                f"camera_preview_{cid}",
                camera_preview_process_main,
                (
                    config,
                    cam,
                    preview_frame_channels[cid],
                    report_queue,
                    stop_event,
                    camera_generations[cid],
                ),
            )

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
            person_request_queue=person_request_queue,
            person_result_queue=person_result_channels[cid],
            pose_request_queue=pose_request_queue,
            pose_result_queue=pose_result_channels.get(cid),
            generation=camera_generations[cid],
            frame_queue=fight_frame_channels.get(cid),
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
            "person_worker_pid": person_worker.pid,
            "person_result_router_pid": person_result_router.pid,
            "person_model_target_instances": 1,
            "pose_worker_pid": None if pose_worker is None else pose_worker.pid,
            "pose_result_router_pid": None if pose_result_router is None else pose_result_router.pid,
            "pose_model_target_instances": 1 if use_pose else 0,
            "camera_ingest_pids": {cid: p.pid for cid, p in ingest_processes.items()},
            "camera_preview_pids": {cid: p.pid for cid, p in preview_processes.items()},
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

            if not person_worker.is_alive():
                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "orchestrator",
                        "detail": "person_inference_process_dead",
                        "exitcode": person_worker.exitcode,
                    },
                )
                stop_event.set()
                exit_code = 4
                break

            if not person_result_router.is_alive():
                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "orchestrator",
                        "detail": "person_result_router_process_dead",
                        "exitcode": person_result_router.exitcode,
                    },
                )
                stop_event.set()
                exit_code = 5
                break

            if use_pose and pose_worker is not None and not pose_worker.is_alive():
                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "orchestrator",
                        "detail": "pose_inference_process_dead",
                        "exitcode": pose_worker.exitcode,
                    },
                )
                stop_event.set()
                exit_code = 6
                break

            if use_pose and pose_result_router is not None and not pose_result_router.is_alive():
                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "orchestrator",
                        "detail": "pose_result_router_process_dead",
                        "exitcode": pose_result_router.exitcode,
                    },
                )
                stop_event.set()
                exit_code = 7
                break

            if centralized_ingest:
                for cam in cameras:
                    cid = _camera_id(cam)
                    ingest = ingest_processes.get(cid)
                    if ingest is not None and ingest.is_alive():
                        continue
                    if ingest is not None and _camera_is_file(cam) and ingest.exitcode == 0:
                        continue
                    _put_status(
                        report_queue,
                        {
                            "ts": now_str(),
                            "camera_id": cid,
                            "stage": "orchestrator",
                            "detail": "camera_ingest_process_dead",
                            "exitcode": None if ingest is None else ingest.exitcode,
                            "source": redact_source(_camera_source(cam)),
                        },
                    )
                    stop_event.set()
                    exit_code = 8
                    break
                if stop_event.is_set():
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
                            "source": redact_source(_camera_source(cam)),
                            "source_is_file": int(_camera_is_file(cam)),
                            "finished_count": len(finished_cameras),
                            "camera_count": len(cameras),
                        },
                    )
                    continue

                if centralized_ingest:
                    _put_status(
                        report_queue,
                        {
                            "ts": now_str(),
                            "camera_id": cid,
                            "stage": "orchestrator",
                            "detail": "centralized_camera_consumer_dead",
                            "exitcode": exitcode,
                            "source": redact_source(_camera_source(cam)),
                        },
                    )
                    stop_event.set()
                    exit_code = 9
                    break

                if not restart_cameras:
                    _put_status(
                        report_queue,
                        {
                            "ts": now_str(),
                            "camera_id": cid,
                            "stage": "orchestrator",
                            "detail": "camera_dead_restart_disabled",
                            "exitcode": exitcode,
                            "source": redact_source(_camera_source(cam)),
                        },
                    )
                    continue

                now = time.time()
                if now - last_restart.get(cid, 0.0) < camera_restart_backoff_sec:
                    continue

                last_restart[cid] = now
                camera_generations[cid] += 1

                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": cid,
                        "stage": "orchestrator",
                        "detail": "camera_process_restarting",
                        "old_exitcode": exitcode,
                        "source": redact_source(_camera_source(cam)),
                        "source_is_file": int(_camera_is_file(cam)),
                        "generation": camera_generations[cid],
                    },
                )

                np = _start_camera(
                    config=config,
                    cam=cam,
                    stage3_queue=stage3_queue,
                    report_queue=report_queue,
                    stop_event=stop_event,
                    person_request_queue=person_request_queue,
                    person_result_queue=person_result_channels[cid],
                    pose_request_queue=pose_request_queue,
                    pose_result_queue=pose_result_channels.get(cid),
                    generation=camera_generations[cid],
                    frame_queue=fight_frame_channels.get(cid),
                )
                camera_processes[cid] = np

            if stop_event.is_set():
                break

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

        for _, p in ingest_processes.items():
            _terminate_process(p, timeout=4.0)

        for _, p in camera_processes.items():
            _terminate_process(p, timeout=4.0)

        for _, p in preview_processes.items():
            _terminate_process(p, timeout=4.0)

        _put_sentinel(person_request_queue, person_worker)

        _terminate_process(person_worker, timeout=8.0)

        _put_sentinel(person_result_queue, person_result_router)

        _terminate_process(person_result_router, timeout=5.0)

        if use_pose and pose_request_queue is not None:
            _put_sentinel(pose_request_queue, pose_worker)

        _terminate_process(pose_worker, timeout=8.0)

        if use_pose and pose_result_queue is not None:
            _put_sentinel(pose_result_queue, pose_result_router)

        _terminate_process(pose_result_router, timeout=5.0)

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

        wall_processing_sec = max(0.0, time.perf_counter() - run_started_monotonic)
        try:
            status_rows = load_status_rows(status_path, start_offset=status_start_offset)
            performance_summary = build_performance_summary(
                config,
                status_rows,
                wall_processing_sec,
            )
            write_json(output_dir / "performance_summary.json", performance_summary)
        except Exception as exc:
            print(f"[PERFORMANCE][WARN] summary write failed: {exc}", flush=True)

        for channel in person_result_channels.values():
            _close_queue(channel)
        for channel in pose_result_channels.values():
            _close_queue(channel)
        for channel in fight_frame_channels.values():
            _close_queue(channel)
        for channel in preview_frame_channels.values():
            _close_queue(channel)
        _close_queue(person_request_queue)
        _close_queue(person_result_queue)
        if pose_request_queue is not None:
            _close_queue(pose_request_queue)
        if pose_result_queue is not None:
            _close_queue(pose_result_queue)
        _close_queue(stage3_queue)
        _close_queue(incident_queue)
        _close_queue(report_queue)

    return int(exit_code)


def _run_dynamic(config: dict) -> int:
    """Run shared inference services once and reconcile camera process trios in-place."""
    run_started_monotonic = time.perf_counter()
    run_id = str(config.get("run_id") or uuid.uuid4().hex)
    config["run_id"] = run_id
    output_dir = Path(config["output_dir"])
    paths = MpPaths.from_output_dir(output_dir)
    paths.mkdirs()
    status_path = output_dir / "camera_status.jsonl"
    status_start_offset = status_path.stat().st_size if status_path.exists() else 0

    runtime = config.setdefault("runtime", {})
    runtime["run_id"] = run_id
    runtime["camera_ingest_mode"] = "centralized"
    initial_cameras = list(config.get("cameras") or [])
    ctx = mp.get_context("spawn")
    slot_count = max(
        len(initial_cameras), int(runtime.get("dynamic_camera_slot_count", 32)), 1
    )
    use_pose = bool(runtime.get("use_pose", True))
    stop_event = ctx.Event()
    install_signal_handlers(stop_event)

    stage3_queue = ctx.JoinableQueue(maxsize=max(1, int(runtime.get("stage3_queue_size", 64))))
    incident_queue = ctx.JoinableQueue(
        maxsize=max(1, int(runtime.get("incident_queue_size", 256)))
    )
    report_queue = ctx.JoinableQueue(
        maxsize=max(1, int(runtime.get("report_queue_size", 8192)))
    )
    health_enabled = bool(runtime.get("health_enabled", True))
    health_queue = (
        ctx.Queue(maxsize=max(64, int(runtime.get("health_queue_size", 4096))))
        if health_enabled
        else None
    )
    person_request_queue = ctx.JoinableQueue(
        maxsize=max(1, int(runtime.get("person_request_queue_size", slot_count)))
    )
    person_result_queue = ctx.JoinableQueue(
        maxsize=max(1, int(runtime.get("person_result_queue_size", slot_count)))
    )
    person_channel_size = max(
        1, int(runtime.get("person_camera_result_queue_size", 2))
    )
    person_result_channels = {
        slot_id: ctx.Queue(maxsize=person_channel_size) for slot_id in range(slot_count)
    }
    pose_request_queue = None
    pose_result_queue = None
    pose_result_channels = {}
    if use_pose:
        pose_request_queue = ctx.JoinableQueue(
            maxsize=max(1, int(runtime.get("pose_request_queue_size", slot_count)))
        )
        pose_result_queue = ctx.JoinableQueue(
            maxsize=max(1, int(runtime.get("pose_result_queue_size", slot_count)))
        )
        pose_channel_size = max(
            1, int(runtime.get("pose_camera_result_queue_size", 2))
        )
        pose_result_channels = {
            slot_id: ctx.Queue(maxsize=pose_channel_size) for slot_id in range(slot_count)
        }
    slot_generations = ctx.Array("q", slot_count, lock=True)
    from fight.pipeline_mp.speed_worker import vehicle_service_main
    speed_epochs = ctx.Array("q", slot_count, lock=True)
    vehicle_requests = FairRequestQueue(ctx, slot_count, 1)
    vehicle_results = {slot: ctx.Queue(maxsize=1) for slot in range(slot_count)}
    vehicle_worker = _start_process("vehicle_inference", vehicle_service_main,
        (config, vehicle_requests, vehicle_results, stop_event, slot_generations, speed_epochs, health_queue))
    # Stable-slot reservations prevent a hot camera from taking another
    # camera's admission budget. Consumer-side RR also feeds microbatching.
    if bool(runtime.get("fair_scheduling_enabled", True)):
        for old_queue in (stage3_queue, person_request_queue, pose_request_queue):
            if old_queue is not None:
                _close_queue(old_queue)
        person_request_queue = FairRequestQueue(ctx, slot_count, 1)
        stage3_queue = FairRequestQueue(
            ctx, slot_count, max(1, int(runtime.get("stage3_pending_per_camera", 1)))
        )
        if use_pose:
            pose_request_queue = FairRequestQueue(ctx, slot_count, 1)
    config["cameras"] = initial_cameras
    write_json(output_dir / "run_config.effective.json", config)

    reporter = _start_process("reporter", reporter_process_main, (config, report_queue, stop_event))
    incident = _start_process(
        "incident",
        incident_process_main,
        (
            config,
            incident_queue,
            report_queue,
            stop_event,
            slot_generations,
            health_queue,
        ),
    )
    stage3 = _start_process(
        "stage3",
        stage3_process_main,
        (
            config,
            stage3_queue,
            incident_queue,
            report_queue,
            stop_event,
            slot_generations,
            health_queue,
        ),
    )
    person_result_router = _start_process(
        "person_result_router",
        person_result_router_main,
        (
            config,
            person_result_queue,
            person_result_channels,
            report_queue,
            stop_event,
            slot_generations,
            health_queue,
        ),
    )
    person_worker = _start_process(
        "person_inference",
        person_inference_process_main,
        (
            config,
            person_request_queue,
            person_result_queue,
            report_queue,
            stop_event,
            slot_generations,
            health_queue,
        ),
    )
    pose_result_router = None
    pose_worker = None
    if use_pose:
        pose_result_router = _start_process(
            "pose_result_router",
            pose_result_router_main,
            (
                config,
                pose_result_queue,
                pose_result_channels,
                report_queue,
                stop_event,
                slot_generations,
                health_queue,
            ),
        )
        pose_worker = _start_process(
            "pose_inference",
            pose_inference_process_main,
            (
                config,
                pose_request_queue,
                pose_result_queue,
                report_queue,
                stop_event,
                slot_generations,
                health_queue,
            ),
        )

    manager = CameraRuntimeManager(
        ctx=ctx,
        config=config,
        stage3_queue=stage3_queue,
        report_queue=report_queue,
        person_request_queue=person_request_queue,
        person_result_channels=person_result_channels,
        pose_request_queue=pose_request_queue,
        pose_result_channels=pose_result_channels,
        slot_generations=slot_generations,
        health_queue=health_queue,
        terminate_process=_terminate_process,
        close_queue=_close_queue,
        vehicle_requests=vehicle_requests,
        vehicle_results=vehicle_results,
        speed_epochs=speed_epochs,
    )
    desired_path = str(runtime.get("desired_camera_state_path") or "").strip()
    desired_store = DesiredCameraStateStore(desired_path) if desired_path else None
    desired_revision = -1
    poll_interval = max(
        0.1, float(runtime.get("dynamic_camera_reconcile_interval_sec", 0.5))
    )
    shared_processes = {
        "stage3": (stage3, 2),
        "incident": (incident, 3),
        "person_inference": (person_worker, 4),
        "person_result_router": (person_result_router, 5),
        "reporter": (reporter, 8),
    }
    if use_pose:
        shared_processes["pose_inference"] = (pose_worker, 6)
        shared_processes["pose_result_router"] = (pose_result_router, 7)
    shared_health_processes = {
        "vehicle": vehicle_worker,
        "person": person_worker,
        "person_router": person_result_router,
        "stage3": stage3,
        "incident": incident,
    }
    if use_pose:
        shared_health_processes["pose"] = pose_worker
        shared_health_processes["pose_router"] = pose_result_router
    from fight.operations import DiskMonitor
    disk_monitor = DiskMonitor(runtime)
    health_registry = HealthRegistry(max_cameras=slot_count) if health_enabled else None
    watchdog = None
    snapshot_store = None
    health_snapshot_path = Path(
        runtime.get("health_snapshot_path") or (output_dir / "runtime_health.json")
    )
    if health_registry is not None:
        for component in shared_health_processes:
            health_registry.register_worker(component)
        watchdog = RuntimeWatchdog(
            health_registry,
            HealthPolicy.from_runtime(runtime),
            report_queue,
        )
        snapshot_store = HealthSnapshotStore(health_snapshot_path)
    watchdog_interval = max(0.25, float(runtime.get("health_watchdog_interval_sec", 1.0)))
    health_drain_limit = max(64, int(runtime.get("health_event_drain_limit", 2048)))
    last_watchdog = -1e30
    health_snapshot_write_failed = False
    exit_code = 0
    graceful_file_finish = False

    _put_status(
        report_queue,
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "orchestrator",
            "detail": "dynamic_runtime_started",
            "pid": os.getpid(),
            "slot_count": slot_count,
            "desired_camera_state_path": bool(desired_path),
            "health_enabled": int(health_enabled),
            "health_snapshot_path": str(health_snapshot_path) if health_enabled else "",
        },
    )

    try:
        manager.reconcile(initial_cameras, revision=desired_revision)
        while not stop_event.is_set():
            for name, (process, failure_code) in shared_processes.items():
                if process is not None and not process.is_alive():
                    _put_status(
                        report_queue,
                        {
                            "ts": now_str(),
                            "camera_id": "__system__",
                            "stage": "orchestrator",
                            "detail": f"{name}_process_dead",
                            "exitcode": process.exitcode,
                        },
                    )
                    exit_code = failure_code
                    stop_event.set()
                    break
            if stop_event.is_set():
                break

            if desired_store is not None:
                try:
                    desired = desired_store.load()
                    revision = int(desired["revision"])
                    if revision > desired_revision:
                        manager.reconcile(desired["cameras"], revision=revision)
                        desired_revision = revision
                except InvalidDesiredCameraState:
                    _put_status(
                        report_queue,
                        {
                            "ts": now_str(),
                            "camera_id": "__system__",
                            "stage": "camera_reconciler",
                            "detail": "desired_state_invalid_ignored",
                        },
                    )
                except Exception as exc:
                    _put_status(
                        report_queue,
                        {
                            "ts": now_str(),
                            "camera_id": "__system__",
                            "stage": "camera_reconciler",
                            "detail": "desired_state_read_failed",
                            "error": type(exc).__name__,
                        },
                    )

            manager.speed_service_available = vehicle_worker.is_alive() and (
                health_registry is None or health_registry.workers["vehicle"]["health"] != "FAILED")
            manager.poll()
            manager.retry_failed(revision=desired_revision)
            if health_registry is not None and health_queue is not None:
                health_registry.sync_cameras(manager.get_camera_status())
                health_registry.drain(health_queue, health_drain_limit)
                now_monotonic = time.monotonic()
                if now_monotonic - last_watchdog >= watchdog_interval:
                    last_watchdog = now_monotonic
                    health_registry.disk = disk_monitor.sample({
                        "runs": output_dir,
                        "outbox": runtime.get("incident_outbox_path") or output_dir,
                    })
                    for stage, admission in (("person", person_request_queue),
                                             ("vehicle", vehicle_requests),
                                             ("pose", pose_request_queue),
                                             ("stage3", stage3_queue)):
                        if not hasattr(admission, "snapshot"):
                            continue
                        capacity = admission.snapshot()
                        per_slot = capacity.pop("slots")
                        health_registry.workers[stage]["capacity"] = capacity
                        for camera in health_registry.cameras.values():
                            camera.setdefault("capacity", {})[stage] = per_slot.get(str(camera["slot_id"]), {})
                    if watchdog.tick(manager, shared_health_processes):
                        exit_code = 10
                        stop_event.set()
                    try:
                        snapshot_store.write(health_registry.snapshot(run_id))
                        if health_snapshot_write_failed:
                            _put_status(
                                report_queue,
                                {
                                    "ts": now_str(),
                                    "camera_id": "__system__",
                                    "stage": "health_watchdog",
                                    "detail": "health_snapshot_write_recovered",
                                },
                            )
                        health_snapshot_write_failed = False
                    except Exception as exc:
                        if not health_snapshot_write_failed:
                            _put_status(
                                report_queue,
                                {
                                    "ts": now_str(),
                                    "camera_id": "__system__",
                                    "stage": "health_watchdog",
                                    "detail": "health_snapshot_write_failed",
                                    "error": type(exc).__name__,
                                },
                            )
                        health_snapshot_write_failed = True
            if stop_event.is_set():
                break
            stop_on_file_eof = bool(
                runtime.get(
                    "stop_run_when_all_file_cameras_done",
                    _all_file_cameras(initial_cameras)
                    and not bool(runtime.get("loop_file_sources", False)),
                )
            )
            if stop_on_file_eof and manager.all_file_cameras_done():
                if getattr(stage3_queue, "capacity_control", False) and not stage3_queue.empty():
                    # Keep ticking health while admitted file candidates drain.
                    # The legacy finalization timeout must not discard a fair
                    # queue backlog or an inference already in flight.
                    time.sleep(poll_interval)
                    continue
                graceful_file_finish = True
                if any(item.speed_failed for item in manager.runtimes.values()):
                    exit_code = 13
                    graceful_file_finish = False
                _put_status(
                    report_queue,
                    {
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "orchestrator",
                        "detail": "all_file_cameras_finished",
                        "camera_count": len(manager.runtimes),
                    },
                )
                _wait_for_pipeline_settle(
                    stage3_queue=stage3_queue,
                    incident_queue=incident_queue,
                    report_queue=report_queue,
                    runtime=runtime,
                )
                stop_event.set()
                break
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        exit_code = 130
        stop_event.set()
    finally:
        manager.stop_all(reason="global_stop")
        stop_event.set()
        _terminate_process(vehicle_worker, timeout=5.0)
        _close_queue(vehicle_requests)
        for channel in vehicle_results.values():
            _close_queue(channel)
        _put_sentinel(person_request_queue, person_worker)
        _terminate_process(person_worker, timeout=8.0)
        _put_sentinel(person_result_queue, person_result_router)
        _terminate_process(person_result_router, timeout=5.0)
        if use_pose:
            _put_sentinel(pose_request_queue, pose_worker)
            _terminate_process(pose_worker, timeout=8.0)
            _put_sentinel(pose_result_queue, pose_result_router)
            _terminate_process(pose_result_router, timeout=5.0)
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
            report_queue.put(None, timeout=1.0)
        except Exception:
            pass
        _terminate_process(reporter, timeout=5.0)

        wall_processing_sec = max(0.0, time.perf_counter() - run_started_monotonic)
        try:
            status_rows = load_status_rows(status_path, start_offset=status_start_offset)
            summary = build_performance_summary(config, status_rows, wall_processing_sec)
            summary["capacity"] = {
                stage: admission.snapshot()
                for stage, admission in (("person", person_request_queue),
                                         ("pose", pose_request_queue),
                                         ("stage3", stage3_queue))
                if hasattr(admission, "snapshot")
            }
            summary["capacity"]["vehicle"] = vehicle_requests.snapshot()
            write_json(output_dir / "performance_summary.json", summary)
        except Exception as exc:
            print(f"[PERFORMANCE][WARN] summary write failed: {exc}", flush=True)
        for channel in person_result_channels.values():
            _close_queue(channel)
        for channel in pose_result_channels.values():
            _close_queue(channel)
        _close_queue(person_request_queue)
        _close_queue(person_result_queue)
        if pose_request_queue is not None:
            _close_queue(pose_request_queue)
        if pose_result_queue is not None:
            _close_queue(pose_result_queue)
        if health_queue is not None:
            _close_queue(health_queue)
        _close_queue(stage3_queue)
        _close_queue(incident_queue)
        _close_queue(report_queue)
    return int(exit_code)


def run(config: dict) -> int:
    from fight.operations import atomic_json, run_lock_path
    from fight.runtime_supervisor.locking import SingletonLock

    if (any(camera.get("use_speed_detection") for camera in config.get("cameras", []))
            and not config.get("runtime", {}).get("dynamic_camera_lifecycle_enabled", False)):
        raise ValueError("Speed requires the common dynamic CameraIngest runtime")

    output = Path(config["output_dir"]).resolve()
    with SingletonLock(run_lock_path(output)):
        marker = output / ".run_state.json"
        run_id = str(config.get("run_id") or config.get("runtime", {}).get("run_id") or "")
        atomic_json(marker, {"state": "RUNNING", "pid": os.getpid(), "run_id": run_id})
        if bool(config.get("runtime", {}).get("dynamic_camera_lifecycle_enabled", False)):
            code = _run_dynamic(config)
        else:
            code = _run_static(config)
        # An exception/abnormal exit leaves RUNNING: cleanup fails closed.
        atomic_json(marker, {"state": "COMPLETED" if code == 0 else "FAILED",
                             "exit_code": code, "run_id": run_id, "finished_at": time.time()})
        return code


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
