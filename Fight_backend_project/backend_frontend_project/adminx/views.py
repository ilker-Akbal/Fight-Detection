from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import cv2
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Count, OuterRef, Subquery
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import role_required
from accounts.models import LoginActivity, UserProfile
from services.access_scope import get_user_accessible_cameras
from services.email_service import EmailServiceError, send_email
from services.pipeline_bridge.report_reader import build_dashboard_report
from services.speed_bridge.calibration_writer import (
    sync_speed_calibration_file,
)
from speed_detection.models import SpeedCameraConfig
from streams.models import Camera
from incidents.models import Incident

from .forms import CameraForm, FacultyLocationForm, LocationForm, SpeedCameraConfigForm, UserEditForm
from .forms import UserCreateForm
from .models import FacultyLocation, Location


MAX_ADMIN_INCIDENT_RUNS = 30
MAX_ADMIN_INCIDENT_ROWS = 500
ADMIN_INCIDENTS_PER_PAGE = 6

MAX_ADMIN_SPEED_RUNS = 30
MAX_ADMIN_SPEED_ROWS = 500
ADMIN_SPEED_RECORDS_PER_PAGE = 6


@never_cache
@login_required
@role_required(["admin"])
def dashboard(request):
    from guvenlik.presentation import camera_cards, incident_card, system_snapshot, visible_incidents
    system, health = system_snapshot()
    cards = camera_cards(request.user, system, health)
    events = visible_incidents(request.user)
    return render(request, "adminx/dashboard.html", {
        "system": system, "camera_count": len(cards),
        "online_count": sum(card["tone"] == "success" for card in cards),
        "recent_events": [incident_card(item) for item in events[:6]],
        "event_count": events.count(),
        "attention": [card for card in cards if card["tone"] in {"warning", "danger"}][:8],
        "has_locations": Location.objects.exists() or FacultyLocation.objects.exists(),
    })


@never_cache
@login_required
@role_required(["admin"])
@require_GET
def camera_preview_frame(request, pk):
    camera = get_object_or_404(get_user_accessible_cameras(request.user), pk=pk)

    from services.pipeline_bridge.live_preview import snapshot
    from io import BytesIO
    import numpy as np
    data = snapshot(camera.camera_id)
    frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR) if data else None
    if frame is None:
        raise Http404("Canlı izlemeyi başlatın; kamera görüntüsü henüz hazır değil.")
    height, width = frame.shape[:2]
    if width > 960:
        frame = cv2.resize(frame, (960, int(height * 960 / width)))
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise Http404("Kare hazırlanamadı.")
    response = FileResponse(BytesIO(encoded.tobytes()), content_type="image/jpeg")
    response["Cache-Control"] = "no-store"
    return response

@never_cache
@login_required
@role_required(["admin"])
def user_list(request):
    latest_login_activity = LoginActivity.objects.filter(
        user=OuterRef("user")
    ).order_by("-login_at")

    users = (
        UserProfile.objects.select_related("user")
        .annotate(
            login_count=Count("user__login_activities"),
            last_ip=Subquery(latest_login_activity.values("ip_address")[:1]),
            last_role_at_login=Subquery(
                latest_login_activity.values("role_at_login")[:1]
            ),
            last_activity_at=Subquery(
                latest_login_activity.values("login_at")[:1]
            ),
        )
        .order_by("-user__last_login", "-user__date_joined")
    )

    from django.db.models import Prefetch
    from .models import UserSecurityAssignment
    users = users.prefetch_related(Prefetch("user__security_assignments",
        queryset=UserSecurityAssignment.objects.filter(active=True, security_unit__active=True)
        .select_related("security_unit"), to_attr="active_security_assignments"))
    return render(
        request,
        "adminx/user_list.html",
        {
            "users": users,
            "user_count": UserProfile.objects.count(),
            "approved_user_count": UserProfile.objects.filter(status="approved").count(),
            "pending_user_count": UserProfile.objects.filter(status="pending").count(),
            "rejected_user_count": UserProfile.objects.filter(status="rejected").count(),
        },
    )


def _admin_url_prefix():
    prefix = getattr(settings, "FORCE_SCRIPT_NAME", None) or getattr(
        settings,
        "URL_PREFIX",
        "",
    )

    if not prefix:
        return ""

    return str(prefix).rstrip("/")


