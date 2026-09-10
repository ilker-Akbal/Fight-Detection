from __future__ import annotations

import queue
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fight.pipeline_mp.camera_ingest import camera_ingest_process_main
from fight.pipeline_mp.camera_preview import camera_preview_process_main
from fight.pipeline_mp.camera_worker import camera_process_main
from fight.pipeline_mp.common import is_file_source, now_str, redact_source
from fight.pipeline_mp.generation import set_slot_generation
from fight.pipeline_mp.messages import ReportMessage
from fight.pipeline_mp.speed_worker import speed_process_main


STARTING = "STARTING"
RUNNING = "RUNNING"
RECONNECTING = "RECONNECTING"
STOPPING = "STOPPING"
STOPPED = "STOPPED"
FAILED = "FAILED"


def runtime_camera(camera: dict) -> dict:
    """Canonical camera data used by the runtime; cosmetic fields are retained only."""
    return {
        "camera_id": str(camera.get("camera_id") or "").strip(),
        "source": str(camera.get("source") or "").strip(),
        "name": str(camera.get("name") or camera.get("camera_id") or "").strip(),
        "enabled": bool(camera.get("enabled", True)),
        "use_fight_detection": bool(camera.get("use_fight_detection", True)),
        "use_speed_detection": bool(camera.get("use_speed_detection", False)),
        "speed_config": dict(camera.get("speed_config") or {}),
    }


def restart_identity(camera: dict) -> tuple[str]:
    item = runtime_camera(camera)
    return (item["source"], item["use_fight_detection"], item["use_speed_detection"],
            json.dumps(item["speed_config"], sort_keys=True))


@dataclass
class CameraRuntime:
    camera: dict
    slot_id: int
    generation: int
    stop_event: Any
    fight_queue: Any
    preview_queue: Any
    processes: dict[str, Any] = field(default_factory=dict)
    state: str = STARTING
    intentional_stop: bool = False
    file_done: bool = False
    file_eof_event: Any = None
    fight_service_waiting: bool = False
    last_restart_at: float = 0.0
    restart_count: int = 0
    speed_queue: Any = None
    speed_stop: Any = None
    speed_epoch: int = 0
    speed_failed: bool = False
    speed_service_waiting: bool = False
    speed_restarts: int = 0
    speed_last_restart: float = 0

    @property
    def camera_id(self) -> str:
        return str(self.camera["camera_id"])


