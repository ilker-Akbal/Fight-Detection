from __future__ import annotations

import csv
import json
import queue
import time
from pathlib import Path

from fight.pipeline_mp.common import now_str
from fight.pipeline_mp.messages import ReportMessage


class BufferedJsonlWriter:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, row: dict) -> None:
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass


class BufferedCsvWriter:
    def __init__(self, path: Path, fieldnames: list[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fieldnames = list(fieldnames)
        exists = self.path.exists()

        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.fieldnames)

        if not exists:
            self._writer.writeheader()

    def write(self, row: dict) -> None:
        self._writer.writerow({k: row.get(k, "") for k in self.fieldnames})

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass


def reporter_process_main(config: dict, report_queue, stop_event) -> None:
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    flush_interval = float(config.get("runtime", {}).get("report_flush_interval_sec", 0.25))

    events_jsonl = BufferedJsonlWriter(output_dir / "events.jsonl")
    stage3_jsonl = BufferedJsonlWriter(output_dir / "stage3_results.jsonl")
    status_jsonl = BufferedJsonlWriter(output_dir / "camera_status.jsonl")

    events_csv = BufferedCsvWriter(
        output_dir / "events.csv",
        [
            "camera_id",
            "ip",
            "event_id",
            "event_start",
            "event_end",
            "duration_sec",
            "status",
            "pose_score_max",
            "pose_score_mean",
            "positive_hits",
            "clip_path",
            "queue_status",
            "queue_reason",
            "frames",
            "closed_at",
        ],
    )

    stage3_csv = BufferedCsvWriter(
        output_dir / "stage3_results.csv",
        [
            "camera_id",
            "ip",
            "event_id",
            "event_start",
            "event_end",
            "clip_path",
            "fight_prob",
            "fight_label",
            "pose_score_max",
            "pose_score_mean",
            "queued_at",
            "processed_at",
        ],
    )

    def flush_all() -> None:
        status_jsonl.flush()
        events_jsonl.flush()
        stage3_jsonl.flush()
        events_csv.flush()
        stage3_csv.flush()

    try:
        last_flush = time.time()

        status_jsonl.write(
            {
                "ts": now_str(),
                "camera_id": "__system__",
                "stage": "reporter",
                "detail": "started",
            }
        )
        flush_all()

        while not stop_event.is_set() or not report_queue.empty():
            try:
                msg = report_queue.get(timeout=0.2)
            except queue.Empty:
                if time.time() - last_flush >= flush_interval:
                    flush_all()
                    last_flush = time.time()
                continue

            try:
                if msg is None:
                    break

                if isinstance(msg, ReportMessage):
                    kind = msg.kind
                    row = msg.row
                elif isinstance(msg, dict):
                    kind = msg.get("kind")
                    row = msg.get("row", {})
                else:
                    continue

                if kind == "status":
                    status_jsonl.write(row)
                elif kind == "event":
                    events_jsonl.write(row)
                    events_csv.write(row)
                elif kind == "stage3":
                    stage3_jsonl.write(row)
                    stage3_csv.write(row)
                else:
                    status_jsonl.write(
                        {
                            "ts": now_str(),
                            "camera_id": "__system__",
                            "stage": "reporter",
                            "detail": "unknown_message_kind",
                            "kind": str(kind),
                        }
                    )

            except Exception as exc:
                status_jsonl.write(
                    {
                        "ts": now_str(),
                        "camera_id": "__system__",
                        "stage": "reporter",
                        "detail": "write_error",
                        "error": str(exc),
                    }
                )

            finally:
                try:
                    report_queue.task_done()
                except Exception:
                    pass

            if time.time() - last_flush >= flush_interval:
                flush_all()
                last_flush = time.time()

    finally:
        try:
            status_jsonl.write(
                {
                    "ts": now_str(),
                    "camera_id": "__system__",
                    "stage": "reporter",
                    "detail": "stopped",
                }
            )
        except Exception:
            pass

        flush_all()
        events_jsonl.close()
        stage3_jsonl.close()
        status_jsonl.close()
        events_csv.close()
        stage3_csv.close()