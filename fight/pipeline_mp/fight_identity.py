"""Spawn-safe Fight incarnation boundaries; lifecycle policy stays in the parent."""
from dataclasses import dataclass, replace
import queue
import time

from fight.pipeline_mp.messages import ReportMessage


@dataclass
class FightGenerations:
    generations: object
    publication_floor: object

    def __getitem__(self, slot):
        return self.generations[slot]

    def allows(self, message):
        slot = int(getattr(message, "slot_id", -1))
        return slot < 0 or int(getattr(message, "consumer_epoch", 0)) >= self.publication_floor[slot + 1]


@dataclass
class FightChannel:
    """Tag requests, health and reports; reject results from an older consumer.

    Frames carry an ingest-assigned Fight epoch too. Filtering never owns EOF.
    Queue operations retain their caller's bounded timeout/admission semantics.
    """
    channel: object
    epoch: int

    @property
    def capacity_control(self):
        return getattr(self.channel, "capacity_control", False)

    def observe(self, slot, outcome):
        if hasattr(self.channel, "observe"):
            self.channel.observe(slot, outcome)

    def qsize(self):
        return self.channel.qsize()

    def put(self, item, *args, **kwargs):
        if isinstance(item, ReportMessage):
            item = replace(item, row={**item.row, "consumer_epoch": self.epoch})
        elif item is not None:
            item = replace(item, consumer_epoch=self.epoch)
        return self.channel.put(item, *args, **kwargs)

    def put_nowait(self, item):
        return self.put(item, block=False)

    def get(self, block=True, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            item = self.channel.get(block=block, timeout=remaining)
            if getattr(item, "consumer_epoch", 0) == self.epoch:
                return item
            if not block or (deadline is not None and time.monotonic() >= deadline):
                raise queue.Empty

    def get_nowait(self):
        return self.channel.get_nowait()
