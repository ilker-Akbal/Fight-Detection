from __future__ import annotations

import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _safe_float(value, default=0.0):
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value, default=0):
    try:
        if value in ("", None):
            return default
        return int(value)
    except Exception:
        return default


def _rel_media_path(path_str: str, media_root: Path) -> str:
    if not path_str:
        return ""

    try:
        p = Path(path_str).resolve()
        media_root = Path(media_root).resolve()
        return str(p.relative_to(media_root)).replace("\\", "/")
    except Exception:
        return ""


def _safe_clip_name(path_str: str) -> str:
    """
    Incident video endpoint'i tam Windows path değil, sadece dosya adı almalı.
    C:\\...\\file.mp4 veya /.../file.mp4 gelse bile file.mp4 döndürür.
    """
    if not path_str:
        return ""

    s = str(path_str).strip().replace("\\", "/")
    return Path(s).name


def build_dashboard_report(
    run_dir: Path,
    sources: list[dict],
    process_alive: bool,
    pid: int | None,
    started_at: float | None,
    return_code: int | None,
    media_root: Path,
):
    run_dir = Path(run_dir)

    status_rows = read_jsonl(run_dir / "camera_status.jsonl")
    stage3_rows = read_jsonl(run_dir / "stage3_results.jsonl")
    incident_rows = read_jsonl(run_dir / "incidents.jsonl")

    latest_status = {}
    for row in status_rows:
        camera_id = row.get("camera_id")
        if camera_id:
            latest_status[camera_id] = row

    latest_stage3 = {}
    for row in stage3_rows:
        camera_id = row.get("camera_id")
        if camera_id:
            latest_stage3[camera_id] = row

    latest_incident = {}
    for row in incident_rows:
        camera_id = row.get("camera_id")
        if camera_id:
            latest_incident[camera_id] = row

    cameras = []
    for item in sources:
        camera_id = item["camera_id"]
        source = item["source"]
        name = item.get("name", camera_id)

        status = latest_status.get(camera_id, {})
        stage3 = latest_stage3.get(camera_id, {})
        incident = latest_incident.get(camera_id, {})

        latest_stage3_label = status.get("latest_stage3_label") or stage3.get("fight_label") or ""

        latest_stage3_prob = status.get("latest_stage3_prob")
        if latest_stage3_prob in ("", None):
            latest_stage3_prob = stage3.get("fight_prob", 0.0)

        latest_incident_clip_path = incident.get("clip_path", "")
        latest_incident_clip_name = _safe_clip_name(latest_incident_clip_path)

        cameras.append(
            {
                "camera_id": camera_id,
                "name": name,
                "source": source,
                "stage": status.get("stage", ""),
                "detail": status.get("detail", "beklemede"),
                "last_ts": status.get("ts", ""),
                "motion_score": _safe_float(status.get("motion_score", ""), default=0.0),
                "persons": _safe_int(status.get("persons", ""), default=0),
                "pair_ok": _safe_int(status.get("pair_ok", ""), default=0),
                "pose_positive": _safe_int(status.get("pose_positive", ""), default=0),
                "pose_score": _safe_float(status.get("pose_score", ""), default=0.0),
                "event_active": _safe_int(status.get("event_active", ""), default=0),
                "latest_event_status": status.get("latest_event_status", ""),
                "latest_stage3_label": latest_stage3_label,
                "latest_stage3_prob": _safe_float(latest_stage3_prob, default=0.0),
                "latest_incident_id": incident.get("incident_id", ""),
                "latest_incident_label": incident.get("final_label", ""),
                "latest_incident_clip_path": latest_incident_clip_path,
                "latest_incident_clip_name": latest_incident_clip_name,
                "latest_incident_clip_media_path": _rel_media_path(latest_incident_clip_path, media_root),
                "latest_incident_part_count": _safe_int(incident.get("part_count", ""), default=0)
                if incident.get("part_count", "") not in ("", None)
                else "",
                "queue_status": status.get("queue_status", ""),
                "queue_reason": status.get("queue_reason", ""),
                "queue_size": _safe_int(status.get("queue_size", ""), default=0)
                if status.get("queue_size", "") != ""
                else "",
                "queue_capacity": _safe_int(status.get("queue_capacity", ""), default=0)
                if status.get("queue_capacity", "") != ""
                else "",
                "preview_media_path": f"pipeline_runs/{run_dir.name}/previews/{camera_id}.jpg",
            }
        )

    recent_stage3 = list(reversed(stage3_rows[-100:]))

    recent_incidents = []
    for row in reversed(incident_rows[-100:]):
        clip_path = row.get("clip_path", "")
        clip_name = _safe_clip_name(clip_path)

        recent_incidents.append(
            {
                **row,
                "clip_name": clip_name,
                "clip_media_path": _rel_media_path(clip_path, media_root),
            }
        )

    recent_status = list(reversed(status_rows[-100:]))

    return {
        "running": process_alive,
        "pid": pid,
        "return_code": return_code,
        "started_at": started_at,
        "run_dir": str(run_dir),
        "run_name": run_dir.name,
        "source_count": len(sources),
        "camera_count": len(cameras),
        "sources": sources,
        "cameras": cameras,
        "recent_events": [],
        "recent_stage3": recent_stage3,
        "recent_incidents": recent_incidents,
        "recent_status": recent_status,
    }