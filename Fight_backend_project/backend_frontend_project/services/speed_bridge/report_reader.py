from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    try:
        if not path.exists():
            return rows

        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue

        if limit is not None and limit > 0:
            rows = rows[-limit:]

        return rows

    except Exception:
        return rows


def _format_ts(value) -> str:
    if value in (None, "", "-"):
        return "-"

    try:
        if isinstance(value, (int, float)):
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except Exception:
        pass

    s = str(value).strip().replace("T", " ")

    if "." in s:
        s = s.split(".", 1)[0]

    return s


def _media_relative_url(path_value: str | None, media_root: str | Path | None) -> str:
    if not path_value:
        return ""

    path = Path(str(path_value))

    if media_root:
        try:
            media_root_path = Path(media_root).resolve()
            resolved = path.resolve()

            if str(resolved).startswith(str(media_root_path)):
                rel = resolved.relative_to(media_root_path).as_posix()
                return "/media/" + rel
        except Exception:
            pass

    return ""


def _file_exists(path_value: str | None) -> bool:
    if not path_value:
        return False

    try:
        return Path(str(path_value)).exists()
    except Exception:
        return False


def _normalize_event(row: dict[str, Any], run_name: str, media_root: str | Path | None) -> dict[str, Any]:
    out = dict(row)

    out["run_name"] = run_name
    out["created_at_text"] = _format_ts(out.get("created_at"))
    out["speed_kmh"] = round(float(out.get("speed_kmh", 0.0)), 2)
    out["speed_limit_kmh"] = round(float(out.get("speed_limit_kmh", 0.0)), 2)
    out["tolerance_kmh"] = round(float(out.get("tolerance_kmh", 0.0)), 2)
    out["threshold_kmh"] = round(float(out.get("threshold_kmh", 0.0)), 2)

    snapshot_path = out.get("snapshot_path")
    clip_path = out.get("clip_path")

    out["snapshot_exists"] = _file_exists(snapshot_path)
    out["clip_exists"] = _file_exists(clip_path)

    out["snapshot_url"] = _media_relative_url(snapshot_path, media_root)
    out["clip_url"] = _media_relative_url(clip_path, media_root)

    return out


def _collect_events_from_run(run_dir: Path, run_name: str, media_root: str | Path | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    # Yeni multiprocess formatı
    rows.extend(_read_jsonl(run_dir / "speed_events.jsonl"))

    # Mevcut EvidenceWriter formatı
    events_dir = run_dir / "events"
    if events_dir.exists() and events_dir.is_dir():
        for path in events_dir.glob("*_speed_violations.jsonl"):
            rows.extend(_read_jsonl(path))

    normalized = [
        _normalize_event(row, run_name=run_name, media_root=media_root)
        for row in rows
    ]

    normalized.sort(
        key=lambda x: float(x.get("created_at") or 0),
        reverse=True,
    )

    return normalized


def _latest_status_by_camera(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}

    for row in rows:
        camera_id = str(row.get("camera_id") or "")
        if not camera_id:
            continue

        latest[camera_id] = row

    return latest


def _preview_url(run_dir: Path, camera_id: str, media_root: str | Path | None) -> str:
    preview_path = run_dir / "previews" / f"{camera_id}.jpg"

    if not preview_path.exists():
        return ""

    return _media_relative_url(str(preview_path), media_root)


def build_speed_dashboard_report(
    run_dir: str | Path,
    cameras: list[dict[str, Any]],
    process_alive: bool,
    pid: int | None,
    started_at: float | None,
    return_code: int | None,
    media_root: str | Path | None = None,
    max_events: int = 200,
    max_status: int = 500,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    run_name = run_dir.name

    config = _read_json(run_dir / "run_config.json")

    status_rows = _read_jsonl(run_dir / "camera_status.jsonl", limit=max_status)
    latest_status = _latest_status_by_camera(status_rows)

    events = _collect_events_from_run(
        run_dir=run_dir,
        run_name=run_name,
        media_root=media_root,
    )[:max_events]

    camera_cards = []

    for cam in cameras:
        camera_id = str(cam.get("camera_id") or "")
        status = latest_status.get(camera_id, {})

        camera_cards.append(
            {
                "camera_id": camera_id,
                "name": cam.get("name") or camera_id,
                "source": cam.get("source") or "",
                "faculty": cam.get("faculty") or "",
                "speed_limit_kmh": cam.get("speed_limit_kmh"),
                "tolerance_kmh": cam.get("tolerance_kmh"),

                "stage": status.get("stage", "-"),
                "detail": status.get("detail", "-"),
                "last_ts": _format_ts(status.get("ts") or status.get("created_at")),
                "frame_idx": status.get("frame_idx", "-"),
                "fps": status.get("fps", "-"),
                "tracks": status.get("tracks", 0),
                "motion_active": status.get("motion_active", "-"),
                "latest_speed_kmh": status.get("latest_speed_kmh", "-"),
                "latest_violation": status.get("latest_violation", "-"),

                "preview_url": _preview_url(run_dir, camera_id, media_root),
            }
        )

    return {
        "running": bool(process_alive),
        "pid": pid,
        "return_code": return_code,
        "started_at": started_at,
        "started_at_text": _format_ts(started_at),
        "run_dir": str(run_dir),
        "run_name": run_name,
        "source_count": len(cameras),
        "camera_count": len(cameras),
        "cameras": camera_cards,
        "events": events,
        "recent_status": status_rows[-max_status:],
        "config": config,
    }