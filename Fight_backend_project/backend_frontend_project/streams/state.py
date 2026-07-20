import time
from threading import Lock

_lock = Lock()
_state = {
    "running": False,
    "updated_at": None,
    "streams": {}
}

def _touch():
    _state["updated_at"] = time.time()

def set_running(value: bool):
    with _lock:
        _state["running"] = value
        _touch()

def replace_streams(streams: dict):
    with _lock:
        _state["streams"] = streams
        _touch()

def upsert_stream(stream_id: str, payload: dict):
    with _lock:
        current = _state["streams"].get(stream_id, {})
        current.update(payload)
        current["stream_id"] = stream_id
        _state["streams"][stream_id] = current
        _touch()

def stop_all_streams_state():
    with _lock:
        for stream_id, item in _state["streams"].items():
            item["status"] = "stopped"
            item["connected"] = False
            item["fight"] = False
            item["confidence"] = 0.0
            item["message"] = "Durduruldu"
        _state["running"] = False
        _touch()

def snapshot():
    with _lock:
        streams = list(_state["streams"].values())

    def key_fn(item):
        stream_id = item.get("stream_id", "")
        try:
            return int(stream_id.split("_")[-1])
        except Exception:
            return 999999

    streams.sort(key=key_fn)

    return {
        "running": _state["running"],
        "updated_at": _state["updated_at"],
        "stream_count": len(streams),
        "streams": streams,
    }