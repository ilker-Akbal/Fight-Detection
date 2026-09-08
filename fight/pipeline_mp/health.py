from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fight.pipeline_mp.messages import HealthEvent, ReportMessage


HEALTHY = "HEALTHY"
STARTING = "STARTING"
ONLINE = "ONLINE"
DEGRADED = "DEGRADED"
RECONNECTING = "RECONNECTING"
OFFLINE = "OFFLINE"
STOPPING = "STOPPING"
STOPPED = "STOPPED"
FAILED = "FAILED"
EOF = "EOF"

CAMERA_COMPONENTS = ("camera_ingest", "camera_worker", "camera_preview", "speed_worker")
CRITICAL_SHARED_COMPONENTS = (
    "person",
    "person_router",
    "pose",
    "pose_router",
    "stage3",
    "incident",
)
KNOWN_EVENTS = {
    "heartbeat",
    "process_started",
    "process_stopping",
    "process_error",
    "frame_progress",
    "frame_consumed",
    "preview_published",
    "person_request",
    "pose_request",
    "event_generated",
    "source_online",
    "source_offline",
    "reconnecting",
    "eof",
    "request_received",
    "inference_completed",
    "result_delivered",
    "work_received",
    "work_completed",
    "result_produced",
    "capacity_wait",
    "live_shed",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class HealthPolicy:
    capacity_overload_ratio: float = 0.9
    startup_grace_sec: float = 120.0
    camera_heartbeat_timeout_sec: float = 20.0
    camera_frame_stall_warn_sec: float = 20.0
    camera_frame_stall_fail_sec: float = 90.0
    camera_reconnect_grace_sec: float = 120.0
    shared_worker_heartbeat_timeout_sec: float = 60.0
    inference_stall_warn_sec: float = 30.0
    inference_stall_fail_sec: float = 120.0
    preview_heartbeat_timeout_sec: float = 30.0
    watchdog_camera_restart_cooldown_sec: float = 120.0
    watchdog_camera_restart_limit: int = 3

    @classmethod
    def from_runtime(cls, runtime: dict) -> "HealthPolicy":
        def seconds(name: str, default: float) -> float:
            return max(0.1, float(runtime.get(name, default)))

        return cls(
            capacity_overload_ratio=max(0.1, min(1.0, float(runtime.get("capacity_overload_ratio", 0.9)))),
            startup_grace_sec=seconds("health_startup_grace_sec", 120.0),
            camera_heartbeat_timeout_sec=seconds(
                "camera_heartbeat_timeout_sec", 20.0
            ),
            camera_frame_stall_warn_sec=seconds(
                "camera_frame_stall_warn_sec", 20.0
            ),
            camera_frame_stall_fail_sec=seconds(
                "camera_frame_stall_fail_sec", 90.0
            ),
            camera_reconnect_grace_sec=seconds(
                "camera_reconnect_grace_sec", 120.0
            ),
            shared_worker_heartbeat_timeout_sec=seconds(
                "shared_worker_heartbeat_timeout_sec", 60.0
            ),
            inference_stall_warn_sec=seconds("inference_stall_warn_sec", 30.0),
            inference_stall_fail_sec=seconds("inference_stall_fail_sec", 120.0),
            preview_heartbeat_timeout_sec=seconds(
                "preview_heartbeat_timeout_sec", 30.0
            ),
            watchdog_camera_restart_cooldown_sec=seconds(
                "watchdog_camera_restart_cooldown_sec", 120.0
            ),
            watchdog_camera_restart_limit=max(
                0, int(runtime.get("watchdog_camera_restart_limit", 3))
            ),
        )


class HealthEmitter:
    """Best-effort, throttled producer for the bounded runtime health channel."""

    def __init__(
        self,
        health_queue,
        *,
        component: str,
        component_type: str,
        camera_id: str = "",
        slot_id: int = -1,
        generation: int = -1,
        consumer_epoch: int = 0,
        interval_sec: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.health_queue = health_queue
        self.component = str(component)
        self.component_type = str(component_type)
        self.camera_id = str(camera_id)
        self.slot_id = int(slot_id)
        self.generation = int(generation)
        self.consumer_epoch = int(consumer_epoch)
        self.interval_sec = max(0.1, float(interval_sec))
        self.monotonic = monotonic
        self._last_emit: dict[str, float] = {}

    def emit(
        self,
        event_type: str,
        *,
        force: bool = False,
        progress: int = 0,
        secondary_progress: int = 0,
        queue_depth: int = -1,
        dropped: int = 0,
        reconnect_count: int = 0,
        detail: str = "",
    ) -> bool:
        if self.health_queue is None or event_type not in KNOWN_EVENTS:
            return False
        now = float(self.monotonic())
        if not force and now - self._last_emit.get(event_type, -1e30) < self.interval_sec:
            return False
        event = HealthEvent(
            component=self.component,
            component_type=self.component_type,
            event_type=event_type,
            monotonic_ts=now,
            camera_id=self.camera_id,
            slot_id=self.slot_id,
            generation=self.generation,
            consumer_epoch=self.consumer_epoch,
            progress=int(progress),
            secondary_progress=int(secondary_progress),
            queue_depth=int(queue_depth),
            dropped=int(dropped),
            reconnect_count=int(reconnect_count),
            detail=str(detail)[:80],
        )
        try:
            self.health_queue.put_nowait(event)
            self._last_emit[event_type] = now
            return True
        except Exception:
            return False

    def heartbeat(self, **metrics) -> bool:
        return self.emit("heartbeat", **metrics)


def _new_record(
    component: str,
    component_type: str,
    now: float,
    *,
    camera_id: str = "",
    slot_id: int = -1,
    generation: int = -1,
) -> dict:
    return {
        "component": component,
        "component_type": component_type,
        "camera_id": camera_id,
        "slot_id": int(slot_id),
        "generation": int(generation),
        "registered_at": float(now),
        "last_heartbeat": float(now),
        "last_event": "registered",
        "last_event_at": float(now),
        "last_request": 0.0,
        "last_completed": 0.0,
        "last_result": 0.0,
        "first_frame": 0.0,
        "last_frame": 0.0,
        "last_fight_publish": 0.0,
        "last_preview": 0.0,
        "last_person_request": 0.0,
        "last_pose_request": 0.0,
        "last_event_generated": 0.0,
        "last_reconnect": 0.0,
        "source_state": "STARTING",
        "capacity_stage": "",
        "capacity": {},
        "progress": 0,
        "secondary_progress": 0,
        "queue_depth": -1,
        "dropped": 0,
        "reconnect_count": 0,
        "health": STARTING,
        "reason": "startup_grace",
    }


class HealthRegistry:
    """Runtime-parent current-state registry; it never retains heartbeat history."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        transition_limit: int = 128,
        max_cameras: int = 512,
    ):
        self.monotonic = monotonic
        self.max_cameras = max(1, int(max_cameras))
        self.cameras: dict[str, dict] = {}
        self.workers: dict[str, dict] = {}
        self.transitions = deque(maxlen=max(1, int(transition_limit)))
        self.runtime_health = STARTING
        self.runtime_reason = "startup_grace"
        self.disk = {}

    def register_worker(self, component: str) -> None:
        now = self.monotonic()
        self.workers[component] = _new_record(component, "shared_worker", now)

    def register_camera(
        self, camera_id: str, slot_id: int, generation: int, lifecycle: str
    ) -> None:
        cid = str(camera_id)
        current = self.cameras.get(cid)
        identity = (int(slot_id), int(generation))
        if current and (current["slot_id"], current["generation"]) == identity:
            current["lifecycle"] = str(lifecycle)
            return
        if len(self.cameras) >= self.max_cameras and cid not in self.cameras:
            return
        now = self.monotonic()
        self.cameras[cid] = {
            "camera_id": cid,
            "slot_id": identity[0],
            "generation": identity[1],
            "registered_at": now,
            "lifecycle": str(lifecycle),
            "file_done": False,
            "restart_count": 0,
            "health": STARTING,
            "reason": "startup_grace",
            "components": {
                name: _new_record(
                    name,
                    "camera",
                    now,
                    camera_id=cid,
                    slot_id=identity[0],
                    generation=identity[1],
                )
                for name in CAMERA_COMPONENTS
            },
        }

    def sync_cameras(self, statuses: dict[str, dict]) -> None:
        active = set(statuses)
        for cid in set(self.cameras) - active:
            del self.cameras[cid]
        for cid, status in statuses.items():
            slot_id = status.get("slot_id")
            generation = status.get("generation")
            if slot_id is None or generation is None:
                continue
            self.register_camera(cid, slot_id, generation, status.get("state", STARTING))
            camera = self.cameras[cid]
            camera["lifecycle"] = str(status.get("state", STARTING))
            camera["file_done"] = bool(status.get("file_done", False))
            camera["restart_count"] = int(status.get("restart_count", 0) or 0)
            for key in ("use_fight_detection", "use_speed_detection", "speed_failed", "speed_restarts"):
                camera[key] = status.get(key, key == "use_fight_detection")
            if camera.get("speed_epoch") != status.get("speed_epoch"):
                camera["speed_epoch"] = status.get("speed_epoch")
                camera["components"]["speed_worker"] = _new_record("speed_worker", "camera", self.monotonic())

    def handle(self, event: HealthEvent) -> bool:
        if not isinstance(event, HealthEvent) or event.event_type not in KNOWN_EVENTS:
            return False
        if event.component_type == "camera":
            camera = self.cameras.get(str(event.camera_id))
            if camera is None:
                return False
            if (
                int(event.slot_id) != int(camera["slot_id"])
                or int(event.generation) != int(camera["generation"])
            ):
                return False
            record = camera["components"].get(event.component)
            if record is None:
                return False
            if event.component == "speed_worker" and event.consumer_epoch != camera.get("speed_epoch", 0):
                return False
        else:
            record = self.workers.get(event.component)
            if record is None:
                return False
            if event.service_epoch != record.get("service_epoch", 0):
                return False

        ts = float(event.monotonic_ts)
        if ts < float(record["registered_at"]) or ts < float(record["last_event_at"]):
            return False
        record["last_heartbeat"] = max(float(record["last_heartbeat"]), ts)
        record["last_event"] = event.event_type
        record["last_event_at"] = max(float(record["last_event_at"]), ts)
        record["progress"] = max(int(record["progress"]), int(event.progress))
        previous_secondary = int(record["secondary_progress"])
        record["secondary_progress"] = max(
            int(record["secondary_progress"]), int(event.secondary_progress)
        )
        record["queue_depth"] = int(event.queue_depth)
        record["dropped"] = max(int(record["dropped"]), int(event.dropped))
        record["reconnect_count"] = max(
            int(record["reconnect_count"]), int(event.reconnect_count)
        )
        if event.event_type in {"request_received", "work_received"}:
            record["last_request"] = ts
        elif event.event_type in {"inference_completed", "work_completed"}:
            record["last_completed"] = ts
        elif event.event_type in {"result_delivered", "result_produced"}:
            record["last_result"] = ts
        elif event.event_type == "frame_progress":
            if float(record["first_frame"]) <= 0.0:
                record["first_frame"] = ts
            record["last_frame"] = ts
            record["source_state"] = ONLINE
            if int(event.secondary_progress) > previous_secondary:
                record["last_fight_publish"] = ts
        elif event.event_type == "frame_consumed":
            record["last_frame"] = ts
        elif event.event_type == "preview_published":
            record["last_preview"] = ts
        elif event.event_type == "person_request":
            record["last_person_request"] = ts
        elif event.event_type == "pose_request":
            record["last_pose_request"] = ts
        elif event.event_type == "event_generated":
            record["last_event_generated"] = ts
        elif event.event_type == "source_online":
            record["source_state"] = ONLINE
        elif event.event_type in {"source_offline", "process_error"}:
            record["source_state"] = OFFLINE
        elif event.event_type == "reconnecting":
            if record["source_state"] != RECONNECTING:
                record["last_reconnect"] = ts
            record["source_state"] = RECONNECTING
        elif event.event_type == "eof":
            record["source_state"] = EOF
        elif event.event_type == "capacity_wait":
            record["capacity_stage"] = event.detail if event.detail in {"person", "pose", "stage3", "vehicle"} else ""
        return True

    def drain(self, health_queue, limit: int = 2048) -> int:
        accepted = 0
        for _ in range(max(1, int(limit))):
            try:
                event = health_queue.get_nowait()
            except Exception:
                break
            if self.handle(event):
                accepted += 1
        return accepted

    def _transition(self, subject: str, record: dict, health: str, reason: str) -> None:
        previous = str(record.get("health", STARTING))
        previous_reason = str(record.get("reason", ""))
        record["health"] = health
        record["reason"] = reason
        if (previous, previous_reason) != (health, reason):
            self.transitions.append(
                {
                    "subject": subject,
                    "previous_health": previous,
                    "new_health": health,
                    "reason": reason,
                }
            )

    @staticmethod
    def _pending_inference(record: dict, policy: HealthPolicy) -> tuple[float, float]:
        # Shared workers execute synchronously: a completion/result after the
        # latest request closes that work, even if one health event was dropped.
        finished_at = max(float(record["last_completed"]), float(record["last_result"]))
        request_at = float(record["last_request"])
        pending_since = request_at if request_at > finished_at else 0.0
        fail_after = policy.inference_stall_fail_sec
        if finished_at <= 0 and record["component"] in {"person", "pose", "stage3", "vehicle"}:
            # Lazy CUDA warm-up starts at the first request, potentially long
            # after process startup. Reuse the existing bounded startup grace.
            fail_after = max(fail_after, policy.startup_grace_sec)
        return pending_since, fail_after

    def _worker_health(
        self, component: str, record: dict, policy: HealthPolicy, now: float, alive: bool
    ) -> tuple[str, str]:
        if not alive:
            return FAILED, "process_dead"
        age = now - float(record["registered_at"])
        pending_since, fail_after = self._pending_inference(record, policy)
        if pending_since:
            stalled = now - pending_since
            if stalled >= fail_after:
                return FAILED, "inference_stall"
            if stalled >= policy.inference_stall_warn_sec:
                return DEGRADED, "inference_stall"
            # A synchronous inference cannot also heartbeat its worker loop.
            return HEALTHY, "inference_in_progress"
        heartbeat_age = now - float(record["last_heartbeat"])
        if heartbeat_age >= policy.shared_worker_heartbeat_timeout_sec:
            if age < policy.startup_grace_sec:
                return STARTING, "startup_grace"
            return FAILED, "heartbeat_timeout"
        if age < policy.startup_grace_sec and record["last_event"] == "registered":
            return STARTING, "startup_grace"
        if record["last_event"] == "capacity_wait":
            return DEGRADED, "queue_pressure"
        return HEALTHY, "heartbeat_fresh"

    def _camera_health(
        self, camera: dict, policy: HealthPolicy, now: float, process_alive: dict
    ) -> tuple[str, str, str | None]:
        lifecycle = camera["lifecycle"]
        if camera["file_done"]:
            return EOF, "file_eof", None
        if lifecycle == STOPPING:
            return STOPPING, "intentional_stop", None
        if lifecycle == STOPPED:
            return STOPPED, "intentional_stop", None
        if lifecycle == FAILED:
            return FAILED, "lifecycle_failed", None
        age = now - float(camera["registered_at"])
        components = camera["components"]
        ingest = components["camera_ingest"]
        worker = components["camera_worker"]
        if not camera.get("use_fight_detection", True):
            worker = ingest
        preview = components["camera_preview"]
        ingest_eof = ingest["source_state"] == EOF
        if not ingest_eof and not process_alive.get("ingest", True):
            return FAILED, "process_dead", "restart_camera"
        if camera.get("use_fight_detection", True) and not process_alive.get("camera", True):
            return FAILED, "process_dead", "restart_camera"
        if not ingest_eof and not process_alive.get("preview", True):
            return DEGRADED, "preview_process_dead", "restart_preview"
        reconnect_age = now - float(ingest["last_reconnect"])
        if (
            ingest["source_state"] == RECONNECTING
            and reconnect_age <= policy.camera_reconnect_grace_sec
        ):
            return RECONNECTING, "source_reconnecting", None
        if age < policy.startup_grace_sec and float(ingest["last_frame"]) <= 0.0:
            return STARTING, "startup_grace", None
        if (camera.get("use_speed_detection") and ingest["last_event"] == "capacity_wait"
                and ingest["capacity_stage"] == "vehicle"
                and now - float(ingest["last_heartbeat"]) < policy.camera_heartbeat_timeout_sec):
            return DEGRADED, "queue_pressure", None
        # A blocked camera consumer can also fill an ordered file-ingest queue.
        # Exempt only cameras whose latest signal is a request to this pending
        # shared worker; unrelated cameras and actual process deaths still fail.
        pending_component = {
            "person_request": "person", "pose_request": "pose"
        }.get(worker["last_event"])
        shared = self.workers.get(pending_component)
        waiting_on_inference = False
        if worker["last_event"] == "capacity_wait":
            shared = self.workers.get(worker["capacity_stage"])
            if (shared is not None and shared["health"] != FAILED
                    and now - float(worker["last_heartbeat"]) < policy.camera_heartbeat_timeout_sec):
                return DEGRADED, "queue_pressure", None
        if shared is not None and shared["health"] != FAILED:
            pending_since, fail_after = self._pending_inference(shared, policy)
            waiting_on_inference = bool(
                pending_since
                and now - pending_since < fail_after
                and now - float(worker["last_event_at"]) < fail_after
            )
        ingest_heartbeat_age = now - float(ingest["last_heartbeat"])
        worker_heartbeat_age = now - float(worker["last_heartbeat"])
        frame_at = float(ingest["last_frame"])
        frame_age = now - (frame_at if frame_at > 0 else float(camera["registered_at"]))
        if ingest_eof:
            ingest_heartbeat_age = frame_age = 0.0
        first_ingest_frame = float(ingest["first_frame"])
        worker_frame_at = float(worker["last_frame"])
        worker_frame_age = (
            now - (worker_frame_at if worker_frame_at > 0 else first_ingest_frame)
            if first_ingest_frame > 0
            else 0.0
        )
        if (
            ingest_heartbeat_age >= policy.camera_frame_stall_fail_sec
            or frame_age >= policy.camera_frame_stall_fail_sec
            or worker_heartbeat_age >= policy.camera_frame_stall_fail_sec
            or worker_frame_age >= policy.camera_frame_stall_fail_sec
        ):
            if waiting_on_inference:
                return DEGRADED, "shared_inference_in_progress", None
            return FAILED, "frame_stall", "restart_camera"
        if (
            ingest_heartbeat_age >= policy.camera_heartbeat_timeout_sec
            or worker_heartbeat_age >= policy.camera_heartbeat_timeout_sec
            or frame_age >= policy.camera_frame_stall_warn_sec
            or worker_frame_age >= policy.camera_frame_stall_warn_sec
        ):
            reason = "shared_inference_in_progress" if waiting_on_inference else "frame_stall"
            return DEGRADED, reason, None
        preview_age = now - float(preview["last_heartbeat"])
        if ingest_eof:
            return ONLINE, "file_draining", None
        preview_publish_at = float(preview["last_preview"])
        preview_publish_age = (
            now - (preview_publish_at if preview_publish_at > 0 else first_ingest_frame)
            if first_ingest_frame > 0
            else 0.0
        )
        if (
            preview_age >= policy.preview_heartbeat_timeout_sec
            or preview_publish_age >= policy.preview_heartbeat_timeout_sec
        ):
            return DEGRADED, "preview_heartbeat_timeout", "restart_preview"
        return ONLINE, "frames_progressing", None

    def evaluate(
        self,
        policy: HealthPolicy,
        *,
        now: float | None = None,
        camera_process_alive: dict[str, dict] | None = None,
        worker_process_alive: dict[str, bool] | None = None,
    ) -> tuple[list[dict], list[dict]]:
        current = self.monotonic() if now is None else float(now)
        camera_process_alive = camera_process_alive or {}
        worker_process_alive = worker_process_alive or {}
        actions: list[dict] = []
        for component, record in self.workers.items():
            service_state = record.get("service_state")
            if service_state == "disabled" and not record.get("required", True):
                health, reason = HEALTHY, "service_disabled"
            elif service_state == "restarting":
                health, reason = DEGRADED, "service_restarting"
            elif service_state == "failed":
                health, reason = FAILED, "service_recovery_exhausted"
            else:
                health, reason = self._worker_health(
                    component, record, policy, current,
                    worker_process_alive.get(component, True),
                )
            capacity = record.get("capacity", {})
            if (health == HEALTHY and service_state != "disabled" and capacity.get("capacity", 0) > 0
                    and capacity.get("outstanding", 0) >=
                    capacity["capacity"] * policy.capacity_overload_ratio):
                health, reason = DEGRADED, "queue_pressure"
            self._transition(component, record, health, reason)
        for cid, camera in self.cameras.items():
            health, reason, action = self._camera_health(
                camera,
                policy,
                current,
                camera_process_alive.get(cid, {}),
            )
            if camera.get("use_speed_detection"):
                speed = camera["components"]["speed_worker"]
                if camera.get("speed_failed"):
                    if action is None:
                        health, reason = DEGRADED, "speed_consumer_failed"
                elif (not camera["file_done"] and speed["source_state"] != EOF
                      and current - max(float(speed["last_heartbeat"]), float(speed["registered_at"]))
                      >= max(policy.startup_grace_sec, policy.inference_stall_fail_sec)):
                    if action is None:
                        health, reason, action = DEGRADED, "speed_consumer_stall", "restart_speed"
            self._transition(cid, camera, health, reason)
            if action:
                actions.append({"action": action, "camera_id": cid, "reason": reason})

        if any(record["health"] == FAILED for name, record in self.workers.items() if name != "vehicle"):
            aggregate, aggregate_reason = FAILED, "critical_shared_worker_unhealthy"
        elif any(
            camera["health"] in {DEGRADED, RECONNECTING, OFFLINE, FAILED}
            for camera in self.cameras.values()
        ) or any(record["health"] in {STARTING, DEGRADED, FAILED} for record in self.workers.values()):
            aggregate, aggregate_reason = DEGRADED, "component_degraded"
        else:
            aggregate, aggregate_reason = HEALTHY, "critical_components_healthy"
        if aggregate == HEALTHY and self.disk.get("state") in {"WARNING", "CRITICAL", "UNKNOWN"}:
            aggregate, aggregate_reason = DEGRADED, "disk_pressure"
        if (self.runtime_health, self.runtime_reason) != (aggregate, aggregate_reason):
            self.transitions.append(
                {
                    "subject": "runtime",
                    "previous_health": self.runtime_health,
                    "new_health": aggregate,
                    "reason": aggregate_reason,
                }
            )
        self.runtime_health = aggregate
        self.runtime_reason = aggregate_reason
        transitions = list(self.transitions)
        self.transitions.clear()
        return actions, transitions

    @staticmethod
    def _age(now: float, value: float) -> float | None:
        return round(max(0.0, now - value), 3) if value > 0.0 else None

    def snapshot(self, run_id: str, *, now: float | None = None) -> dict:
        current = self.monotonic() if now is None else float(now)
        cameras = {}
        for cid, camera in sorted(self.cameras.items()):
            ingest = camera["components"]["camera_ingest"]
            worker = camera["components"]["camera_worker"]
            preview = camera["components"]["camera_preview"]
            cameras[cid] = {
                "generation": camera["generation"],
                "slot_id": camera["slot_id"],
                "lifecycle": camera["lifecycle"],
                "health": camera["health"],
                "reason": camera["reason"],
                "last_ingest_heartbeat_age_sec": self._age(
                    current, ingest["last_heartbeat"]
                ),
                "last_frame_age_sec": self._age(current, ingest["last_frame"]),
                "last_fight_publish_age_sec": self._age(
                    current, ingest["last_fight_publish"]
                ),
                "last_worker_heartbeat_age_sec": self._age(
                    current, worker["last_heartbeat"]
                ),
                "last_frame_consumed_age_sec": self._age(
                    current, worker["last_frame"]
                ),
                "last_person_request_age_sec": self._age(
                    current, worker["last_person_request"]
                ),
                "last_pose_request_age_sec": self._age(
                    current, worker["last_pose_request"]
                ),
                "last_event_generated_age_sec": self._age(
                    current, worker["last_event_generated"]
                ),
                "last_preview_publish_age_sec": self._age(
                    current, preview["last_preview"]
                ),
                "reconnect_count": ingest["reconnect_count"],
                "frames_dropped": ingest["dropped"],
                "restart_count": camera["restart_count"],
                "source_state": ingest["source_state"],
                "capacity": camera.get("capacity", {}),
                "speed": {"enabled": camera.get("use_speed_detection", False),
                          "failed": camera.get("speed_failed", False),
                          "restarts": camera.get("speed_restarts", 0),
                          "epoch": camera.get("speed_epoch"),
                          "progress": camera["components"]["speed_worker"]["progress"],
                          "dropped": camera["components"]["speed_worker"]["dropped"],
                          "heartbeat_age_sec": self._age(current, camera["components"]["speed_worker"]["last_heartbeat"])},
            }
        workers = {
            name: {
                "health": record["health"],
                "reason": record["reason"],
                "heartbeat_age_sec": self._age(current, record["last_heartbeat"]),
                "last_request_age_sec": self._age(current, record["last_request"]),
                "last_completed_age_sec": self._age(current, record["last_completed"]),
                "last_result_age_sec": self._age(current, record["last_result"]),
                "progress": record["progress"],
                "queue_depth": record["queue_depth"],
                "dropped": record["dropped"],
                "capacity": record.get("capacity", {}),
                "required": record.get("required", True),
                "service_state": record.get("service_state", "running"),
                "service_epoch": record.get("service_epoch", 0),
                "restart_count": record.get("restart_count", 0),
            }
            for name, record in sorted(self.workers.items())
        }
        return {
            "schema_version": 1,
            "run_id": str(run_id),
            "runtime_health": self.runtime_health,
            "disk": self.disk,
            "reason": self.runtime_reason,
            "updated_at": utc_now(),
            "written_wall_time": time.time(),
            "camera_count": len(cameras),
            "worker_count": len(workers),
            "cameras": cameras,
            "workers": workers,
        }


class HealthSnapshotStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def write(self, snapshot: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class RuntimeWatchdog:
    def __init__(
        self,
        registry: HealthRegistry,
        policy: HealthPolicy,
        report_queue,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.registry = registry
        self.policy = policy
        self.report_queue = report_queue
        self.monotonic = monotonic
        self._restart_state: dict[str, dict] = {}
        self._preview_restart_at: dict[str, float] = {}

    def _report(self, detail: str, **extra) -> None:
        row = {
            "ts": utc_now(),
            "camera_id": extra.pop("camera_id", "__system__"),
            "stage": "health_watchdog",
            "detail": detail,
        }
        row.update(extra)
        try:
            self.report_queue.put_nowait(ReportMessage(kind="status", row=row))
        except Exception:
            pass

    @staticmethod
    def _alive(process) -> bool:
        try:
            return bool(process is not None and process.is_alive())
        except Exception:
            return False

    def tick(self, manager, shared_processes: dict[str, object]) -> bool:
        statuses = manager.get_camera_status()
        for cid in set(self._restart_state) - set(statuses):
            self._restart_state.pop(cid, None)
            self._preview_restart_at.pop(cid, None)
        self.registry.sync_cameras(statuses)
        camera_alive = {}
        for cid, runtime in manager.runtimes.items():
            camera_alive[cid] = {
                name: self._alive(runtime.processes.get(name))
                for name in ("ingest", "camera", "preview")
            }
        worker_alive = {
            component: self._alive(process)
            for component, process in shared_processes.items()
        }
        actions, transitions = self.registry.evaluate(
            self.policy,
            camera_process_alive=camera_alive,
            worker_process_alive=worker_alive,
        )
        for transition in transitions:
            subject = transition.pop("subject")
            camera = self.registry.cameras.get(subject)
            detail = (
                "runtime_health_changed"
                if subject == "runtime"
                else "camera_health_changed"
                if subject in self.registry.cameras
                else "component_health_changed"
            )
            self._report(
                detail,
                camera_id=subject if subject in self.registry.cameras else "__system__",
                component=subject,
                generation=camera["generation"] if camera is not None else -1,
                restart_count=camera["restart_count"] if camera is not None else 0,
                **transition,
            )
        if self.registry.runtime_health == FAILED:
            self._report(
                "shared_worker_unhealthy",
                new_health=FAILED,
                reason=self.registry.runtime_reason,
            )
            return True

        now = self.monotonic()
        for action in actions:
            cid = action["camera_id"]
            runtime = manager.runtimes.get(cid)
            if runtime is None or runtime.intentional_stop or runtime.file_done:
                continue
            if action["action"] == "restart_speed":
                manager.disable_speed(runtime, action["reason"])
                continue
            if action["action"] == "restart_preview":
                if (
                    now - self._preview_restart_at.get(cid, -1e30)
                    < self.policy.watchdog_camera_restart_cooldown_sec
                ):
                    continue
                self._preview_restart_at[cid] = now
                manager.restart_preview(cid, reason=action["reason"])
                continue
            state = self._restart_state.setdefault(
                cid,
                {"generation": runtime.generation, "count": 0, "last": -1e30},
            )
            if state["generation"] != runtime.generation:
                state.update(generation=runtime.generation, count=0, last=-1e30)
            if state["count"] >= self.policy.watchdog_camera_restart_limit:
                runtime.state = FAILED
                self._report(
                    "camera_watchdog_restart_suppressed",
                    camera_id=cid,
                    generation=runtime.generation,
                    reason="restart_limit",
                    restart_count=state["count"],
                )
                continue
            if now - state["last"] < self.policy.watchdog_camera_restart_cooldown_sec:
                continue
            state["count"] += 1
            state["last"] = now
            self._report(
                "camera_watchdog_restart_requested",
                camera_id=cid,
                generation=runtime.generation,
                reason=action["reason"],
                restart_count=state["count"],
            )
            restarted = manager.restart_camera(
                cid, dict(runtime.camera), reason=action["reason"]
            )
            state["generation"] = restarted.generation
        return False
