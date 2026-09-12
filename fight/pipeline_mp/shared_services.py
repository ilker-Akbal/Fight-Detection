"""Parent-only capability lifecycle; only queue handles/events cross spawn boundaries."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from contextlib import nullcontext

from fight.pipeline_mp.common import is_file_source
from fight.pipeline_mp.health import HealthPolicy, HealthRegistry
from fight.pipeline_mp.scheduling import FairRequestQueue
from fight.pipeline_mp.fight_identity import FightGenerations


class SharedServiceStartError(RuntimeError):
    pass


@dataclass
class ServiceHealthChannel:
    """Tag incarnation identity while retaining the single bounded health queue."""
    channel: object
    epoch: int

    def put_nowait(self, event):
        self.channel.put_nowait(replace(event, service_epoch=self.epoch))


@dataclass
class FightIncidentChannel:
    channel: object
    epoch: int

    def put(self, result, *args, **kwargs):
        self.channel.put(replace(result, service_epoch=self.epoch), *args, **kwargs)


@dataclass
class ServiceReportChannel:
    channel: object
    epoch: int

    def put(self, message, *args, **kwargs):
        self.channel.put(replace(message, row={**message.row, "service_epoch": self.epoch}), *args, **kwargs)

    def put_nowait(self, message):
        self.put(message, block=False)


def required_capabilities(cameras):
    active = [cam for cam in cameras if cam.get("enabled", True)]
    return {"fight": any(cam.get("use_fight_detection", True) for cam in active),
            "vehicle": any(cam.get("use_speed_detection", False) for cam in active)}


class SharedServices:
    """Capability-managed transport bundles with bounded in-runtime recovery.

    Vehicle recovery replaces its entire transport after withdrawing consumers.
    This avoids reusing multiprocessing queues/locks from a killed CUDA worker.
    Retry budget is per runtime lifetime, including failures of replacement starts.
    """

    FIGHT = ("person", "person_router", "pose", "pose_router", "stage3")
    CODES = {"person": 4, "person_router": 5, "pose": 6, "pose_router": 7, "stage3": 2}
    FINALIZE_TIMEOUT_SEC = 8.0  # One grace budget per bundle, not per worker.

    def __init__(self, manager, incident_queue, process_factory, terminate_process,
                 registry=None, monotonic=time.monotonic, publication_floor=None):
        self.manager, self.ctx, self.config = manager, manager.ctx, manager.config
        self.runtime = self.config.get("runtime", {})
        self.incident_queue = incident_queue
        self.start_process, self.terminate = process_factory, terminate_process
        self.registry = registry if registry is not None else HealthRegistry(monotonic=monotonic)
        self.clock = monotonic
        self.policy = HealthPolicy.from_runtime(self.runtime)
        self.slots = len(manager.slot_generations)
        self.required = {"fight": False, "vehicle": False}
        self.bundles = {}
        self.idle_since = {}
        self.grace = max(0, float(self.runtime.get("shared_service_idle_grace_sec", 5)))
        self.retry_limit = max(0, int(self.runtime.get("vehicle_service_restart_limit", 3)))
        self.retry_base = max(0.25, float(self.runtime.get("vehicle_service_restart_backoff_sec", 2)))
        self.retry_max = max(self.retry_base, float(self.runtime.get("vehicle_service_restart_max_backoff_sec", 30)))
        self.attempts, self.vehicle_epoch = 0, 0
        self.fight_epoch = 0
        self.publication_floor = publication_floor if publication_floor is not None else manager.fight_publication_floor
        self.fight_attempts = 0
        self.fight_retry_at = None
        self.fight_failed = False
        self.fight_failure_reason = ""
        self.fight_retry_limit = max(0, int(self.runtime.get("fight_service_restart_limit", 3)))
        self.fight_retry_base = max(.25, float(self.runtime.get("fight_service_restart_backoff_sec", 2)))
        self.fight_retry_max = max(self.fight_retry_base, float(self.runtime.get("fight_service_restart_max_backoff_sec", 30)))
        self.health_queue = manager.health_queue if manager.health_queue is not None else self.ctx.Queue(
            maxsize=max(64, int(self.runtime.get("health_queue_size", 4096))))
        self.retry_at = None
        self.vehicle_failed = False
        self.draining = None
        self.drain_invalidated = False
        for component in (*self.FIGHT, "vehicle"):
            self.registry.register_worker(component)
            self.registry.workers[component].update(required=False, service_state="disabled")
        manager.speed_service_available = False

    def _queue(self, bundle, size=1, joinable=False):
        channel = (self.ctx.JoinableQueue if joinable else self.ctx.Queue)(maxsize=max(1, size))
        bundle["queues"].append(channel)
        return channel

    def _admission(self, bundle, stage, size):
        if stage == "vehicle" or self.runtime.get("fair_scheduling_enabled", True):
            channel = FairRequestQueue(self.ctx, self.slots, size)
            bundle["queues"].append(channel)
        else:
            channel = self._queue(bundle, int(self.runtime.get(f"{stage}_queue_size" if stage == "stage3"
                else f"{stage}_request_queue_size", 64 if stage == "stage3" else self.slots)), True)
        bundle["admissions"][stage] = channel
        return channel

    def _spawn(self, bundle, component, target, args):
        bundle["processes"][component] = self.start_process(component, target, args)
        self.registry.register_worker(component)
        self.registry.workers[component].update(required=True, service_state="starting",
            service_epoch=self.vehicle_epoch if component == "vehicle" else self.fight_epoch,
            pid=getattr(bundle["processes"][component], "pid", None))
        self.manager._status("shared_service_started", component=component,
            service_epoch=self.registry.workers[component]["service_epoch"],
            pid=self.registry.workers[component]["pid"])

    def _start(self, kind):
        if self.manager.stopping:
            return
        bundle = {"processes": {}, "queues": [], "admissions": {}, "stop": self.ctx.Event()}
        self.bundles[kind] = bundle  # Cleanup also covers a partially failed spawn.
        if kind == "vehicle":
            self.vehicle_epoch += 1
        else:
            self.fight_epoch += 1
        bundle["health"] = ServiceHealthChannel(self.health_queue,
            self.vehicle_epoch if kind == "vehicle" else self.fight_epoch)
        manager, cfg, stop = self.manager, self.config, bundle["stop"]
        reports = ServiceReportChannel(manager.report_queue,
                                       self.vehicle_epoch if kind == "vehicle" else self.fight_epoch)
        generations, health = manager.slot_generations, bundle["health"]
        if kind == "vehicle":
            from fight.pipeline_mp.speed_worker import vehicle_service_main
            manager.vehicle_requests = self._admission(bundle, "vehicle", 1)
            manager.vehicle_results = {slot: self._queue(bundle) for slot in range(self.slots)}
            self._spawn(bundle, "vehicle", vehicle_service_main, (cfg, manager.vehicle_requests,
                manager.vehicle_results, stop, generations, manager.speed_epochs, health,
                None, reports, self.vehicle_epoch))
            manager.speed_service_available = True
        else:
            from fight.pipeline_mp.person_worker import person_inference_process_main, person_result_router_main
            from fight.pipeline_mp.pose_worker import pose_inference_process_main, pose_result_router_main
            from fight.pipeline_mp.stage3_worker import stage3_process_main
            generations = FightGenerations(generations, self.publication_floor)
            manager.person_request_queue = self._admission(bundle, "person", 1)
            person_results = self._queue(bundle, int(self.runtime.get("person_result_queue_size", self.slots)), True)
            manager.person_result_channels = {slot: self._queue(bundle, int(self.runtime.get("person_camera_result_queue_size", 2)))
                                              for slot in range(self.slots)}
            self._spawn(bundle, "person_router", person_result_router_main, (cfg, person_results,
                manager.person_result_channels, reports, stop, generations, health))
            self._spawn(bundle, "person", person_inference_process_main, (cfg, manager.person_request_queue,
                person_results, reports, stop, generations, health))
            if self.runtime.get("use_pose", True):
                manager.pose_request_queue = self._admission(bundle, "pose", 1)
                pose_results = self._queue(bundle, int(self.runtime.get("pose_result_queue_size", self.slots)), True)
                manager.pose_result_channels = {slot: self._queue(bundle, int(self.runtime.get("pose_camera_result_queue_size", 2)))
                                                for slot in range(self.slots)}
                self._spawn(bundle, "pose_router", pose_result_router_main, (cfg, pose_results,
                    manager.pose_result_channels, reports, stop, generations, health))
                self._spawn(bundle, "pose", pose_inference_process_main, (cfg, manager.pose_request_queue,
                    pose_results, reports, stop, generations, health))
            if self.runtime.get("use_stage3", True):
                manager.stage3_queue = self._admission(bundle, "stage3", max(1, int(self.runtime.get("stage3_pending_per_camera", 1))))
                self._spawn(bundle, "stage3", stage3_process_main, (cfg, manager.stage3_queue,
                    FightIncidentChannel(self.incident_queue, self.fight_epoch), reports, stop, generations, health))
            manager.fight_service_available = True
            manager.fight_service_epoch = self.fight_epoch

    @staticmethod
    def _close(channel):
        # Abandoned/invalidated work must not leave parent feeder joins unbounded.
        try:
            channel.cancel_join_thread()
            channel.close()
        except (AttributeError, OSError, ValueError):
            pass

    def _finalize(self, bundle):
        """Normal withdrawal only: finish inference before stopping its routers.

        Person/Pose require a sentinel even when idle: their loop deliberately
        ignores the stop event. Joining a normally exited process also flushes
        its multiprocessing Reporter feeder before the parent sends Reporter EOF.
        The sender is bounded from the parent's perspective even if an unhealthy
        worker left an admission lock unusable. Failed transport is never reused.
        """
        deadline = time.monotonic() + self.FINALIZE_TIMEOUT_SEC
        sent = threading.Event()

        def send():
            try:
                for stage, channel in bundle["admissions"].items():
                    process = bundle["processes"].get(stage)
                    if process is not None and process.is_alive():
                        remaining = max(0.0, deadline - time.monotonic())
                        channel.put(None, timeout=remaining)
            except Exception:
                pass  # The bounded forced fallback below still owns teardown.
            finally:
                sent.set()

        threading.Thread(target=send, name="shared_service_finalize", daemon=True).start()
        sent.wait(max(0.0, deadline - time.monotonic()))
        for stage in bundle["admissions"]:
            process = bundle["processes"].get(stage)
            if process is not None:
                process.join(timeout=max(0.0, deadline - time.monotonic()))

    def _stop(self, kind, *, graceful=False):
        bundle = self.bundles.pop(kind, None)
        if bundle is None:
            return
        if graceful:
            self._finalize(bundle)
        bundle["stop"].set()
        for process in bundle["processes"].values():
            self.terminate(process, timeout=1.0)
            if kind == "fight" and process.is_alive():
                self.bundles[kind] = bundle
                raise SharedServiceStartError("fight_service_stop_failed")
        for channel in bundle["queues"]:
            self._close(channel)
        if kind == "vehicle":
            self.manager.vehicle_requests, self.manager.vehicle_results = None, {}
            self.manager.speed_service_available = False
        else:
            self.manager.person_request_queue = self.manager.pose_request_queue = self.manager.stage3_queue = None
            self.manager.person_result_channels, self.manager.pose_result_channels = {}, {}
            self.draining = None

    def _fight_failure(self, reason, *, component="fight", exit_code=None):
        # Fence both buffered aggregator segments and late Stage3 output BEFORE
        # touching old transport. Normal capability removal/EOF does not fence
        # valid pending incidents; only a failed incarnation does.
        lock = getattr(self.publication_floor, "get_lock", lambda: nullcontext())()
        with lock:
            self.publication_floor[0] = self.fight_epoch + 1
        file_affected = any(item.camera["use_fight_detection"] and is_file_source(item.camera["source"])
                            and not item.fight_service_waiting for item in self.manager.runtimes.values())
        self.fight_failure_reason = "fight_file_incomplete" if file_affected else reason
        try:
            self.manager.suspend_fight()
            self._stop("fight")
        except Exception:
            self.fight_failure_reason = "fight_withdrawal_failed"
            file_affected = True  # Unsafe teardown must never start a duplicate owner.
        self.fight_failed = file_affected or self.fight_attempts >= self.fight_retry_limit
        if self.fight_failed and not file_affected:
            self.fight_failure_reason = "fight_recovery_exhausted"
        self.fight_retry_at = None if self.fight_failed else self.clock() + min(
            self.fight_retry_max, self.fight_retry_base * 2 ** min(self.fight_attempts, 20))
        self.manager._status("fight_service_failed" if self.fight_failed else "fight_service_restarting",
            reason=self.fight_failure_reason, component_failure=reason,
            component=component, exit_code=exit_code,
            retries=self.fight_attempts, service_epoch=self.fight_epoch)

    def _withdraw_speed(self):
        self.manager.speed_service_available = False
        for item in self.manager.runtimes.values():
            if item.camera["use_speed_detection"] and item.speed_stop is not None and not item.file_done:
                process = item.processes.get("speed")
                if (is_file_source(item.camera["source"]) and process is not None
                        and not process.is_alive() and process.exitcode == 0):
                    continue  # Already drained before the unrelated service death.
                self.manager.disable_speed(item, "vehicle_service_restarting")

    def _vehicle_failure(self, reason):
        # Capture before withdrawal: terminate/kill exit codes are consequences,
        # not the cause of the health decision (a stalled worker is still alive).
        process = self.bundles.get("vehicle", {}).get("processes", {}).get("vehicle")
        exit_code = getattr(process, "exitcode", None)
        self._withdraw_speed()  # Invalidate before killing/replacing any transport.
        self._stop("vehicle")
        self.vehicle_failed = self.attempts >= self.retry_limit
        self.retry_at = None if self.vehicle_failed else self.clock() + min(
            self.retry_max, self.retry_base * 2 ** min(self.attempts, 20))
        self.manager._status("vehicle_service_failed" if self.vehicle_failed else "vehicle_service_restarting",
                             component="vehicle", reason=reason, component_failure=f"vehicle_{reason}",
                             exit_code=exit_code,
                             retries=self.attempts, service_epoch=self.vehicle_epoch)

    def prepare(self, cameras):
        """Start dependencies BEFORE camera reconcile; stop only afterwards in tick."""
        if self.manager.stopping:
            return
        self.required = required_capabilities(cameras)
        for kind, required in self.required.items():
            if required:
                self.idle_since.pop(kind, None)
                if kind == "fight":
                    self.drain_invalidated = True
                if kind not in self.bundles and (kind != "vehicle" or
                        (self.retry_at is None and not self.vehicle_failed)) and (kind != "fight" or
                        (self.fight_retry_at is None and not self.fight_failed)):
                    try:
                        self._start(kind)
                    except Exception:
                        if kind != "vehicle":
                            self._stop(kind)
                            raise SharedServiceStartError("fight_service_start_failed") from None
                        self._vehicle_failure("start_failed")
            else:
                self.idle_since.setdefault(kind, self.clock())
        self._sync_health()

    def _fight_drained(self, bundle):
        # No camera producers remain. Owned FairQueue counters include inference;
        # JoinableQueue.join acknowledgements cover legacy queues and result routers.
        if any(not channel.empty() for channel in bundle["admissions"].values()
               if getattr(channel, "capacity_control", False)):
            return False
        if self.draining is not None and self.drain_invalidated:
            if not self.draining.is_set():
                return False
            self.draining = None
        if self.draining is None:
            self.drain_invalidated = False
            self.draining = threading.Event()
            done = self.draining
            channels = [channel for channel in bundle["queues"] if hasattr(channel, "join")]
            def join_pending():
                for channel in channels:
                    channel.join()
                done.set()
            threading.Thread(target=join_pending, name="fight_service_drain", daemon=True).start()
        return self.draining.is_set()

    def tick(self):
        if self.manager.stopping:
            return
        if self.manager.health_queue is not None:
            self.registry.sync_cameras(self.manager.get_camera_status())
        self.registry.drain(self.health_queue, max(64, int(self.runtime.get("health_event_drain_limit", 2048))))
        fight = self.bundles.get("fight")
        if fight is not None and not self.fight_failed:
            for component, process in fight["processes"].items():
                state, reason = self.registry._worker_health(component, self.registry.workers[component],
                    self.policy, self.clock(), process.is_alive())
                if state == "FAILED":
                    self._fight_failure(f"{component}_{reason}", component=component,
                                        exit_code=getattr(process, "exitcode", None))
                    break
        if self.required["fight"] and self.fight_retry_at is not None and self.clock() >= self.fight_retry_at:
            self.fight_attempts += 1
            self.fight_retry_at = None
            try:
                self._start("fight")
                self.manager.resume_fight()
            except Exception:
                self._fight_failure("fight_replacement_start_failed")
        bundle = self.bundles.get("vehicle")
        if bundle is not None:
            process = bundle["processes"]["vehicle"]
            record = self.registry.workers["vehicle"]
            state, reason = self.registry._worker_health("vehicle", record, self.policy, self.clock(), process.is_alive())
            if state == "FAILED":
                self._vehicle_failure(reason)
        if self.required["vehicle"] and self.retry_at is not None and self.clock() >= self.retry_at:
            self.attempts += 1
            self.retry_at = None
            try:
                self._start("vehicle")
            except Exception:
                self._vehicle_failure("replacement_start_failed")
            else:
                # Service recovery has its own bounded budget, independent of local
                # consumer failures. Files are never resumed after partial failure.
                for item in self.manager.runtimes.values():
                    if item.camera["use_speed_detection"] and item.speed_service_waiting and not item.file_done and not is_file_source(item.camera["source"]):
                        try:
                            self.manager._spawn_speed(item)
                        except Exception:
                            self.manager.disable_speed(item, "speed_consumer_restart_failed")
                            item.speed_restarts += 1
                            item.speed_last_restart = self.clock()
        for kind in list(self.bundles):
            if kind == "fight" and self.fight_failed:
                continue
            if not self.required[kind] and self.clock() - self.idle_since[kind] >= self.grace:
                if kind == "fight" and not self._fight_drained(self.bundles[kind]):
                    continue
                self._stop(kind, graceful=True)
        self._sync_health()

    def _sync_health(self):
        processes = self.processes()
        for component in (*self.FIGHT, "vehicle"):
            kind = "vehicle" if component == "vehicle" else "fight"
            required = self.required[kind] and (component not in {"pose", "pose_router", "stage3"} or
                self.runtime.get("use_stage3" if component == "stage3" else "use_pose", True))
            record = self.registry.workers[component]
            if kind == "fight" and self.fight_failed and required:
                state = "failed"
            elif component in processes:
                state = "starting" if record["last_event"] == "registered" else "running"
            elif required and kind == "vehicle":
                state = "failed" if self.vehicle_failed else "restarting"
            elif required and kind == "fight" and self.fight_retry_at is not None:
                state = "restarting"
            else:
                state = "disabled"
            record.update(required=bool(required), service_state=state)
            if state == "disabled":
                record["capacity"] = {}
            if component == "vehicle":
                record.update(service_epoch=self.vehicle_epoch, restart_count=self.attempts)
            else:
                record.update(service_epoch=self.fight_epoch, restart_count=self.fight_attempts, recoverable=True)
                record["service_failure_reason"] = self.fight_failure_reason

    def processes(self):
        return {name: process for bundle in self.bundles.values() for name, process in bundle["processes"].items()}

    def critical_processes(self):
        return {name: (process, self.CODES[name]) for name, process in self.processes().items() if name != "vehicle"}

    def admissions(self):
        return {stage: channel for bundle in self.bundles.values() for stage, channel in bundle["admissions"].items()}

    def close(self, *, graceful=True):
        for kind in list(self.bundles):
            self._stop(kind, graceful=graceful)
        if self.manager.health_queue is None:
            self._close(self.health_queue)