def _admin_frontend_url(path):
    frontend = getattr(settings, "FRONTEND_URL", "http://127.0.0.1:8000").rstrip("/")
    prefix = _admin_url_prefix()

    if not path.startswith("/"):
        path = f"/{path}"

    return f"{frontend}{prefix}{path}"


@never_cache
@login_required
@role_required(["admin"])
@require_POST
def user_approve(request, pk):
    profile = get_object_or_404(UserProfile, pk=pk)
    profile.status = "approved"
    profile.save()

    login_url = _admin_frontend_url("/accounts/login/")
    logo_url = getattr(
        settings,
        "MAIL_LOGO_URL",
        _admin_frontend_url("/static/images/togu-logo.png"),
    )

    mail_body = f"""
    <div style="margin:0;padding:0;background:#f4f7fb;font-family:Arial,sans-serif;">
      <div style="max-width:640px;margin:0 auto;padding:34px 18px;">
        <div style="background:#ffffff;border-radius:24px;padding:36px 30px;border:1px solid #e5e7eb;box-shadow:0 10px 30px rgba(15,23,42,0.08);text-align:center;">

          <div style="margin-bottom:24px;">
            <img
              src="{logo_url}"
              alt="TOGÜ Logo"
              style="width:120px;height:auto;margin:0 auto;display:block;"
            />
          </div>

          <div style="width:58px;height:58px;border-radius:18px;background:#dcfce7;color:#15803d;margin:0 auto 18px;display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:900;">
            ✓
          </div>

          <h1 style="margin:0 0 12px;color:#0f172a;font-size:26px;font-weight:800;">
            Hesabınız Onaylandı
          </h1>

          <p style="margin:0 auto 22px;max-width:500px;color:#64748b;font-size:15px;line-height:1.75;">
            Akıllı Güvenlik sistemine erişim başvurunuz admin tarafından onaylanmıştır.
            Artık kullanıcı bilgilerinizle sisteme giriş yapabilirsiniz.
          </p>

          <div style="margin:22px 0;padding:18px;border-radius:18px;background:#f8fafc;border:1px solid #e2e8f0;text-align:left;">
            <div style="margin-bottom:10px;">
              <span style="display:block;color:#64748b;font-size:12px;font-weight:700;">Kullanıcı Adı</span>
              <strong style="display:block;color:#0f172a;font-size:15px;font-weight:800;">
                {profile.user.username}
              </strong>
            </div>

            <div style="margin-bottom:10px;">
              <span style="display:block;color:#64748b;font-size:12px;font-weight:700;">E-posta</span>
              <strong style="display:block;color:#0f172a;font-size:15px;font-weight:800;">
                {profile.user.email or "-"}
              </strong>
            </div>

            <div>
              <span style="display:block;color:#64748b;font-size:12px;font-weight:700;">Rol</span>
              <strong style="display:block;color:#0f172a;font-size:15px;font-weight:800;">
                {profile.get_role_display()}
              </strong>
            </div>
          </div>

          <a
            href="{login_url}"
            style="display:inline-block;background:#0f4c81;color:#ffffff;text-decoration:none;padding:14px 28px;border-radius:14px;font-size:15px;font-weight:800;box-shadow:0 12px 24px rgba(15,76,129,0.22);"
          >
            Sisteme Giriş Yap
          </a>

          <p style="margin:24px 0 0;color:#94a3b8;font-size:12px;line-height:1.6;">
            Bu e-posta sistem tarafından otomatik olarak gönderilmiştir.
            Eğer bu işlem hakkında bilginiz yoksa sistem yöneticisiyle iletişime geçiniz.
          </p>

        </div>
      </div>
    </div>
    """

    try:
        send_email(
            to=profile.user.email,
            subject="Akıllı Güvenlik Hesabınız Onaylandı",
            body=mail_body,
        )
    except EmailServiceError as exc:
        print(f"Onay maili gönderilemedi: {exc}")

    return redirect("adminx:user_list")


