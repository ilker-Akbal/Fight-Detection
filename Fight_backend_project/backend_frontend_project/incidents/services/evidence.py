from __future__ import annotations

from pathlib import Path

from django.conf import settings


def _allowed_roots() -> list[Path]:
    return [Path(value).resolve() for value in settings.INCIDENT_EVIDENCE_ROOTS]


def validate_evidence_path(raw_path: str) -> tuple[str, bool, str]:
    if not raw_path:
        return "", False, "evidence_path_missing"

    media_root = Path(settings.MEDIA_ROOT).resolve()
    candidate = Path(str(raw_path))
    if not candidate.is_absolute():
        candidate = media_root / candidate
    candidate = candidate.resolve()

    if not any(_is_relative_to(candidate, root) for root in _allowed_roots()):
        return "", False, "evidence_path_outside_allowed_roots"
    if not candidate.exists() or not candidate.is_file():
        return "", False, "evidence_file_missing"

    try:
        stored = candidate.relative_to(media_root).as_posix()
    except ValueError:
        return "", False, "evidence_path_outside_media_root"
    return stored, True, ""


def resolve_incident_evidence_path(incident) -> Path | None:
    if not incident.evidence_valid or not incident.evidence_path:
        return None
    media_root = Path(settings.MEDIA_ROOT).resolve()
    candidate = (media_root / incident.evidence_path).resolve()
    if not any(_is_relative_to(candidate, root) for root in _allowed_roots()):
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
