from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import cv2
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, OuterRef, Subquery
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import role_required
from accounts.models import LoginActivity, UserProfile
from services.email_service import EmailServiceError, send_email
from services.pipeline_bridge.report_reader import build_dashboard_report
from services.pipeline_bridge.runtime_state import runtime
from services.speed_bridge.calibration_writer import (
    is_speed_calibration_ready,
    resolve_speed_calibration_path,
    sync_speed_calibration_file,
)
from speed_detection.models import SpeedCameraConfig
from streams.models import Camera

from .forms import CameraForm, FacultyLocationForm, SpeedCameraConfigForm, UserEditForm
from .models import FacultyLocation


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
    camera_count = Camera.objects.count()
    active_camera_count = Camera.objects.filter(is_active=True).count()
    passive_camera_count = Camera.objects.filter(is_active=False).count()

    fight_camera_count = Camera.objects.filter(
        is_active=True,
        use_fight_detection=True,
    ).count()

    speed_camera_count = SpeedCameraConfig.objects.filter(
        enabled=True,
        camera__is_active=True,
        camera__use_speed_detection=True,
    ).count()

    total_user_count = User.objects.count()

    approved_user_count = UserProfile.objects.filter(status="approved").count()
    pending_user_count = UserProfile.objects.filter(status="pending").count()
    rejected_user_count = UserProfile.objects.filter(status="rejected").count()

    admin_user_count = UserProfile.objects.filter(role="admin").count()
    operator_user_count = UserProfile.objects.filter(role="operator").count()
    viewer_user_count = UserProfile.objects.filter(role="viewer").count()

    active_run = runtime.get()
    pipeline_running = False
    pipeline_pid = None
    pipeline_run_dir = ""

    if active_run is not None and getattr(active_run, "process", None) is not None:
        pipeline_running = active_run.process.poll() is None
        pipeline_pid = active_run.process.pid
        pipeline_run_dir = str(active_run.run_dir)

    fight_incident_count = len(_admin_collect_incidents())
    speed_record_count = len(_admin_collect_speed_records())

    context = {
        "camera_count": camera_count,
        "active_camera_count": active_camera_count,
        "passive_camera_count": passive_camera_count,
        "fight_camera_count": fight_camera_count,
        "speed_camera_count": speed_camera_count,

        "user_count": UserProfile.objects.count(),
        "total_user_count": total_user_count,
        "approved_user_count": approved_user_count,
        "pending_user_count": pending_user_count,
        "rejected_user_count": rejected_user_count,

        "admin_user_count": admin_user_count,
        "operator_user_count": operator_user_count,
        "viewer_user_count": viewer_user_count,

        "pipeline_running": pipeline_running,
        "pipeline_pid": pipeline_pid,
        "pipeline_run_dir": pipeline_run_dir,

        "fight_incident_count": fight_incident_count,
        "speed_record_count": speed_record_count,
    }

    return render(request, "adminx/dashboard.html", context)


@never_cache
@login_required
@role_required(["admin"])
@require_GET
def camera_preview_frame(request, pk):
    camera = get_object_or_404(Camera, pk=pk)

    source = str(camera.source or "").strip()

    if not source:
        raise Http404("Kamera kaynağı boş.")

    cap = None

    try:
        if source.isdigit():
            cap = cv2.VideoCapture(int(source))
        else:
            cap = cv2.VideoCapture(source)

        if cap is None or not cap.isOpened():
            raise Http404("Kamera açılamadı.")

        ok, frame = cap.read()

        if not ok or frame is None:
            raise Http404("Kare okunamadı.")

        max_width = 960
        h, w = frame.shape[:2]

        if w > max_width:
            scale = max_width / float(w)
            new_w = max_width
            new_h = int(h * scale)
            frame = cv2.resize(frame, (new_w, new_h))

        ok, buffer = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 90],
        )

        if not ok:
            raise Http404("Kare encode edilemedi.")

        from io import BytesIO

        bio = BytesIO(buffer.tobytes())
        response = FileResponse(bio, content_type="image/jpeg")
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"

        return response

    finally:
        if cap is not None:
            cap.release()


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
def camera_list(request):
    for camera in Camera.objects.all():
        SpeedCameraConfig.objects.get_or_create(camera=camera)

    cameras = (
        Camera.objects
        .select_related("speed_config")
        .all()
        .order_by("name", "camera_id")
    )

    for camera in cameras:
        try:
            speed_config = camera.speed_config
        except SpeedCameraConfig.DoesNotExist:
            speed_config = None

        camera.speed_calibration_ready = False
        camera.speed_calibration_reason = "Hız konfigürasyonu yok."
        camera.speed_threshold_kmh = "-"

        if speed_config is not None:
            try:
                camera.speed_threshold_kmh = (
                    speed_config.speed_limit_kmh + speed_config.tolerance_kmh
                )
            except Exception:
                camera.speed_threshold_kmh = "-"

            try:
                cal_path = resolve_speed_calibration_path(
                    speed_config.calibration_path,
                    camera_id=camera.camera_id,
                )
                ready, reason = is_speed_calibration_ready(cal_path)
                camera.speed_calibration_ready = ready
                camera.speed_calibration_reason = reason
            except Exception as exc:
                camera.speed_calibration_ready = False
                camera.speed_calibration_reason = str(exc)

    active_camera_count = sum(1 for camera in cameras if camera.is_active)

    fight_camera_count = sum(
        1 for camera in cameras
        if camera.is_active and camera.use_fight_detection
    )

    speed_camera_count = sum(
        1 for camera in cameras
        if (
            camera.is_active
            and camera.use_speed_detection
            and hasattr(camera, "speed_config")
            and camera.speed_config.enabled
        )
    )

    return render(
        request,
        "adminx/camera_list.html",
        {
            "cameras": cameras,
            "active_camera_count": active_camera_count,
            "fight_camera_count": fight_camera_count,
            "speed_camera_count": speed_camera_count,
        },
    )


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
    camera = get_object_or_404(Camera, pk=pk)
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
    camera = get_object_or_404(Camera, pk=pk)

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