@never_cache
@login_required
@role_required(["admin"])
@require_POST
def user_reject(request, pk):
    profile = get_object_or_404(UserProfile, pk=pk)
    profile.status = "rejected"
    profile.save()

    try:
        send_email(
            to=profile.user.email,
            subject="Hesap Başvurunuz Reddedildi",
            body=f"""
            <h2>Hesap Başvurunuz Reddedildi</h2>
            <p>Merhaba,</p>
            <p>Akıllı Güvenlik sistemine erişim başvurunuz admin tarafından reddedildi.</p>
            <p>Detaylı bilgi için sistem yöneticisiyle iletişime geçebilirsiniz.</p>
            <p><strong>Kullanıcı:</strong> {profile.user.username}</p>
            """,
        )
    except EmailServiceError as exc:
        print(f"Red maili gönderilemedi: {exc}")

    return redirect("adminx:user_list")


@never_cache
@login_required
@role_required(["admin"])
def user_delete(request, pk):
    profile = get_object_or_404(
        UserProfile.objects.select_related("user"),
        pk=pk,
    )

    if request.method == "POST":
        profile.user.delete()
        return redirect("adminx:user_list")

    return render(
        request,
        "adminx/user_delete.html",
        {"profile": profile},
    )


@never_cache
@login_required
@role_required(["admin"])
def user_update(request, pk):
    profile = get_object_or_404(
        UserProfile.objects.select_related("user"),
        pk=pk,
    )

    form = UserEditForm(
        request.POST or None,
        instance=profile,
        user_instance=profile.user,
    )

    if request.method == "POST" and form.is_valid():
        form.save(user_instance=profile.user)
        return redirect("adminx:user_list")

    return render(
        request,
        "adminx/user_form.html",
        {
            "form": form,
            "page_title": "Kullanıcı Düzenle",
            "submit_label": "Güncelle",
            "profile": profile,
        },
    )


@never_cache
@login_required
@role_required(["admin"])
def user_create(request):
    from django.contrib.auth.models import User
    user = User()
    profile = UserProfile(user=user, status="approved", role="viewer")
    form = UserCreateForm(request.POST or None, instance=profile, user_instance=user)
    if request.method == "POST" and form.is_valid():
        form.save(user_instance=user)
        return redirect("adminx:user_list")
    return render(request, "adminx/user_form.html", {"form": form, "profile": profile,
        "page_title": "Kullanıcı Ekle", "submit_label": "Oluştur"})


@never_cache
@login_required
@role_required(["admin"])
def camera_list(request):
    from guvenlik.presentation import camera_cards, system_snapshot
    system, health = system_snapshot()
    return render(request, "adminx/camera_list.html", {
        "cards": camera_cards(request.user, system, health), "system": system,
    })


@never_cache
@login_required
@role_required(["admin"])
def camera_create(request):
    form = CameraForm(
        request.POST or None,
        request.FILES or None,
    )

    speed_form = SpeedCameraConfigForm(
        request.POST or None,
    )

    if request.method == "POST" and form.is_valid() and speed_form.is_valid():
        camera = form.save()

        speed_config = speed_form.save(commit=False)
        speed_config.camera = camera

        if not camera.use_speed_detection:
            speed_config.enabled = False

        speed_config.save()

        if camera.use_speed_detection and speed_config.enabled:
            sync_speed_calibration_file(camera, speed_config)

        messages.success(
            request,
            f"'{camera.name}' kamerası başarıyla oluşturuldu.",
        )

        return redirect("adminx:camera_list")

    return render(
        request,
        "adminx/camera_form.html",
        {
            "form": form,
            "speed_form": speed_form,
            "page_title": "Kamera Ekle",
            "submit_label": "Kaydet",
        },
    )


@never_cache
@login_required
@role_required(["admin"])
def camera_edit(request, pk):
    camera = get_object_or_404(get_user_accessible_cameras(request.user), pk=pk)
    if camera.source_kind != "LIVE":
        from streams.models import OfflineAsset
        asset = get_object_or_404(OfflineAsset, legacy_camera=camera)
        return redirect("dashboard:offline_detail", pk=asset.pk)
    speed_config, _ = SpeedCameraConfig.objects.get_or_create(camera=camera)

    form = CameraForm(
        request.POST or None,
        request.FILES or None,
        instance=camera,
    )

    speed_form = SpeedCameraConfigForm(
        request.POST or None,
        instance=speed_config,
    )

    if request.method == "POST" and form.is_valid() and speed_form.is_valid():
        camera = form.save()

        speed_config = speed_form.save(commit=False)
        speed_config.camera = camera

        if not camera.use_speed_detection:
            speed_config.enabled = False

        speed_config.save()

        if camera.use_speed_detection and speed_config.enabled:
            sync_speed_calibration_file(camera, speed_config)

        messages.success(
            request,
            f"'{camera.name}' kamerası başarıyla güncellendi.",
        )

        return redirect("adminx:camera_list")

    return render(
        request,
        "adminx/camera_form.html",
        {
            "form": form,
            "speed_form": speed_form,
            "page_title": "Kamera Düzenle",
            "submit_label": "Güncelle",
            "camera": camera,
        },
    )


