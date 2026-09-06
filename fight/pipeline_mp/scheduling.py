"""Spawn-safe bounded admission and single-consumer round-robin scheduling."""
from __future__ import annotations

import queue
import time
from collections import deque


class AdmissionShed(RuntimeError):
    """Explicit live-work shedding; callers skip this frame, not a negative result."""


class AdmissionStopped(RuntimeError):
    pass


class FairRequestQueue:
    """One bounded FIFO per stable slot; one shared worker consumes round-robin.

    Capacity is reserved per slot, so a hot producer cannot take another slot's
    admission space. Queues and synchronization objects are created before spawn.
    task_done/empty account for in-flight batches as well as queued work.
    """

    capacity_control = True
    METRICS = ("accepted", "rejected_capacity", "deferred_file", "dropped_live",
               "stale_generation", "dispatches", "high_water")

    def __init__(self, ctx, slot_count: int, pending_per_camera: int = 1):
        self.slot_count = max(1, int(slot_count))
        self.pending_per_camera = max(1, int(pending_per_camera))
        self.capacity = self.slot_count * self.pending_per_camera
        self.channels = [ctx.Queue(self.pending_per_camera) for _ in range(self.slot_count)]
        self.wakeup = ctx.Event()
        self.lock = ctx.RLock()
        self.outstanding = ctx.Array("q", self.slot_count, lock=False)
        self.metrics = ctx.Array("q", self.slot_count * len(self.METRICS), lock=False)
        self._cursor = 0
        self._acks = deque()
        self._sentinel = False

    def for_slot(self, slot):
        return AdmissionPort(self, slot)

    def reset_metrics(self, slot):
        with self.lock:
            for index in range(len(self.METRICS)):
                self.metrics[slot * len(self.METRICS) + index] = 0

    def observe(self, slot: int, outcome: str):
        if 0 <= slot < self.slot_count and outcome in self.METRICS:
            with self.lock:
                self.metrics[slot * len(self.METRICS) + self.METRICS.index(outcome)] += 1

    def put(self, item, block=True, timeout=None):
        slot = 0 if item is None else int(item.slot_id)
        if not 0 <= slot < self.slot_count:
            raise ValueError("invalid admission slot")
        # Count before publication: a very fast consumer may acknowledge before
        # put returns. Never hold the accounting lock across a blocking put.
        with self.lock:
            self.outstanding[slot] += 1
        try:
            self.channels[slot].put(item, block=block, timeout=timeout)
        except BaseException:
            with self.lock:
                self.outstanding[slot] -= 1
            self.observe(slot, "rejected_capacity")
            raise
        with self.lock:
            offset = slot * len(self.METRICS)
            self.metrics[offset] += 1
            self.metrics[offset + 6] = max(self.metrics[offset + 6], self.outstanding[slot])
        self.wakeup.set()

    def put_nowait(self, item):
        self.put(item, block=False)

    def get(self, block=True, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while True:
            self.wakeup.clear()
            for step in range(self.slot_count):
                slot = (self._cursor + step) % self.slot_count
                try:
                    item = self.channels[slot].get_nowait()
                except queue.Empty:
                    continue
                self._cursor = (slot + 1) % self.slot_count
                if item is None:
                    self._sentinel = True
                    continue
                self._acks.append(slot)
                self.observe(slot, "dispatches")
                return item
            with self.lock:
                drained = sum(self.outstanding) == len(self._acks) + 1
            if self._sentinel and drained:
                self._sentinel = False
                self._acks.append(0)
                return None
            remaining = None if deadline is None else deadline - time.monotonic()
            if not block or (remaining is not None and remaining <= 0):
                raise queue.Empty
            # A multiprocessing feeder may publish just after put/set; bounded
            # polling handles that race without Queue.qsize/empty assumptions.
            self.wakeup.wait(0.005 if remaining is None else min(0.005, remaining))

    def get_nowait(self):
        return self.get(block=False)

    def task_done(self):
        slot = self._acks.popleft()
        with self.lock:
            self.outstanding[slot] -= 1

    def empty(self):
        with self.lock:
            return not any(self.outstanding)

    def qsize(self):
        # An accounting observation, including in-flight work and blocked puts.
        with self.lock:
            return sum(self.outstanding)

    def snapshot(self):
        with self.lock:
            slots = {
                str(slot): dict(zip(self.METRICS, self.metrics[
                    slot * len(self.METRICS):(slot + 1) * len(self.METRICS)
                ]))
                for slot in range(self.slot_count)
            }
            totals = {name: sum(row[name] for row in slots.values()) for name in self.METRICS}
            return {"capacity": self.capacity, "pending_per_camera": self.pending_per_camera,
                    "outstanding": sum(self.outstanding), **totals, "slots": slots}

    def close(self):
        for channel in self.channels:
            channel.close()

    def cancel_join_thread(self):
        for channel in self.channels:
            channel.cancel_join_thread()

    def join_thread(self):
        for channel in self.channels:
            channel.join_thread()


class AdmissionPort(FairRequestQueue):
    """Producer view: spawning a camera duplicates only its own queue handles."""

    def __init__(self, owner, slot):
        self.slot_count = owner.slot_count
        self.channels = {slot: owner.channels[slot]}
        self.lock = owner.lock
        self.wakeup = owner.wakeup
        self.outstanding = owner.outstanding
        self.metrics = owner.metrics


def admit(channel, item, stop_event, *, timeout, ordered, health=None, stage=""):
    """Ordered work waits cooperatively; live admission has an explicit outcome."""
    if not getattr(channel, "capacity_control", False):
        channel.put(item, timeout=timeout)
        return
    while stop_event is None or not stop_event.is_set():
        try:
            channel.put(item, timeout=min(0.25, max(0.01, timeout)))
            return
        except queue.Full:
            if not ordered:
                channel.observe(item.slot_id, "dropped_live")
                raise AdmissionShed("live admission capacity")
            channel.observe(item.slot_id, "deferred_file")
            if health is not None:
                health.emit("capacity_wait", detail=stage)
    raise AdmissionStopped("Pipeline is stopping")


def live_request_stale(request, now=None):
    limit = float(getattr(request, "max_age_sec", 0))
    created = float(getattr(request, "created_monotonic", 0))
    return (not getattr(request, "source_is_file", True) and limit > 0 and created > 0
            and (time.perf_counter() if now is None else now) - created >= limit)


def deliver_result(channel, item, stop_event, *, timeout, ordered=False, health=None):
    """Keep admitted results/candidates under downstream pressure until shutdown."""
    if not ordered:
        channel.put(item, timeout=timeout)
        return
    while stop_event is None or not stop_event.is_set():
        try:
            channel.put(item, timeout=min(0.25, max(0.01, timeout)))
            return
        except queue.Full:
            if health is not None:
                health.emit("capacity_wait", detail="result_delivery")
    raise AdmissionStopped("Pipeline is stopping")
