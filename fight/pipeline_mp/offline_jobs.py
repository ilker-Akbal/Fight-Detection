"""Parent-owned one-shot FILE jobs. Durable state, never health/telemetry, is proof.

The Django registry writes one immutable request at a time. A claim is persisted
before opening media. A different parent can fail an interrupted claim, not replay it.
"""
import json
import queue
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fight.operations import atomic_json


@dataclass
class OfflineDrain:
    camera_id: str
    slot_id: int
    generation: int
    consumer_epoch: int
    service_epoch: int = 0


def drain_ack_path(config, camera_id):
    if not camera_id.startswith("offline_"):
        raise ValueError("invalid offline identity")
    uuid.UUID(hex=camera_id[8:])
    return Path(config["output_dir"]) / "offline_drain" / (camera_id + ".json")


def record_failure(config, camera_id, generation, reason):
    if camera_id.startswith("offline_"):
        atomic_json(drain_ack_path(config, camera_id).with_suffix(".failed.json"),
                    {"generation": generation, "reason": reason})


class OfflineJobs:
    def __init__(self, root, config, manager, services, incident_queue):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config, self.manager, self.services = config, manager, services
        self.incident_queue = incident_queue
        self.owner = uuid.uuid4().hex
        self.active = None
        self.sent = False

    def _write(self, state, error=""):
        self.active.update(state=state, error=error, updated_at=time.time())
        atomic_json(self.root / (self.active["camera"]["camera_id"] + ".json"), self.active)

    def cameras(self):
        return [self.active["camera"]] if self.active and self.active["state"] == "PROCESSING" else []

    def tick(self, live_cameras):
        request_path = self.root / "request.json"
        request = json.loads(request_path.read_text(encoding="utf-8")) if request_path.exists() else None
        if self.active and self.active["state"] == "PROCESSING":
            cid = self.active["camera"]["camera_id"]
            item = self.manager.runtimes.get(cid)
            failure = drain_ack_path(self.config, cid).with_suffix(".failed.json")
            failure_row = json.loads(failure.read_text(encoding="utf-8")) if failure.exists() else {}
            evidence_failed = (failure_row.get("generation") == getattr(item, "generation", None)
                               and failure_row.get("reason") == "evidence_write_failed")
            if not request or request.get("camera", {}).get("camera_id") != cid or request.get("cancel"):
                self._write("CANCELLED")
            elif item is None or item.fight_failed or item.speed_failed or item.state == "FAILED":
                self._write("FAILED", "required_consumer_failed")
            elif failure.exists() and not evidence_failed:
                self._write("FAILED", "offline_processing_failed")
            elif item.file_done:
                if not self.manager.file_eof_reached(item):
                    self._write("FAILED", "missing_authoritative_eof")
                elif not self.sent:
                    marker = OfflineDrain(cid, item.slot_id, item.generation, item.fight_epoch)
                    channel = self.manager.stage3_queue if item.camera["use_fight_detection"] else self.incident_queue
                    try:
                        channel.put(marker, block=False)
                        self.sent = True
                    except queue.Full:
                        pass
                elif drain_ack_path(self.config, cid).exists():
                    ack = json.loads(drain_ack_path(self.config, cid).read_text(encoding="utf-8"))
                    if (ack.get("generation") == item.generation
                            and ack.get("consumer_epoch") == item.fight_epoch):
                        self.active["outbox_offset"] = ack["outbox_offset"]
                        if evidence_failed:
                            self._write("FAILED", "evidence_write_failed")
                        else:
                            self._write("COMPLETED")
            if self.active["state"] != "PROCESSING":
                self.manager.stop_camera(cid, reason="offline_terminal")
                self.services.prepare(live_cameras)
            return
        if not request or request.get("cancel") or not self.manager._free_slots:
            return
        camera = request["camera"]
        cid = camera["camera_id"]
        drain_ack_path(self.config, cid)  # validate bounded identifier
        state_path = self.root / (cid + ".json")
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state["state"] == "PROCESSING" and state["owner"] != self.owner:
                self.active = state
                self._write("FAILED", "runtime_interrupted")
            return
        self.active = {"camera": camera, "owner": self.owner, "started_at": time.time()}
        self.sent = False
        self._write("PROCESSING")  # durability before any model/source launch
        if not Path(camera["source"]).is_file():
            self._write("FAILED", "asset_missing")
            return
        try:
            self.services.prepare(live_cameras + [camera])
            self.manager.start_camera(camera, reason="offline_requested")
        except Exception:
            self._write("FAILED", "offline_start_failed")
            raise  # ownership/teardown failures must remain visible

    def close(self):
        if self.active and self.active["state"] == "PROCESSING":
            self._write("FAILED", "runtime_stopped")
