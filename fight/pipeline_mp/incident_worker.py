from __future__ import annotations

import queue
from pathlib import Path

from fight.pipeline.incident_aggregator import IncidentAggregator, Stage3Result
from fight.pipeline_mp.common import configure_process_runtime, now_str
from fight.pipeline_mp.generation import is_current_generation
from fight.pipeline_mp.health import HealthEmitter
from fight.pipeline_mp.messages import ReportMessage, Stage3ResultMessage


def _report(report_queue, kind: str, row: dict) -> None:
    try:
        report_queue.put(ReportMessage(kind=kind, row=row), timeout=0.5)
    except Exception:
        pass


def incident_process_main(
    config: dict,
    incident_queue,
    report_queue,
    stop_event,
    slot_generations=None,
    health_queue=None,
    fight_publication_floor=None,
) -> None:
    runtime = config.get("runtime", {})
    output_dir = config["output_dir"]
    output_path = Path(output_dir)
    default_spool_root = (
        output_path.parent.parent
        if output_path.parent.name == "pipeline_runs"
        else output_path.parent
    )
    outbox_path = runtime.get("incident_outbox_path") or str(
        default_spool_root / "runtime_spool" / "incidents_outbox.jsonl"
    )
    health = HealthEmitter(
        health_queue,
        component="incident",
        component_type="shared_worker",
        interval_sec=float(runtime.get("health_heartbeat_interval_sec", 1.0)),
    )
    health.emit("process_started", force=True)
    received_count = 0
    completed_count = 0

    configure_process_runtime(
        cv2_threads=int(runtime.get("incident_cv2_threads", 1)),
        enable_cuda_tuning=False,
    )

    agg = IncidentAggregator(
        out_dir=str(output_path / "incidents"),
        merge_gap_sec=float(runtime.get("incident_merge_gap_sec", 20.0)),
        max_bridge_nonfight=int(runtime.get("incident_max_bridge_nonfight", 1)),
        enter_thr=float(runtime.get("incident_enter_thr", 0.52)),
        keep_thr=float(runtime.get("incident_keep_thr", 0.48)),
        vote_window=int(runtime.get("incident_vote_window", 7)),
        vote_enter_needed=int(runtime.get("incident_vote_enter_needed", 2)),
        vote_keep_needed=int(runtime.get("incident_vote_keep_needed", 2)),
        min_incident_segments=int(runtime.get("incident_min_segments", 2)),
        single_strong_fight_thr=float(runtime.get("incident_single_strong_fight_thr", 0.68)),
        confirm_min_duration_sec=float(runtime.get("incident_confirm_min_duration_sec", 0.8)),
        cooldown_sec=float(runtime.get("incident_cooldown_sec", 60.0)),
        keep_temp_parts=bool(runtime.get("incident_keep_temp_parts", True)),
        write_nonfight_incidents=bool(runtime.get("incident_write_nonfight", False)),
        clip_ready_wait_sec=float(runtime.get("incident_clip_ready_wait_sec", 8.0)),
        stale_finalize_sec=float(runtime.get("incident_stale_finalize_sec", 8.0)),
        temporal_iou_merge_thr=float(runtime.get("incident_temporal_iou_merge_thr", 0.30)),
        run_id=str(config.get("run_id") or runtime.get("run_id") or ""),
        outbox_path=outbox_path,
        publication_floor=fight_publication_floor,
    )

    _report(
        report_queue,
        "status",
        {
            "ts": now_str(),
            "camera_id": "__system__",
            "stage": "incident",
            "detail": "started",
        },
    )

    try:
        while not stop_event.is_set() or not incident_queue.empty():
            agg.raise_if_failed()
            try:
                msg = incident_queue.get(timeout=0.5)
            except queue.Empty:
                health.heartbeat(progress=completed_count)
                continue

            if msg is None:
                break

            try:
                if not isinstance(msg, Stage3ResultMessage):
                    continue
                if not is_current_generation(msg, slot_generations):
                    _report(
                        report_queue,
                        "status",
                        {
                            "ts": now_str(),
                            "camera_id": msg.camera_id,
                            "stage": "incident",
                            "detail": "stale_generation_dropped",
                            "generation": msg.generation,
                            "slot_id": msg.slot_id,
                        },
                    )
                    continue

                received_count += 1
                health.emit("work_received", progress=received_count)

                agg.submit(
                    Stage3Result(
                        camera_id=msg.camera_id,
                        source=msg.source,
                        event_id=msg.event_id,
                        event_start_ts=msg.event_start_ts,
                        event_end_ts=msg.event_end_ts,
                        clip_path=msg.clip_path,
                        fight_prob=msg.fight_prob,
                        fight_label=msg.fight_label,
                        pose_score_max=msg.pose_score_max,
                        pose_score_mean=msg.pose_score_mean,
                        service_epoch=msg.service_epoch,
                        slot_id=msg.slot_id,
                        consumer_epoch=msg.consumer_epoch,
                    )
                )
                completed_count += 1
                health.emit("work_completed", progress=completed_count)

                _report(
                    report_queue,
                    "status",
                    {
                        "ts": now_str(),
                        "camera_id": msg.camera_id,
                        "ip": msg.source,
                        "stage": "incident",
                        "detail": "accepted_stage3_result",
                        "event_id": msg.event_id,
                        "fight_prob": round(float(msg.fight_prob), 6),
                        "fight_label": msg.fight_label,
                    },
                )

            except Exception as exc:
                _report(
                    report_queue,
                    "status",
                    {
                        "ts": now_str(),
                        "camera_id": getattr(msg, "camera_id", "-"),
                        "stage": "incident",
                        "detail": "failed",
                        "error": str(exc),
                    },
                )
                if isinstance(exc, OSError):
                    health.emit("process_error", force=True, detail="incident_persistence_failed")
                    raise

            finally:
                try:
                    incident_queue.task_done()
                except Exception:
                    pass

    finally:
        agg.close_all()

        _report(
            report_queue,
            "status",
            {
                "ts": now_str(),
                "camera_id": "__system__",
                "stage": "incident",
                "detail": "stopped",
            },
        )
        health.emit("process_stopping", force=True, progress=completed_count)
