from __future__ import annotations

import queue
import time

from fight.pipeline.adapters import Stage3Adapter
from fight.pipeline_mp.common import configure_process_runtime, now_str, ts_to_str
from fight.pipeline_mp.messages import ReportMessage, Stage3ResultMessage


def _report(report_queue, kind: str, row: dict) -> None:
    try:
        report_queue.put(ReportMessage(kind=kind, row=row), timeout=0.5)
    except Exception:
        pass


def stage3_process_main(config: dict, stage3_queue, incident_queue, report_queue, stop_event) -> None:
    runtime = config.get("runtime", {})
    models = config.get("models", {})

    configure_process_runtime(
        cv2_threads=int(runtime.get("stage3_cv2_threads", 1)),
        enable_cuda_tuning=True,
    )

    fight_thr = float(runtime.get("fight_thr", 0.60))
    use_stage3 = bool(runtime.get("use_stage3", True))

    _report(
        report_queue,
        "status",
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "stage3",
            "detail": "starting",
            "queue_capacity": int(runtime.get("stage3_queue_size", 64)),
        },
    )

    stage3 = None
    if use_stage3:
        stage3 = Stage3Adapter(models["stage3_config"])

    _report(
        report_queue,
        "status",
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "stage3",
            "detail": "started",
            "device": getattr(stage3, "device", "disabled") if stage3 is not None else "disabled",
            "model_name": getattr(stage3, "model_name", "disabled") if stage3 is not None else "disabled",
        },
    )

    while not stop_event.is_set() or not stage3_queue.empty():
        try:
            job = stage3_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        if job is None:
            break

        try:
            _report(
                report_queue,
                "status",
                {
                    "ts": now_str(),
                    "camera_id": job.camera_id,
                    "ip": job.source,
                    "stage": "stage3",
                    "detail": "processing",
                    "event_id": job.event_id,
                    "queue_status": "processing",
                    "queue_reason": "stage3_running",
                    "queue_size": stage3_queue.qsize(),
                },
            )

            if stage3 is None:
                prob = 0.0
                label = "stage3_disabled"
            else:
                prob = float(stage3.infer(job.frames))
                label = "fight" if prob >= fight_thr else "non_fight"

            row = {
                "camera_id": job.camera_id,
                "ip": job.source,
                "event_id": job.event_id,
                "event_start": ts_to_str(job.event_start_ts),
                "event_end": ts_to_str(job.event_end_ts),
                "clip_path": job.clip_path,
                "fight_prob": round(float(prob), 6),
                "fight_label": label,
                "pose_score_max": round(float(job.pose_score_max), 6),
                "pose_score_mean": round(float(job.pose_score_mean), 6),
                "queued_at": ts_to_str(job.created_at),
                "processed_at": now_str(),
            }

            _report(report_queue, "stage3", row)

            if stage3 is not None:
                try:
                    incident_queue.put(
                        Stage3ResultMessage(
                            camera_id=job.camera_id,
                            source=job.source,
                            event_id=job.event_id,
                            event_start_ts=job.event_start_ts,
                            event_end_ts=job.event_end_ts,
                            clip_path=job.clip_path,
                            fight_prob=float(prob),
                            fight_label=label,
                            pose_score_max=float(job.pose_score_max),
                            pose_score_mean=float(job.pose_score_mean),
                        ),
                        timeout=1.0,
                    )
                except Exception:
                    _report(
                        report_queue,
                        "status",
                        {
                            "ts": now_str(),
                            "camera_id": job.camera_id,
                            "ip": job.source,
                            "stage": "stage3",
                            "detail": "incident_queue_full",
                            "event_id": job.event_id,
                        },
                    )

            _report(
                report_queue,
                "status",
                {
                    "ts": now_str(),
                    "camera_id": job.camera_id,
                    "ip": job.source,
                    "stage": "stage3",
                    "detail": "completed",
                    "event_id": job.event_id,
                    "latest_event_status": label,
                    "latest_stage3_prob": round(float(prob), 6),
                    "latest_stage3_label": label,
                    "fight_prob": round(float(prob), 6),
                    "fight_label": label,
                    "queue_status": "processed",
                    "queue_reason": "stage3_done",
                    "queue_size": stage3_queue.qsize(),
                    "clip_path": job.clip_path,
                },
            )

        except Exception as exc:
            _report(
                report_queue,
                "status",
                {
                    "ts": now_str(),
                    "camera_id": getattr(job, "camera_id", "-"),
                    "ip": getattr(job, "source", "-"),
                    "stage": "stage3",
                    "detail": "failed",
                    "event_id": getattr(job, "event_id", "-"),
                    "error": str(exc),
                    "queue_status": "failed",
                    "queue_reason": str(exc),
                },
            )

        finally:
            try:
                stage3_queue.task_done()
            except Exception:
                pass

    _report(
        report_queue,
        "status",
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "stage3",
            "detail": "stopped",
        },
    )