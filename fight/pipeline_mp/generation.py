from __future__ import annotations


def current_slot_generation(slot_generations, slot_id: int) -> int | None:
    """Read a spawn-safe shared slot generation without assuming Array locking."""
    if slot_generations is None or int(slot_id) < 0:
        return None
    try:
        return int(slot_generations[int(slot_id)])
    except (IndexError, TypeError, ValueError):
        return None


def is_current_generation(message, slot_generations) -> bool:
    slot_id = int(getattr(message, "slot_id", -1))
    if slot_id < 0:
        return True
    current = current_slot_generation(slot_generations, slot_id)
    return (current is not None and int(getattr(message, "generation", -1)) == current
            and (not hasattr(slot_generations, "allows") or slot_generations.allows(message)))


def set_slot_generation(slot_generations, slot_id: int, generation: int) -> None:
    lock = getattr(slot_generations, "get_lock", lambda: None)()
    if lock is None:
        slot_generations[int(slot_id)] = int(generation)
        return
    with lock:
        slot_generations[int(slot_id)] = int(generation)
