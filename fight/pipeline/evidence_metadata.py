"""Compact evidence identities and explicit offline timeline presentation."""
import hashlib
import json
from datetime import datetime


def source_time_fields(camera_id, start, end, *, start_key="event_start", end_key="event_end"):
    if str(camera_id).startswith("offline_"):
        return {start_key: None, end_key: None,
                "source_start_time_sec": float(start), "source_end_time_sec": float(end)}
    def ts_to_str(value):
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return {start_key: ts_to_str(start), end_key: ts_to_str(end)}


def compact_evidence_name(camera_id, event_id, start, *, generation=0, consumer_epoch=0):
    # Hash the complete identity, not a truncated UUID/sanitized name. Numeric
    # suffixes remain readable; the 128-bit digest fences all identity fields.
    identity = json.dumps([str(camera_id), str(event_id), int(generation), int(consumer_epoch), float(start)],
                          ensure_ascii=True, separators=(",", ":"))
    token = hashlib.blake2b(identity.encode("ascii"), digest_size=16).hexdigest()
    counter = str(event_id).rsplit("_", 1)[-1]
    counter = counter if counter.isascii() and counter.isdigit() and len(counter) <= 8 else "event"
    time_token = (f"v{max(0, round(float(start) * 1000))}" if str(camera_id).startswith("offline_")
                  else datetime.fromtimestamp(start).strftime("t%Y%m%dT%H%M%S"))
    return f"evt_{token}_g{int(generation)}_f{int(consumer_epoch)}_{counter}_{time_token}.mp4"