class CameraRuntimeManager:
    """Parent-owned, spawn-safe lifecycle registry for active camera process trios."""

    def __init__(
        self,
        *,
        ctx,
        config: dict,
        stage3_queue,
        report_queue,
        person_request_queue,
        person_result_channels: dict[int, Any],
        pose_request_queue,
        pose_result_channels: dict[int, Any],
        slot_generations,
        health_queue=None,
        vehicle_requests=None,
        vehicle_results=None,
        speed_epochs=None,
        process_factory: Callable | None = None,
        terminate_process: Callable | None = None,
        close_queue: Callable | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.ctx = ctx
        self.config = config
        self.runtime_config = config.get("runtime", {})
        self.stage3_queue = stage3_queue
        self.report_queue = report_queue
        self.person_request_queue = person_request_queue
        self.person_result_channels = person_result_channels
        self.pose_request_queue = pose_request_queue
        self.pose_result_channels = pose_result_channels
        self.slot_generations = slot_generations
        self.health_queue = health_queue
        self.vehicle_requests = vehicle_requests
        self.vehicle_results = vehicle_results or {}
        self.speed_epochs = speed_epochs
        self.speed_service_available = True
        self.fight_service_available = True
        self.process_factory = process_factory or self._default_process_factory
        self.terminate_process = terminate_process or self._default_terminate
        self.close_queue = close_queue or self._default_close_queue
        self.monotonic = monotonic
        self.runtimes: dict[str, CameraRuntime] = {}
        self._free_slots = set(range(len(slot_generations)))
        self._slot_counters = [int(value) for value in slot_generations]
        self._failed_desired: dict[str, dict] = {}
        self._failed_attempts: dict[str, float] = {}
        self._restart_backoff = max(
            0.0, float(self.runtime_config.get("camera_restart_backoff_sec", 3.0))
        )

    def _default_process_factory(self, name: str, target, args: tuple):
        process = self.ctx.Process(name=name, target=target, args=args, daemon=False)
        process.start()
        return process

    @staticmethod
    def _default_terminate(process, timeout: float = 4.0) -> None:
        if process is None:
            return
        try:
            process.join(timeout=max(0.0, timeout))
            if process.is_alive():
                process.terminate()
                process.join(timeout=max(0.0, timeout))
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=1.0)
        except Exception:
            pass

    @staticmethod
    def _default_close_queue(channel) -> None:
        try:
            channel.close()
        except Exception:
            pass
        try:
            channel.join_thread()
        except Exception:
            pass

    def _status(self, detail: str, camera_id: str = "__system__", **extra) -> None:
        row = {
            "ts": now_str(),
            "camera_id": camera_id,
            "stage": "camera_reconciler",
            "detail": detail,
        }
        row.update(extra)
        try:
            self.report_queue.put(ReportMessage(kind="status", row=row), timeout=0.5)
        except Exception:
            pass

    @staticmethod
    def _drain(channel) -> None:
        while True:
            try:
                channel.get_nowait()
            except (queue.Empty, AttributeError):
                return
            except Exception:
                return

    def _next_generation(self, slot_id: int) -> int:
        self._slot_counters[slot_id] += 1
        generation = self._slot_counters[slot_id]
        set_slot_generation(self.slot_generations, slot_id, generation)
        return generation

    def _spawn_trio(self, item: CameraRuntime) -> None:
        cid = item.camera_id
        def admission_port(channel):
            return channel.for_slot(item.slot_id) if hasattr(channel, "for_slot") else channel
        args_common = (self.config, item.camera)
        item.processes["ingest"] = self.process_factory(
            f"camera_ingest_{cid}",
            camera_ingest_process_main,
            args_common
            + (
                item.fight_queue,
                item.preview_queue,
                self.report_queue,
                item.stop_event,
                item.generation,
                self.health_queue,
                item.slot_id,
                item.speed_queue,
                item.speed_stop,
                item.file_eof_event,
            ),
        )
        item.processes["preview"] = self.process_factory(
            f"camera_preview_{cid}",
            camera_preview_process_main,
            args_common
            + (
                item.preview_queue,
                self.report_queue,
                item.stop_event,
                item.generation,
                self.health_queue,
                item.slot_id,
            ),
        )
        if item.camera["use_fight_detection"]:
            self._spawn_fight(item, args_common, admission_port)
        if item.camera["use_speed_detection"]:
            self._spawn_speed(item)

    def _spawn_fight(self, item, args_common, admission_port):
        cid = item.camera_id
        item.processes["camera"] = self.process_factory(
            f"camera_{cid}",
            camera_process_main,
            args_common
            + (
                admission_port(self.stage3_queue),
                self.report_queue,
                item.stop_event,
                admission_port(self.person_request_queue),
                self.person_result_channels[item.slot_id],
                admission_port(self.pose_request_queue),
                self.pose_result_channels.get(item.slot_id),
                item.generation,
                item.fight_queue,
                item.slot_id,
                self.health_queue,
            ),
        )

    def _spawn_speed(self, item):
        if not self.speed_service_available:
            self.disable_speed(item, "vehicle_service_unavailable")
            return
        if self.vehicle_requests is None:
            raise RuntimeError("shared vehicle service is required")
        self.speed_epochs[item.slot_id] += 1
        item.speed_epoch = int(self.speed_epochs[item.slot_id])
        item.speed_stop.clear()
        item.speed_failed = False
        item.speed_service_waiting = False
        self._drain(self.vehicle_results[item.slot_id])
        item.processes["speed"] = self.process_factory(
            f"speed_{item.camera_id}", speed_process_main,
            (self.config, item.camera, item.speed_queue, self.vehicle_requests.for_slot(item.slot_id),
             self.vehicle_results[item.slot_id], item.speed_stop, item.generation, item.slot_id,
             self.speed_epochs, item.speed_epoch, self.slot_generations, self.health_queue))

    def disable_speed(self, item, reason):
        if item.speed_failed:
            return
        item.speed_failed = True
        item.speed_service_waiting = reason.startswith("vehicle_service_")
        item.speed_stop.set()
        self.speed_epochs[item.slot_id] += 1
        self.terminate_process(item.processes.get("speed"), timeout=1.0)
        self._status("speed_consumer_failed", item.camera_id, reason=reason,
                     generation=item.generation, consumer_epoch=item.speed_epoch)

    def start_camera(
        self,
        camera: dict,
        *,
        reason: str = "desired_added",
        preferred_slot_id: int | None = None,
        reuse_current_generation: bool = False,
    ) -> CameraRuntime:
        normalized = runtime_camera(camera)
        cid = normalized["camera_id"]
        existing = self.runtimes.get(cid)
        if existing is not None:
            return existing
        if not cid or not normalized["source"]:
            raise ValueError("camera_id and source are required")
        if not self._free_slots:
            self._status("camera_failed", cid, reason="no_free_runtime_slot")
            raise RuntimeError("dynamic camera slot capacity exhausted")
        if preferred_slot_id is not None and preferred_slot_id in self._free_slots:
            slot_id = int(preferred_slot_id)
        else:
            slot_id = min(self._free_slots)
        self._free_slots.remove(slot_id)
        generation = (
            int(self._slot_counters[slot_id])
            if reuse_current_generation
            else self._next_generation(slot_id)
        )
        for admission in (self.person_request_queue, self.pose_request_queue, self.stage3_queue, self.vehicle_requests):
            if hasattr(admission, "reset_metrics"):
                admission.reset_metrics(slot_id)
        item = CameraRuntime(
            camera=normalized,
            slot_id=slot_id,
            generation=generation,
            stop_event=self.ctx.Event(),
            file_eof_event=self.ctx.Event(),
            fight_queue=self.ctx.Queue(
                maxsize=max(
                    1, int(self.runtime_config.get("camera_ingest_fight_queue_size", 8))
                )
            ) if normalized["use_fight_detection"] else None,
            preview_queue=self.ctx.Queue(
                maxsize=max(
                    1, int(self.runtime_config.get("camera_ingest_preview_queue_size", 1))
                )
            ),
        )
        if normalized["use_speed_detection"]:
            item.speed_queue = self.ctx.Queue(maxsize=max(1, int(self.runtime_config.get("camera_ingest_speed_queue_size", 2))))
            item.speed_stop = self.ctx.Event()
        self.runtimes[cid] = item
        self._status(
            "camera_starting", cid, generation=generation, slot_id=slot_id, reason=reason
        )
        try:
            if normalized["use_fight_detection"] and not self.fight_service_available:
                item.fight_service_waiting = True
                item.intentional_stop = True
                item.state = RECONNECTING
                return item
            self._spawn_trio(item)
        except Exception:
            item.state = FAILED
            self.stop_camera(cid, reason="start_failed")
            raise
        item.state = RUNNING
        self._status(
            "camera_started", cid, generation=generation, slot_id=slot_id, reason=reason
        )
        return item

    def stop_camera(self, camera_id: str, *, reason: str = "desired_removed") -> bool:
        item = self.runtimes.get(str(camera_id))
        if item is None:
            return False
        item.intentional_stop = True
        item.state = STOPPING
        self._status(
            "camera_stopping",
            item.camera_id,
            generation=item.generation,
            slot_id=item.slot_id,
            reason=reason,
        )
        # Invalidate first so delayed shared-worker output cannot enter a reused slot.
        self._next_generation(item.slot_id)
        item.stop_event.set()
        if item.speed_stop is not None:
            item.speed_stop.set()
            self.speed_epochs[item.slot_id] += 1
        for process in item.processes.values():
            self.terminate_process(process, timeout=2.0)
        if not item.fight_service_waiting:
            self._drain(self.person_result_channels.get(item.slot_id))
        if not item.fight_service_waiting and item.slot_id in self.pose_result_channels:
            self._drain(self.pose_result_channels[item.slot_id])
        self.close_queue(item.fight_queue)
        self.close_queue(item.preview_queue)
        if item.speed_queue is not None:
            self.close_queue(item.speed_queue)
        item.state = STOPPED
        del self.runtimes[item.camera_id]
        self._free_slots.add(item.slot_id)
        self._status(
            "camera_stopped",
            item.camera_id,
            generation=item.generation,
            slot_id=item.slot_id,
            reason=reason,
        )
        return True

    def suspend_fight(self):
        """Reserve slots, invalidate all affected generations, then withdraw readers.

        Whole affected LIVE camera runtimes restart on fresh transport; Speed-only
        cameras and the shared Vehicle process are not involved.
        """
        self.fight_service_available = False
        affected = [item for item in self.runtimes.values()
                    if item.camera["use_fight_detection"] and not item.fight_service_waiting]
        for item in affected:
            item.generation = self._next_generation(item.slot_id)
            item.fight_service_waiting = True
            item.intentional_stop = True
            item.state = RECONNECTING
            item.stop_event.set()
            if item.speed_stop is not None:
                item.speed_stop.set()
                self.speed_epochs[item.slot_id] += 1
        for item in affected:
            for process in item.processes.values():
                self.terminate_process(process, timeout=1.0)
                if process.is_alive():
                    raise RuntimeError("fight_camera_withdrawal_failed")
            item.processes.clear()
            for channel in (item.fight_queue, item.preview_queue, item.speed_queue):
                try:
                    channel.cancel_join_thread()
                except AttributeError:
                    pass
                self.close_queue(channel)

    def resume_fight(self):
        self.fight_service_available = True
        for item in list(self.runtimes.values()):
            if item.fight_service_waiting:
                try:
                    self.restart_camera(item.camera_id, item.camera, reason="fight_service_recovered")
                except Exception as exc:
                    self._remember_failed(item.camera, "fight_camera_resume_failed", exc)
                    raise

    def restart_camera(self, camera_id: str, camera: dict, *, reason: str) -> CameraRuntime:
        old = self.runtimes[str(camera_id)]
        slot_id = old.slot_id
        restart_count = old.restart_count + 1
        self._status(
            "camera_restarting",
            old.camera_id,
            generation=old.generation,
            slot_id=slot_id,
            reason=reason,
        )
        self.stop_camera(old.camera_id, reason=reason)
        item = self.start_camera(
            camera,
            reason=reason,
            preferred_slot_id=slot_id,
            reuse_current_generation=True,
        )
        item.restart_count = restart_count
        self._status(
            "camera_generation_changed",
            item.camera_id,
            generation=item.generation,
            slot_id=item.slot_id,
            reason=reason,
        )
        return item

    def reconcile(self, cameras: list[dict], *, revision: int | None = None) -> dict:
        desired = {
            item["camera_id"]: item
            for item in (runtime_camera(camera) for camera in cameras)
            if item["enabled"] and (item["use_fight_detection"] or item["use_speed_detection"])
        }
        actual_ids = set(self.runtimes)
        desired_ids = set(desired)
        for cid in set(self._failed_desired) - desired_ids:
            self._failed_desired.pop(cid, None)
            self._failed_attempts.pop(cid, None)
        removed = sorted(actual_ids - desired_ids)
        added = sorted(desired_ids - actual_ids)
        restarted: list[str] = []
        for cid in removed:
            self._status("camera_desired_removed", cid, reason="desired_removed")
            self.stop_camera(cid, reason="desired_removed")
        for cid in sorted(actual_ids & desired_ids):
            current = self.runtimes[cid]
            if restart_identity(current.camera) != restart_identity(desired[cid]):
                try:
                    self.restart_camera(cid, desired[cid], reason="runtime_config_changed")
                    restarted.append(cid)
                    self._failed_desired.pop(cid, None)
                    self._failed_attempts.pop(cid, None)
                except Exception as exc:
                    self._remember_failed(desired[cid], "restart_failed", exc)
            else:
                current.camera = desired[cid]
        for cid in added:
            self._status("camera_desired_added", cid, reason="desired_added")
            if not self._retry_due(cid):
                continue
            try:
                self.start_camera(desired[cid], reason="desired_added")
                self._failed_desired.pop(cid, None)
                self._failed_attempts.pop(cid, None)
            except Exception as exc:
                self._remember_failed(desired[cid], "start_failed", exc)
        result = {
            "revision": revision,
            "desired_count": len(desired),
            "active_count": len(self.runtimes),
            "added": added,
            "removed": removed,
            "restarted": restarted,
        }
        self._status("camera_reconcile_summary", **result)
        return result

    def _retry_due(self, camera_id: str) -> bool:
        attempted = self._failed_attempts.get(camera_id)
        return attempted is None or self.monotonic() - attempted >= self._restart_backoff

    def _remember_failed(self, camera: dict, reason: str, exc: Exception) -> None:
        cid = str(camera["camera_id"])
        self._failed_desired[cid] = dict(camera)
        self._failed_attempts[cid] = self.monotonic()
        self._status(
            "camera_failed",
            cid,
            reason=reason,
            error=type(exc).__name__,
            source=redact_source(camera["source"]),
        )

    def retry_failed(self, *, revision: int | None = None) -> dict | None:
        if not any(self._retry_due(cid) for cid in self._failed_desired):
            return None
        desired = [item.camera for item in self.runtimes.values()]
        desired.extend(self._failed_desired.values())
        return self.reconcile(desired, revision=revision)

    def file_eof_reached(self, item: CameraRuntime) -> bool:
        return bool(
            is_file_source(item.camera["source"])
            and not self.runtime_config.get("loop_file_sources", False)
            and item.file_eof_event is not None and item.file_eof_event.is_set()
        )

    def poll(self) -> None:
        for cid, item in list(self.runtimes.items()):
            if item.intentional_stop or item.file_done:
                continue
            ingest = item.processes.get("ingest")
            camera = item.processes.get("camera")
            preview = item.processes.get("preview")
            speed = item.processes.get("speed")
            if speed is not None:
                speed_alive = speed.is_alive()
                speed_exitcode = getattr(speed, "exitcode", None)
                speed_dead = not speed_alive or speed_exitcode is not None
                speed_drained = (speed_dead and speed_exitcode == 0
                                 and self.file_eof_reached(item))
                if not self.speed_service_available and not speed_drained:
                    self.disable_speed(item, "vehicle_service_unavailable")
                elif speed_dead and (speed_exitcode != 0 or (
                    is_file_source(item.camera["source"]) and not speed_drained
                )):
                    # Zero exit can mean stop/epoch invalidation, not EOF. Latch
                    # the existing file failure; later ingest EOF cannot erase it.
                    self.disable_speed(item, "speed_process_dead")
                if (item.speed_failed and self.speed_service_available
                        and not is_file_source(item.camera["source"])
                        and item.speed_restarts < int(self.runtime_config.get("watchdog_camera_restart_limit", 3))
                        and self.monotonic() - item.speed_last_restart >= max(1, float(self.runtime_config.get("watchdog_camera_restart_cooldown_sec", 120)))):
                    item.speed_restarts += 1
                    item.speed_last_restart = self.monotonic()
                    self._spawn_speed(item)
            # Check a Fight exit before finalizing on ingest exit. A nonzero
            # consumer exit must never be mislabeled as successful file drain.
            if camera is not None and not camera.is_alive():
                if not (getattr(camera, "exitcode", None) == 0 and self.file_eof_reached(item)):
                    self._restart_failed(item, "camera_process_dead")
                    continue
            if ingest is not None and not ingest.is_alive():
                if (
                    getattr(ingest, "exitcode", None) == 0
                    and self.file_eof_reached(item)
                ):
                    if camera is not None and camera.is_alive():
                        continue
                    if camera is not None and getattr(camera, "exitcode", None) != 0:
                        self._restart_failed(item, "camera_process_dead")
                        continue
                    if speed is not None and speed.is_alive():
                        continue
                    if speed is not None and getattr(speed, "exitcode", None) != 0:
                        # It may have exited since the Speed observation above.
                        self.disable_speed(item, "speed_process_dead")
                    item.file_done = True
                    item.state = STOPPED
                    self._status(
                        "camera_stopped",
                        cid,
                        generation=item.generation,
                        slot_id=item.slot_id,
                        reason="file_eof",
                    )
                    continue
                self._restart_failed(item, "ingest_process_dead")
                continue
            if self.file_eof_reached(item):
                # Other required consumers can still be draining. Preview exit
                # is expected too; do not spawn another reader of its EOF queue.
                continue
            if preview is not None and not preview.is_alive():
                self._status(
                    "camera_restarting",
                    cid,
                    generation=item.generation,
                    slot_id=item.slot_id,
                    reason="preview_process_dead",
                )
                item.processes["preview"] = self.process_factory(
                    f"camera_preview_{cid}",
                    camera_preview_process_main,
                    (
                        self.config,
                        item.camera,
                        item.preview_queue,
                        self.report_queue,
                        item.stop_event,
                        item.generation,
                        self.health_queue,
                        item.slot_id,
                    ),
                )

    def restart_preview(self, camera_id: str, *, reason: str) -> bool:
        item = self.runtimes.get(str(camera_id))
        if item is None or item.intentional_stop or item.file_done:
            return False
        old = item.processes.get("preview")
        self.terminate_process(old, timeout=2.0)
        self._status(
            "camera_restarting",
            item.camera_id,
            generation=item.generation,
            slot_id=item.slot_id,
            reason=reason,
            component="camera_preview",
        )
        item.processes["preview"] = self.process_factory(
            f"camera_preview_{item.camera_id}",
            camera_preview_process_main,
            (
                self.config,
                item.camera,
                item.preview_queue,
                self.report_queue,
                item.stop_event,
                item.generation,
                self.health_queue,
                item.slot_id,
            ),
        )
        return True

    def _restart_failed(self, item: CameraRuntime, reason: str) -> None:
        now = self.monotonic()
        if item.last_restart_at > 0.0 and now - item.last_restart_at < self._restart_backoff:
            item.state = RECONNECTING
            return
        item.last_restart_at = now
        camera = dict(item.camera)
        self._status(
            "camera_failed",
            item.camera_id,
            generation=item.generation,
            slot_id=item.slot_id,
            reason=reason,
            source=redact_source(item.camera["source"]),
        )
        try:
            self.restart_camera(item.camera_id, camera, reason=reason)
        except Exception as exc:
            self._remember_failed(camera, "restart_failed", exc)

    def all_file_cameras_done(self) -> bool:
        values = list(self.runtimes.values())
        return bool(values) and all(
            item.file_done and is_file_source(item.camera["source"]) for item in values
        )

    def get_camera_status(self) -> dict[str, dict]:
        status = {
            cid: {
                "state": item.state,
                "slot_id": item.slot_id,
                "generation": item.generation,
                "file_done": item.file_done,
                "file_eof": self.file_eof_reached(item),
                "fight_service_waiting": item.fight_service_waiting,
                "restart_count": item.restart_count,
                "use_fight_detection": item.camera["use_fight_detection"],
                "use_speed_detection": item.camera["use_speed_detection"],
                "speed_failed": item.speed_failed,
                "speed_restarts": item.speed_restarts,
                "speed_epoch": item.speed_epoch,
                "pids": {
                    name: getattr(process, "pid", None)
                    for name, process in item.processes.items()
                },
            }
            for cid, item in self.runtimes.items()
        }
        for cid in self._failed_desired:
            status.setdefault(
                cid,
                {
                    "state": FAILED,
                    "slot_id": None,
                    "generation": None,
                    "file_done": False,
                    "pids": {},
                },
            )
        return status

    def stop_all(self, *, reason: str = "global_stop") -> None:
        for camera_id in list(self.runtimes):
            self.stop_camera(camera_id, reason=reason)
