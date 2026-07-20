from __future__ import annotations

import queue

from fight.pipeline.incident_aggregator import IncidentAggregator, Stage3Result
from fight.pipeline_mp.common import configure_process_runtime, now_str
from fight.pipeline_mp.messages import ReportMessage, Stage3ResultMessage


def _report(report_queue, kind: str, row: dict) -> None:
    try:
        report_queue.put(ReportMessage(kind=kind, row=row), timeout=0.5)
    except Exception:
        pass


def incident_process_main(config: dict, incident_queue, report_queue, stop_event) -> None:
    runtime = config.get("runtime", {})
    output_dir = config["output_dir"]

    configure_process_runtime(
        cv2_threads=int(runtime.get("incident_cv2_threads", 1)),
        enable_cuda_tuning=False,
    )

    agg = IncidentAggregator(
        out_dir=str(__import__("pathlib").Path(output_dir) / "incidents"),
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
            try:
                msg = incident_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if msg is None:
                break

            try:
                if not isinstance(msg, Stage3ResultMessage):
                    continue

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
                    )
                )

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

            finally:
                try:
                    incident_queue.task_done()
                except Exception:
                    pass

    finally:
        try:
            agg.close_all()
        except Exception:
            pass

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