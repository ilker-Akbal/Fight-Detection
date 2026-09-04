from __future__ import annotations

import re
from pathlib import Path

from django.http import FileResponse, HttpResponse, StreamingHttpResponse


_RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)\Z", re.IGNORECASE)
DEFAULT_CHUNK_SIZE = 64 * 1024


def _parse_range(value: str, file_size: int) -> tuple[int, int] | None:
    match = _RANGE_PATTERN.fullmatch(value.strip())
    if match is None or file_size <= 0:
        return None

    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        return None
    if not start_raw:
        suffix_length = int(end_raw)
        if suffix_length <= 0:
            return None
        return max(0, file_size - suffix_length), file_size - 1

    start = int(start_raw)
    if start >= file_size:
        return None
    end = file_size - 1 if not end_raw else min(int(end_raw), file_size - 1)
    if end < start:
        return None
    return start, end


def _file_range_iterator(path: Path, start: int, length: int, chunk_size: int):
    remaining = length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def range_file_response(
    request,
    file_path: str | Path,
    content_type: str,
    *,
    cache_control: str = "no-cache",
    content_disposition: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
):
    """Serve a local file with bounded-memory, single-range HTTP semantics."""
    path = Path(file_path)
    file_size = path.stat().st_size
    range_header = request.headers.get("Range", "").strip()

    if not range_header:
        response = FileResponse(path.open("rb"), content_type=content_type)
        response["Content-Length"] = str(file_size)
    else:
        byte_range = _parse_range(range_header, file_size)
        if byte_range is None:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{file_size}"
            response["Content-Length"] = "0"
        else:
            start, end = byte_range
            length = end - start + 1
            response = StreamingHttpResponse(
                _file_range_iterator(
                    path,
                    start,
                    length,
                    max(1024, int(chunk_size)),
                ),
                status=206,
                content_type=content_type,
            )
            response["Content-Length"] = str(length)
            response["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = cache_control
    if content_disposition:
        response["Content-Disposition"] = content_disposition
    return response
