"""Fail-closed compatibility URLs for explicitly registered, scoped media."""
from pathlib import Path

from django.http import Http404
from django.views.static import serve


def scoped_media(request, path, document_root=None, **kwargs):
    from streams.models import OfflineAsset, OfflineResult
    from services.pipeline_bridge.offline_analysis import accessible_assets
    root = Path(document_root).resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise Http404
    user = request.user
    if not user.is_authenticated or not user.is_active:
        raise Http404
    profile = getattr(user, "profile", None)
    if not (user.is_staff or user.is_superuser or (profile and profile.is_approved)):
        raise Http404
    # Operational JSON, logs, telemetry and previews are never generic media.
    if candidate.suffix.lower() not in {".mp4", ".webm", ".avi", ".mov", ".mkv", ".m4v", ".jpg", ".jpeg", ".png"}:
        raise Http404
    assets = OfflineAsset.objects.filter(file_path=str(candidate))
    results = OfflineResult.objects.filter(evidence_path=candidate.relative_to(root).as_posix())
    allowed = accessible_assets(user)
    recognized = assets.filter(pk__in=allowed).exists() or results.filter(run__asset__in=allowed).exists()
    # Both LIVE and historical evidence use the canonical incident authority.
    from incidents.models import Incident
    from services.incident_access import user_can_view_incident
    from incidents.services.evidence import resolve_incident_evidence_path
    incidents = Incident.objects.filter(evidence_path=candidate.relative_to(root).as_posix())
    recognized = recognized or any(
        user_can_view_incident(user, row) and resolve_incident_evidence_path(row) == candidate
        for row in incidents
    )
    if not recognized:
        raise Http404
    return serve(request, path, document_root=document_root, **kwargs)