@never_cache
@login_required
@role_required(["admin"])
def camera_delete(request, pk):
    camera = get_object_or_404(get_user_accessible_cameras(request.user), pk=pk)
    if camera.source_kind != "LIVE":
        return redirect("dashboard:offline_list")

    if request.method == "POST":
        camera_name = camera.name
        uploaded_file = getattr(camera, "uploaded_video", None)

        try:
            with transaction.atomic():
                camera.delete()

        except IntegrityError:
            Camera.objects.filter(pk=pk).update(is_active=False)

            messages.warning(
                request,
                (
                    f"'{camera_name}' kamerası geçmiş kayıtlarla ilişkili olduğu için "
                    "tamamen silinemedi. Veri bütünlüğünü korumak için kamera pasife alındı."
                ),
            )

            return redirect("adminx:camera_list")

        if uploaded_file:
            try:
                uploaded_file.delete(save=False)
            except Exception as exc:
                print(f"Kamera video dosyası silinemedi: {exc}")

        messages.success(
            request,
            f"'{camera_name}' kamerası başarıyla silindi.",
        )

        return redirect("adminx:camera_list")

    return render(
        request,
        "adminx/camera_delete.html",
        {"camera": camera},
    )


@never_cache
@login_required
@role_required(["admin"])
def faculty_location_list(request):
    form = FacultyLocationForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Fakülte / mevki başarıyla eklendi.")
        return redirect("adminx:faculty_location_list")

    items = FacultyLocation.objects.all().order_by("name")

    total_count = items.count()
    active_count = items.filter(is_active=True).count()
    passive_count = items.filter(is_active=False).count()

    return render(
        request,
        "adminx/faculty_location_list.html",
        {
            "form": form,
            "items": items,
            "total_count": total_count,
            "active_count": active_count,
            "passive_count": passive_count,
            "physical_locations": Location.objects.select_related("parent"),
        },
    )


@never_cache
@login_required
@role_required(["admin"])
def faculty_location_edit(request, pk):
    item = get_object_or_404(FacultyLocation, pk=pk)
    form = FacultyLocationForm(request.POST or None, instance=item)

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Fakülte / mevki başarıyla güncellendi.")
        return redirect("adminx:faculty_location_list")

    items = FacultyLocation.objects.all().order_by("name")

    total_count = items.count()
    active_count = items.filter(is_active=True).count()
    passive_count = items.filter(is_active=False).count()

    return render(
        request,
        "adminx/faculty_location_list.html",
        {
            "form": form,
            "items": items,
            "editing_item": item,
            "physical_locations": Location.objects.select_related("parent"),
            "total_count": total_count,
            "active_count": active_count,
            "passive_count": passive_count,
        },
    )


@never_cache
@login_required
@role_required(["admin"])
@require_POST
def faculty_location_delete(request, pk):
    item = get_object_or_404(FacultyLocation, pk=pk)
    item.delete()

    messages.success(request, "Fakülte / mevki başarıyla silindi.")

    return redirect("adminx:faculty_location_list")


@never_cache
@login_required
@role_required(["admin"])
@require_POST
def faculty_location_toggle(request, pk):
    item = get_object_or_404(FacultyLocation, pk=pk)
    item.is_active = not item.is_active
    item.save(update_fields=["is_active", "updated_at"])

    messages.success(request, "Fakülte / mevki durumu güncellendi.")

    return redirect("adminx:faculty_location_list")


@never_cache
@login_required
@role_required(["admin"])
def location_edit(request, pk=None):
    location = get_object_or_404(Location, pk=pk) if pk else None
    form = LocationForm(request.POST or None, instance=location)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Lokasyon kaydedildi.")
        return redirect("adminx:faculty_location_list")
    return render(request, "adminx/location_form.html", {"form": form, "location": location})


