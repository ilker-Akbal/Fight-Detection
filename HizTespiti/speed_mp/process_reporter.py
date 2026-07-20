from __future__ import annotations

import json
import queue
import time
from pathlib import Path
from typing import Any


class JsonlWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, row: dict[str, Any]) -> None:
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.flush()
        except Exception:
            pass

        try:
            self._fh.close()
        except Exception:
            pass


def reporter_main(
    report_queue,
    output_dir: str,
    flush_interval_sec: float = 0.25,
) -> None:
    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)

    status_writer = JsonlWriter(output_dir_path / "camera_status.jsonl")
    event_writer = JsonlWriter(output_dir_path / "speed_events.jsonl")

    last_flush = time.time()

    try:
        while True:
            try:
                msg = report_queue.get(timeout=0.2)
            except queue.Empty:
                if time.time() - last_flush >= flush_interval_sec:
                    status_writer.flush()
                    event_writer.flush()
                    last_flush = time.time()
                continue

            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type")

            if msg_type == "stop":
                break

            if msg_type == "status":
                row = dict(msg)
                row.pop("type", None)
                status_writer.write(row)

            elif msg_type == "speed_event":
                event = dict(msg.get("event") or {})
                event_writer.write(event)

            if time.time() - last_flush >= flush_interval_sec:
                status_writer.flush()
                event_writer.flush()
                last_flush = time.time()

    finally:
        status_writer.flush()
        event_writer.flush()
        status_writer.close()
        event_writer.close()