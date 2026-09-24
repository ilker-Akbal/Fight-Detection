"""Compatibility for development media URLs: historical assets retain scope."""
from pathlib import Path

from django.http import Http404
from django.views.static import serve


def scoped_media(request, path, document_root=None, **kwargs):
    from streams.models import OfflineAsset, OfflineResult
    from services.pipeline_bridge.offline_analysis import accessible_assets
    root = Path(document_root).resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise Http404
    assets = OfflineAsset.objects.filter(file_path=str(candidate))
    results = OfflineResult.objects.filter(evidence_path=candidate.relative_to(root).as_posix())
    if assets.exists() or results.exists():
        allowed = accessible_assets(request.user)
        if not (assets.filter(pk__in=allowed).exists() or results.filter(run__asset__in=allowed).exists()):
            raise Http404
    # Legacy historical evidence retains exactly the original incident access.
    from incidents.models import Incident
    from services.incident_access import user_can_view_incident
    legacy = Incident.objects.filter(camera__source_kind="OFFLINE", evidence_path=candidate.relative_to(root).as_posix())
    if legacy.exists() and not any(user_can_view_incident(request.user, row) for row in legacy):
        raise Http404
    return serve(request, path, document_root=document_root, **kwargs)
