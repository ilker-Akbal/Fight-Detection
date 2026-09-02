from __future__ import annotations

import queue
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectedBatch:
    requests: list
    received_monotonic: list[float]
    sentinel_received: bool
    collect_wait_ms: float


def collect_request_batch(
    request_queue,
    first_request,
    *,
    enabled: bool,
    batch_size: int,
    max_wait_ms: float,
    first_received_monotonic: float | None = None,
) -> CollectedBatch:
    """Collect a small FIFO batch without relying on Queue.empty/qsize."""
    requests = [first_request]
    size_limit = max(1, int(batch_size))
    wait_seconds = max(0.0, float(max_wait_ms)) / 1000.0
    started = (
        time.perf_counter()
        if first_received_monotonic is None
        else float(first_received_monotonic)
    )
    received_monotonic = [started]

    if not enabled or size_limit <= 1 or wait_seconds <= 0.0:
        return CollectedBatch(requests, received_monotonic, False, 0.0)

    deadline = started + wait_seconds
    sentinel_received = False
    while len(requests) < size_limit:
        remaining = deadline - time.perf_counter()
        if remaining <= 0.0:
            break
        try:
            item = request_queue.get(timeout=remaining)
        except queue.Empty:
            break
        if item is None:
            sentinel_received = True
            break
        requests.append(item)
        received_monotonic.append(time.perf_counter())

    return CollectedBatch(
        requests=requests,
        received_monotonic=received_monotonic,
        sentinel_received=sentinel_received,
        collect_wait_ms=max(0.0, (time.perf_counter() - started) * 1000.0),
    )
