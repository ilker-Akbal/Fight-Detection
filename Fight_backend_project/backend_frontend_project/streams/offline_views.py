"""Location-scoped historical video workflows; no live source capture."""
import json
import mimetypes
import re
import uuid
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.files.storage import FileSystemStorage
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import role_required
from services.access_scope import get_user_location_scope
from services.pipeline_bridge.offline_analysis import accessible_assets, create_run, job_root
from streams.models import OfflineAsset, OfflineRun, OfflineResult


class UploadForm(forms.Form):
    name = forms.CharField(label="Video adı", max_length=120)
    location = forms.ModelChoiceField(label="Lokasyon", queryset=None)
    video = forms.FileField(label="Video", widget=forms.FileInput(attrs={"accept": "video/*"}))

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = get_user_location_scope(user)

    def clean_video(self):
        video = self.cleaned_data["video"]
        if Path(video.name).suffix.lower() not in {".mp4", ".avi", ".mov", ".mkv", ".webm"} or video.size > 1024**3:
            raise forms.ValidationError("Desteklenen video formatı ve en fazla 1 GB dosya kullanın.")
        return video


class AnalysisForm(forms.Form):
    analysis_type = forms.ChoiceField(label="Analiz", choices=OfflineRun._meta.get_field("analysis_type").choices)
    calibration = forms.JSONField(label="Hız kalibrasyonu (JSON)", required=False,
        help_text="Hız analizi için mevcut iki çizgi / mesafe veya ölçek kalibrasyonu belgesi gereklidir.",
        widget=forms.Textarea(attrs={"rows": 8}))


@login_required
@role_required(["admin", "operator", "viewer"])
@require_GET
def listing(request):
    page = Paginator(accessible_assets(request.user).order_by("-created_at"), 20).get_page(request.GET.get("page"))
    return render(request, "operations/offline_list.html", {"assets": page, "page_obj": page})


@login_required
@role_required(["admin", "operator"])
def upload(request):
    form = UploadForm(request.user, request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        video = form.cleaned_data["video"]
        storage = FileSystemStorage(location=job_root() / "assets")
        name = storage.save(uuid.uuid4().hex + Path(video.name).suffix.lower(), video)
        asset = OfflineAsset.objects.create(name=form.cleaned_data["name"], location=form.cleaned_data["location"],
                                            file_path=storage.path(name))
        return redirect("dashboard:offline_detail", pk=asset.pk)
    return render(request, "operations/offline_upload.html", {"form": form})


@login_required
@role_required(["admin", "operator", "viewer"])
@require_GET
def detail(request, pk):
    asset = get_object_or_404(accessible_assets(request.user), pk=pk)
    initial = {}
    # Read-only compatibility: existing calibration stays attached to its legacy
    # camera. Explicit re-analysis snapshots it; no calibration math is changed.
    if asset.legacy_camera_id:
        speed = getattr(asset.legacy_camera, "speed_config", None)
        if speed:
            from services.speed_bridge.calibration_writer import resolve_speed_calibration_path
            path = resolve_speed_calibration_path(speed.calibration_path, camera_id=asset.legacy_camera.camera_id)
            try:
                initial["calibration"] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    from incidents.models import Incident
    legacy = Incident.objects.filter(camera_id=asset.legacy_camera_id) if asset.legacy_camera_id else Incident.objects.none()
    runs = asset.runs.order_by("-created_at")[:20]
    return render(request, "operations/offline_detail.html", {
        "asset": asset, "runs": runs, "legacy": legacy[:100], "form": AnalysisForm(initial=initial),
        "speed_width": int(settings.SPEED_PIPELINE_DEFAULTS.get("resize_width", 960)),
        "can_manage": request.user.is_staff or request.user.is_superuser or request.user.profile.role in {"admin", "operator"}})


@login_required
@role_required(["admin", "operator"])
@require_POST
def submit(request, pk):
    asset = get_object_or_404(accessible_assets(request.user), pk=pk)
    form = AnalysisForm(request.POST)
    if form.is_valid():
        try:
            create_run(asset, form.cleaned_data["analysis_type"], {"calibration": form.cleaned_data.get("calibration")})
            return redirect("dashboard:offline_detail", pk=pk)
        except ValueError:
            form.add_error("calibration", "Geçerli ve tamamlanmış hız kalibrasyonu gereklidir.")
    return render(request, "operations/offline_submit.html", {"asset": asset, "form": form}, status=400)


@login_required
@role_required(["admin", "operator"])
@require_POST
def cancel(request, run_id):
    run = get_object_or_404(OfflineRun, pk=run_id, asset__in=accessible_assets(request.user))
    if run.state in {"QUEUED", "PROCESSING"}:
        OfflineRun.objects.filter(pk=run.pk, state__in=["QUEUED", "PROCESSING"]).update(cancel_requested=True)
    return redirect("dashboard:offline_detail", pk=run.asset_id)


def _video_response(request, path):
    """Authenticated byte ranges for original playback/seek; bounded reads."""
    path = Path(path)
    if not path.is_file():
        raise Http404("Video dosyası bulunamadı.")
    size = path.stat().st_size
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    value = request.headers.get("Range")
    if not value:
        response = FileResponse(path.open("rb"), content_type=content_type)
    else:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
        if not match or not any(match.groups()):
            return HttpResponse(status=416, headers={"Content-Range": f"bytes */{size}"})
        first, last = match.groups()
        start = int(first) if first else max(0, size - int(last))
        end = min(size - 1, int(last)) if first and last else size - 1
        if start > end or start >= size:
            return HttpResponse(status=416, headers={"Content-Range": f"bytes */{size}"})
        def chunks():
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = handle.read(min(256 * 1024, remaining))
                    if not chunk:
                        return
                    remaining -= len(chunk)
                    yield chunk
        response = StreamingHttpResponse(chunks(), status=206, content_type=content_type)
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = end - start + 1
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@role_required(["admin", "operator", "viewer"])
@require_GET
def playback(request, pk):
    asset = get_object_or_404(accessible_assets(request.user), pk=pk)
    return _video_response(request, asset.file_path)


@login_required
@role_required(["admin", "operator", "viewer"])
@require_GET
def evidence(request, pk):
    result = get_object_or_404(OfflineResult, pk=pk, run__asset__in=accessible_assets(request.user))
    from incidents.services.evidence import validate_evidence_path
    path, valid, _ = validate_evidence_path(result.evidence_path)
    if not valid:
        raise Http404
    return _video_response(request, Path(settings.MEDIA_ROOT) / path)