def _admin_collect_incidents():
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
    selected_faculty = request.GET.get("faculty", "").strip()
    selected_camera = request.GET.get("camera", "").strip()
    search_query = request.GET.get("q", "").strip()

    all_incidents = _admin_collect_incidents()

    filtered_incidents = _filter_admin_incidents(
        incidents=all_incidents,
        selected_faculty=selected_faculty,
        selected_camera=selected_camera,
        search_query=search_query,
    )

    fight_count = sum(
        1 for item in filtered_incidents
        if str(item.get("final_label", "")).lower() == "fight"
    )

    faculty_count = len(
        {
            item.get("faculty")
            for item in filtered_incidents
            if item.get("faculty")
        }
    )

    paginator = Paginator(filtered_incidents, ADMIN_INCIDENTS_PER_PAGE)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    query_params = request.GET.copy()

    if "page" in query_params:
        query_params.pop("page")

    return render(
        request,
        "adminx/incident_list.html",
        {
            "incidents": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,

            "incident_count": len(filtered_incidents),
            "all_incident_count": len(all_incidents),
            "fight_count": fight_count,
            "faculty_count": faculty_count,

            "selected_faculty": selected_faculty,
            "selected_camera": selected_camera,
            "search_query": search_query,
            "query_string": query_params.urlencode(),

            "faculty_options": _admin_faculty_location_options(),
            "camera_options": Camera.objects.all().order_by("name", "camera_id"),
        },
    )


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
    selected_faculty = request.GET.get("faculty", "").strip()
    selected_camera = request.GET.get("camera", "").strip()
    search_query = request.GET.get("q", "").strip()

    all_records = _admin_collect_speed_records()

    filtered_records = _filter_admin_speed_records(
        records=all_records,
        selected_faculty=selected_faculty,
        selected_camera=selected_camera,
        search_query=search_query,
    )

    camera_count = len(
        {
            item.get("camera_id")
            for item in filtered_records
            if item.get("camera_id")
        }
    )

    max_speed_kmh = "-"
    numeric_speeds = []

    for item in filtered_records:
        try:
            numeric_speeds.append(float(item.get("speed_kmh")))
        except Exception:
            pass

    if numeric_speeds:
        max_speed_kmh = f"{max(numeric_speeds):.2f}"

    paginator = Paginator(filtered_records, ADMIN_SPEED_RECORDS_PER_PAGE)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    query_params = request.GET.copy()

    if "page" in query_params:
        query_params.pop("page")

    return render(
        request,
        "adminx/speed_record_list.html",
        {
            "records": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,

            "record_count": len(filtered_records),
            "all_record_count": len(all_records),
            "camera_count": camera_count,
            "max_speed_kmh": max_speed_kmh,

            "selected_faculty": selected_faculty,
            "selected_camera": selected_camera,
            "search_query": search_query,
            "query_string": query_params.urlencode(),

            "faculty_options": _admin_faculty_location_options(),
            "camera_options": Camera.objects.all().order_by("name", "camera_id"),
        },
    )