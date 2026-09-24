"""Presentation-only views over existing camera, incident and Supervisor services."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils.dateparse import parse_date
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import role_required
from services.access_scope import get_user_accessible_cameras, is_it_admin
from services.pipeline_bridge.common_preview import preview_context
from .presentation import camera_cards, incident_card, reason_label, system_snapshot, visible_incidents


@never_cache
@login_required
@role_required(["admin", "operator", "viewer"])
@require_GET
def live(request):
    system, health = system_snapshot()
    cards = camera_cards(request.user, system, health)
    capability = request.GET.get("capability", "")
    if capability in {"fight", "speed"}:
        cards = [card for card in cards if card[capability]]
    page = Paginator(cards, 12).get_page(request.GET.get("page"))
    return render(request, "operations/live.html", {
        "no_access": not get_user_accessible_cameras(request.user).exists(),
        "cards": page.object_list, "page_obj": page, "system": system, "capability": capability,
        "events": [incident_card(item) for item in visible_incidents(request.user)[:12]],
    })


@never_cache
@login_required
@role_required(["admin", "operator", "viewer"])
@require_GET
def overview(request):
    system, health = system_snapshot()
    result = {"system": system}
    result["cameras"] = camera_cards(request.user, system, health)
    if is_it_admin(request.user):
        result["workers"] = worker_cards(health, system)
    if request.GET.get("live") == "1":
        result["cameras"] = camera_cards(request.user, system, health)
        result["events_html"] = render_to_string("operations/event_feed.html", {
            "events": [incident_card(item) for item in visible_incidents(request.user)[:12]],
        }, request=request)
    return JsonResponse(result)


@never_cache
@login_required
@role_required(["admin", "operator", "viewer"])
@require_GET
def history(request):
    cameras = get_user_accessible_cameras(request.user).select_related("location")
    rows = visible_incidents(request.user)
    kind = request.GET.get("type", "")
    if kind in {"FIGHT", "SPEED"}:
        rows = rows.filter(incident_type=kind)
    camera = request.GET.get("camera", "")
    location = request.GET.get("location", "")
    query = request.GET.get("q", "").strip()
    if camera:
        rows = rows.filter(camera__camera_id=camera)
    if location.isdigit():
        rows = rows.filter(camera__location_id=int(location))
    if query:
        rows = rows.filter(Q(camera__name__icontains=query) | Q(camera__location__name__icontains=query))
    date_error = ""
    for param, lookup in (("from", "detected_at__date__gte"), ("to", "detected_at__date__lte")):
        raw = request.GET.get(param, "")
        try:
            date = parse_date(raw) if raw else None
        except ValueError:
            date = None
        if raw and date is None:
            date_error = "Geçerli bir tarih seçiniz."
            rows = rows.none()
        elif date:
            rows = rows.filter(**{lookup: date})
    page = Paginator(rows, 18).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    tabs = []
    for value, label in (("", "Tümü"), ("FIGHT", "Kavga"), ("SPEED", "Hız İhlali")):
        tab = params.copy()
        tab["type"] = value
        tabs.append({"label": label, "query": tab.urlencode(), "active": value == kind})
    locations = {cam.location_id: cam.get_location_display() for cam in cameras if cam.location_id}
    return render(request, "operations/history.html", {
        "events": [incident_card(item) for item in page], "page_obj": page,
        "camera_options": cameras, "locations": locations, "tabs": tabs,
        "filters": request.GET, "date_error": date_error, "query_string": params.urlencode(),
    })


@never_cache
@login_required
@role_required(["admin", "operator", "viewer"])
@require_GET
def incident_detail(request, pk):
    incident = get_object_or_404(visible_incidents(request.user), pk=pk)
    return render(request, "operations/incident_detail.html", {
        "event": incident_card(incident),
        "technical": {"event_id": incident.event_id, "run_id": incident.run_id} if is_it_admin(request.user) else None,
    })


@never_cache
@login_required
@role_required(["admin", "operator", "viewer"])
@require_GET
def live_preview(request, camera_id):
    get_object_or_404(get_user_accessible_cameras(request.user, active_only=True), camera_id=camera_id, source_kind="LIVE")
    from services.pipeline_bridge.live_preview import stream, gateway_context
    if gateway_context() is None:
        raise Http404("Canlı görüntü henüz hazır değil.")
    def authorized():
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.filter(pk=request.user.pk).first()
        if user is None:
            return False
        return user.is_active and get_user_accessible_cameras(user, active_only=True).filter(camera_id=camera_id, source_kind="LIVE").exists()
    response = StreamingHttpResponse(stream(camera_id, authorized), content_type="multipart/x-mixed-replace; boundary=frame")
    response["Cache-Control"] = "no-store"
    response["X-Accel-Buffering"] = "no"
    return response


@never_cache
@login_required
@role_required(["admin", "operator", "viewer"])
@require_GET
def live_previews(request):
    """One bounded stream per page, not twelve browser HTTP connections."""
    from services.pipeline_bridge.live_preview import stream, gateway_context
    from django.contrib.auth import get_user_model
    ids = request.GET.getlist("camera")
    if not ids or len(ids) > 12 or len(ids) != len(set(ids)):
        return JsonResponse({"ok": False}, status=400)
    def authorized():
        user = get_user_model().objects.filter(pk=request.user.pk).first()
        return bool(user and user.is_active and get_user_accessible_cameras(user, active_only=True)
                    .filter(camera_id__in=ids, source_kind="LIVE").count() == len(ids))
    if not authorized() or gateway_context() is None:
        raise Http404
    response = StreamingHttpResponse(stream(",".join(ids), authorized, multiplex=True),
                                     content_type="application/x-camera-frames")
    response["Cache-Control"] = "no-store"
    response["X-Accel-Buffering"] = "no"
    return response


@never_cache
@login_required
@role_required(["admin"])
@require_POST
def analytics_control(request, action):
    from services.pipeline_bridge.analytics_control import set_analytics, AnalyticsControlError
    if action not in {"start", "stop"}:
        raise Http404
    try:
        result = set_analytics(action == "stop")
    except AnalyticsControlError as exc:
        return JsonResponse(exc.payload, status=exc.status)
    return JsonResponse({"ok": True, "pending": True, "analytics_paused": action == "stop",
                         "desired_camera_revision": result.get("revision")})


def worker_cards(health, system=None):
    labels = {"HEALTHY": "Sağlıklı", "STARTING": "Başlatılıyor", "STOPPED": "Durduruldu",
              "DEGRADED": "Uyarı", "FAILED": "Hata"}
    names = {"person": "Kişi algılama", "person_router": "Kişi dağıtımı", "pose": "Poz analizi",
             "pose_router": "Poz dağıtımı", "stage3": "Kavga doğrulama", "vehicle": "Araç algılama",
             "incident": "Olay kaydı"}
    rows = []
    for name, label in names.items():
        row = health.get("workers", {}).get(name, {})
        rows.append({"name": name, "label": label,
                     "health": "Durduruldu" if (system or {}).get("state") == "STOPPED" else
                     "Gerekli değil" if row.get("required") is False else
                     "Bilinmiyor" if health.get("stale") else labels.get(row.get("health"), "Bilinmiyor"),
                     "reason": reason_label(row.get("reason")),
                     **{key: row.get(key) for key in ("restart_count", "service_epoch", "pid", "heartbeat_age_sec")}})
    return rows


@never_cache
@login_required
@role_required(["admin"])
@require_GET
def system_status(request):
    system, health = system_snapshot()
    # Never pass logs, config, source URLs or arbitrary error strings to templates.
    workers = worker_cards(health, system)
    cards = camera_cards(request.user, system, health)
    return render(request, "operations/system.html", {
        "system": system, "workers": workers, "stale": health.get("stale", True),
        "cards": cards,
        "speed_only": bool(cards) and not any(card["fight"] for card in cards) and any(card["speed"] for card in cards),
    })
