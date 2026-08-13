"""Safely transcode historical incident MP4 files to browser-compatible H.264.

The default is a dry run. Use ``--apply`` only after reviewing its output.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable


def probe(path: Path, ffprobe: str) -> bool:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,pix_fmt:format=duration", "-of", "json", str(path)],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return (stream.get("codec_name") == "h264"
                and stream.get("pix_fmt") == "yuv420p"
                and float(data["format"]["duration"]) > 0)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return False


def transcode(path: Path, ffmpeg: str, ffprobe: str, dry_run: bool) -> str:
    if path.is_symlink():
        return "skipped-symlink"
    if not path.is_file():
        return "skipped-not-file"
    if probe(path, ffprobe):
        return "already-compatible"
    if dry_run:
        return "would-transcode"
    fd, name = tempfile.mkstemp(prefix=f".{path.stem}__h264_", suffix=".mp4", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
             "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "libx264",
             "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(temporary)],
            capture_output=True, check=False,
        )
        if result.returncode != 0 or not probe(temporary, ffprobe):
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1000:])
        os.replace(temporary, path)
        return "transcoded"
    finally:
        temporary.unlink(missing_ok=True)


def incident_paths(directory: Path, recursive: bool) -> Iterable[Path]:
    if not recursive:
        return directory.glob("*.mp4")
    if directory.name.lower() == "incidents":
        return directory.rglob("*.mp4")
    return (
        path
        for path in directory.rglob("*.mp4")
        if "incidents" in (part.lower() for part in path.relative_to(directory).parts[:-1])
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Safely convert incompatible incident MP4 files to H.264/yuv420p. "
            "Runs as a dry run unless --apply is supplied."
        )
    )
    parser.add_argument("directory", type=Path, help="Incident directory to scan")
    parser.add_argument("--apply", action="store_true", help="Perform conversion (default: dry run)")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan nested directories (useful when directory is the pipeline_runs root)",
    )
    args = parser.parse_args()
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        parser.error("ffmpeg and ffprobe must both be available on PATH")
    directory = args.directory.resolve()
    if not directory.is_dir():
        parser.error(f"not a directory: {directory}")
    failed = 0
    for path in sorted(incident_paths(directory, args.recursive)):
        try:
            status = transcode(path, ffmpeg, ffprobe, not args.apply)
        except Exception as exc:
            failed += 1
            status = f"failed: {exc}"
        print(f"{status}\t{path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