def _admin_pipeline_runs_root() -> Path:
    return Path(settings.MEDIA_ROOT) / "pipeline_runs"


def _admin_run_dirs(limit: int = MAX_ADMIN_INCIDENT_RUNS) -> list[Path]:
    root = _admin_pipeline_runs_root()

    if not root.exists() or not root.is_dir():
        return []

    dirs = [p for p in root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    return dirs[:limit]


def _admin_active_sources():
    return [
        {
            "camera_id": cam.camera_id,
            "source": cam.source,
            "name": cam.name,
            "description": cam.description,
        }
        for cam in Camera.objects.filter(is_active=True).order_by("-created_at")
    ]


def _admin_format_ts(value):
    if value in (None, "", "-"):
        return "-"

    s = str(value).strip().replace("T", " ")

    if "." in s:
        s = s.split(".", 1)[0]

    return s


def _admin_read_run_report(run_dir: Path):
    try:
        report = build_dashboard_report(
            run_dir=str(run_dir),
            sources=_admin_active_sources(),
            process_alive=False,
            pid=None,
            started_at=None,
            return_code=None,
            media_root=settings.MEDIA_ROOT,
        )

        report["run_name"] = report.get("run_name") or run_dir.name
        return report

    except Exception:
        return None


def _safe_file_name(value) -> str:
    if not value:
        return ""

    text = str(value).strip().replace("\\", "/")
    return text.split("/")[-1]


def _admin_incident_clip_url(run_name, clip_path):
    if not run_name or not clip_path:
        return ""

    clip_name = _safe_file_name(clip_path)

    try:
        return reverse("dashboard:incident_video", args=[run_name, clip_name])
    except Exception:
        prefix = _admin_url_prefix() or "/fight-detection"
        return f"{prefix}/dashboard/incident-video/{run_name}/{clip_name}/"


def _admin_faculty_users_map():
    profiles = (
        UserProfile.objects
        .select_related("user")
        .filter(status="approved")
        .order_by("faculty", "user__username")
    )

    out = {}

    for profile in profiles:
        if not profile.faculty:
            continue

        out.setdefault(profile.faculty, []).append(
            {
                "username": profile.user.username,
                "email": profile.user.email,
                "role": profile.get_role_display(),
            }
        )

    return out


def _admin_faculty_location_options():
    return [
        (item.code, item.name)
        for item in FacultyLocation.objects.filter(is_active=True).order_by("name")
    ]


def _admin_faculty_location_label(value):
    if not value:
        return "-"

    item = FacultyLocation.objects.filter(code=value).first()

    if item:
        return item.name

    return str(value)


def _admin_collect_legacy_incidents():
    camera_map = {
        cam.camera_id: cam
        for cam in Camera.objects.all()
    }

    faculty_users = _admin_faculty_users_map()

    rows = []
    seen = set()

    for run_dir in _admin_run_dirs():
        report = _admin_read_run_report(run_dir)

        if not report:
            continue

        run_name = report.get("run_name") or run_dir.name

        for row in report.get("recent_incidents", []):
            camera_id = row.get("camera_id") or "-"
            incident_id = row.get("incident_id") or "-"
            clip_path = row.get("clip_path") or ""

            key = (
                run_name,
                camera_id,
                incident_id,
                clip_path,
            )

            if key in seen:
                continue

            seen.add(key)

            camera = camera_map.get(camera_id)
            faculty_value = camera.faculty if camera else None
            faculty_label = _admin_faculty_location_label(faculty_value)

            rows.append(
                {
                    "run_name": run_name,
                    "camera_id": camera_id,
                    "camera_name": camera.name if camera else "-",
                    "camera_source": camera.source if camera else "-",
                    "faculty": faculty_value or "",
                    "faculty_label": faculty_label,
                    "faculty_users": faculty_users.get(faculty_value, []),
                    "incident_id": incident_id,
                    "start_ts": _admin_format_ts(row.get("start_ts")),
                    "end_ts": _admin_format_ts(row.get("end_ts")),
                    "final_label": row.get("final_label") or "-",
                    "clip_path": clip_path,
                    "clip_url": _admin_incident_clip_url(run_name, clip_path),
                    "part_count": row.get("part_count", "-"),
                }
            )

    rows.sort(
        key=lambda x: (
            str(x.get("end_ts") or ""),
            str(x.get("start_ts") or ""),
            str(x.get("incident_id") or ""),
        ),
        reverse=True,
    )

    return rows[:MAX_ADMIN_INCIDENT_ROWS]


def _admin_collect_incidents():
    operational = list(
        Incident.objects
        .select_related("camera", "camera__location", "acknowledged_by", "acknowledged_unit")
        .order_by("-detected_at", "-pk")[:MAX_ADMIN_INCIDENT_ROWS]
    )
    if not operational:
        return _admin_collect_legacy_incidents()

    faculty_users = _admin_faculty_users_map()
    rows = []
    for incident in operational:
        camera = incident.camera
        faculty_value = camera.faculty or ""
        rows.append(
            {
                "run_name": incident.run_id,
                "camera_id": camera.camera_id,
                "camera_name": camera.name,
                "camera_source": camera.source,
                "faculty": faculty_value,
                "faculty_label": camera.get_location_display(),
                "faculty_users": faculty_users.get(faculty_value, []),
                "incident_id": incident.external_incident_id,
                "start_ts": _admin_format_ts(incident.detected_at.isoformat()),
                "end_ts": _admin_format_ts(incident.finalized_at.isoformat()),
                "final_label": incident.label or incident.incident_type,
                "clip_path": "",
                "clip_url": (
                    reverse("dashboard:incident_evidence", args=[incident.pk])
                    if incident.evidence_valid
                    else ""
                ),
                "part_count": incident.part_count,
                "status": incident.status,
                "routing_state": incident.routing_state,
            }
        )
    return rows


def _filter_admin_incidents(incidents, selected_faculty, selected_camera, search_query):
    filtered = incidents

    if selected_faculty:
        filtered = [
            item for item in filtered
            if item.get("faculty") == selected_faculty
        ]

    if selected_camera:
        filtered = [
            item for item in filtered
            if item.get("camera_id") == selected_camera
        ]

    if search_query:
        q = search_query.lower()

        filtered = [
            item for item in filtered
            if q in str(item.get("incident_id", "")).lower()
            or q in str(item.get("run_name", "")).lower()
            or q in str(item.get("camera_id", "")).lower()
            or q in str(item.get("camera_name", "")).lower()
            or q in str(item.get("faculty_label", "")).lower()
            or q in str(item.get("final_label", "")).lower()
        ]

    return filtered


@never_cache
@login_required
@role_required(["admin"])
def incident_list(request):
    query = request.GET.copy()
    return redirect(reverse("dashboard:history") + ("?" + query.urlencode() if query else ""))


def _admin_speed_runs_root() -> Path:
    return Path(settings.MEDIA_ROOT) / "speed_runs"


def _admin_speed_run_dirs(limit: int = MAX_ADMIN_SPEED_RUNS) -> list[Path]:
    root = _admin_speed_runs_root()

    if not root.exists() or not root.is_dir():
        return []

    dirs = [p for p in root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    return dirs[:limit]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists() or not path.is_file():
        return []

    rows = []

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()

                if not line:
                    continue

                try:
                    value = json.loads(line)
                except Exception:
                    continue

                if isinstance(value, dict):
                    rows.append(value)
    except Exception:
        return []

    return rows


def _format_speed_value(value) -> str:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "-"


def _format_speed_time(row: dict, fallback_path: Path | None = None) -> str:
    for key in ("created_at_text", "created_at", "time_text", "datetime", "timestamp_text"):
        value = row.get(key)

        if value not in (None, "", "-"):
            if isinstance(value, (int, float)):
                try:
                    return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

            return _admin_format_ts(value)

    if fallback_path is not None:
        try:
            return datetime.fromtimestamp(fallback_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    return "-"


def _speed_media_url(run_name: str, path_value, kind: str) -> str:
    file_name = _safe_file_name(path_value)

    if not run_name or not file_name:
        return ""

    try:
        if kind == "snapshot":
            return reverse("speed_detection:snapshot", args=[run_name, file_name])

        if kind == "clip":
            return reverse("speed_detection:clip", args=[run_name, file_name])
    except Exception:
        return ""

    return ""


def _admin_collect_speed_records():
    camera_map = {
        cam.camera_id: cam
        for cam in Camera.objects.all()
    }

    faculty_users = _admin_faculty_users_map()

    rows = []
    seen = set()

    for run_dir in _admin_speed_run_dirs():
        run_name = run_dir.name

        event_files = [
            run_dir / "speed_events.jsonl",
            run_dir / "events" / "speed_events.jsonl",
        ]

        events_dir = run_dir / "events"

        if events_dir.exists():
            event_files.extend(sorted(events_dir.glob("*speed*.jsonl")))

        for event_file in event_files:
            for row in _read_jsonl(event_file):
                camera_id = str(row.get("camera_id") or "-")
                track_id = row.get("track_id") or "-"
                frame_idx = row.get("frame_idx") or "-"

                snapshot_path = (
                    row.get("snapshot_path")
                    or row.get("snapshot")
                    or row.get("snapshot_file")
                    or ""
                )

                clip_path = (
                    row.get("clip_path")
                    or row.get("clip")
                    or row.get("clip_file")
                    or ""
                )

                key = (
                    run_name,
                    camera_id,
                    track_id,
                    frame_idx,
                    snapshot_path,
                    clip_path,
                )

                if key in seen:
                    continue

                seen.add(key)

                camera = camera_map.get(camera_id)
                faculty_value = camera.faculty if camera else None
                faculty_label = _admin_faculty_location_label(faculty_value)

                speed_kmh = _format_speed_value(row.get("speed_kmh"))
                speed_limit = row.get("speed_limit_kmh", "-")
                tolerance = row.get("tolerance_kmh", "-")

                try:
                    threshold = float(speed_limit) + float(tolerance)
                    threshold_text = f"{threshold:.2f}"
                except Exception:
                    threshold_text = _format_speed_value(row.get("threshold_kmh"))

                rows.append(
                    {
                        "run_name": run_name,
                        "camera_id": camera_id,
                        "camera_name": camera.name if camera else "-",
                        "camera_source": camera.source if camera else "-",
                        "faculty": faculty_value or "",
                        "faculty_label": faculty_label,
                        "faculty_users": faculty_users.get(faculty_value, []),

                        "vehicle_class": row.get("vehicle_class") or row.get("class_name") or "-",
                        "track_id": track_id,
                        "frame_idx": frame_idx,
                        "speed_kmh": speed_kmh,
                        "speed_limit_kmh": speed_limit,
                        "tolerance_kmh": tolerance,
                        "threshold_kmh": threshold_text,
                        "created_at_text": _format_speed_time(row, event_file),

                        "snapshot_path": snapshot_path,
                        "clip_path": clip_path,
                        "snapshot_url": _speed_media_url(run_name, snapshot_path, "snapshot"),
                        "clip_url": _speed_media_url(run_name, clip_path, "clip"),
                    }
                )

    rows.sort(
        key=lambda x: (
            str(x.get("created_at_text") or ""),
            str(x.get("run_name") or ""),
            str(x.get("frame_idx") or ""),
        ),
        reverse=True,
    )

    return rows[:MAX_ADMIN_SPEED_ROWS]


def _filter_admin_speed_records(records, selected_faculty, selected_camera, search_query):
    filtered = records

    if selected_faculty:
        filtered = [
            item for item in filtered
            if item.get("faculty") == selected_faculty
        ]

    if selected_camera:
        filtered = [
            item for item in filtered
            if item.get("camera_id") == selected_camera
        ]

    if search_query:
        q = search_query.lower()

        filtered = [
            item for item in filtered
            if q in str(item.get("run_name", "")).lower()
            or q in str(item.get("camera_id", "")).lower()
            or q in str(item.get("camera_name", "")).lower()
            or q in str(item.get("faculty_label", "")).lower()
            or q in str(item.get("vehicle_class", "")).lower()
            or q in str(item.get("track_id", "")).lower()
            or q in str(item.get("speed_kmh", "")).lower()
        ]

    return filtered


@never_cache
@login_required
@role_required(["admin"])
def speed_record_list(request):
    query = request.GET.copy()
    query["type"] = "SPEED"
    return redirect(reverse("dashboard:history") + "?" + query.urlencode())
