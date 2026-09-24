"""Read-only, allowlisted projections for operator screens. No runtime ownership."""
import math
from pathlib import PurePosixPath

from django.db.models import Prefetch
from django.urls import reverse

from incidents.models import Incident, IncidentIngestRecord
from services.access_scope import get_user_accessible_cameras
from services.pipeline_bridge.fight_runner import get_pipeline_status, get_runtime_health


REASON_LABELS = {
    "process_dead": "İşlem beklenmedik biçimde durdu",
    "heartbeat_timeout": "Hizmet yanıt vermiyor",
    "inference_stall": "Analiz yanıtı gecikiyor",
    "frame_stall": "Kamera görüntüsü gecikiyor",
    "service_disabled": "Gerekli değil / devre dışı",
    "service_restarting": "Hizmet yeniden başlatılıyor",
    "service_recovery_exhausted": "Yeniden başlatma denemeleri tükendi",
    "service_failure_detected": "Hizmet hatası algılandı",
    "queue_pressure": "İş yükü yoğun",
    "startup_grace": "Başlatılıyor",
    "source_offline": "Kamera kaynağına erişilemiyor",
    "critical_components_healthy": "Hizmetler sağlıklı",
    "component_degraded": "Bir hizmet kontrol gerektiriyor",
    "disk_pressure": "Disk alanı azalıyor",
    "health_snapshot_stale": "Sağlık bilgisi güncel değil",
    "supervisor_unavailable": "Sistem yöneticisine erişilemiyor",
    "critical_shared_worker_unhealthy": "Ortak analiz hizmetinde hata",
}


def reason_label(value):
    # Unknown diagnostic strings may contain paths/credentials. Do not render them.
    return REASON_LABELS.get(value, "Ayrıntı kullanılamıyor") if isinstance(value, str) else "Ayrıntı kullanılamıyor"


def system_snapshot():
    control = get_pipeline_status()
    health = get_runtime_health()
    state = control.get("runtime_state", "UNKNOWN")
    paused = bool(control.get("analytics_paused", False))
    observations = [row for cid, row in health.get("cameras", {}).items() if not cid.startswith("offline_")]
    analytics_confirmed = (bool(observations) and not health.get("stale", True)
                           and control.get("desired_camera_revision") is not None
                           and control["desired_camera_revision"] == health.get("desired_camera_revision"))
    if paused:
        analytics_confirmed = analytics_confirmed and all(
            not row.get("fight", {}).get("enabled", True)
            and not row.get("speed", {}).get("enabled", True) for row in observations)
        # Shared models may still be required by an explicit historical job.
    else:
        analytics_confirmed = analytics_confirmed and all(
            not branch.get("failed") and not branch.get("waiting")
            for row in observations for branch in (row.get("fight", {}), row.get("speed", {})))
        analytics_confirmed = analytics_confirmed and health.get("runtime_health") == "HEALTHY"
    if state == "STOPPED":
        label, tone = "Sistem Teknik Olarak Durduruldu", "neutral"
    elif state == "FAILED" or health.get("runtime_health") == "FAILED":
        label, tone = "Sistem Hatası", "danger"
    elif (state == "RUNNING" and health.get("runtime_health") == "HEALTHY"
          and not health.get("stale")):
        label, tone = ("Kameralar Aktif · Analizler Durduruldu" if paused and analytics_confirmed else
                       "Kameralar Aktif · Analiz duraklatma durumu doğrulanamadı" if paused else
                       "Kameralar Aktif · Analizler Aktif" if analytics_confirmed else
                       "Kameralar Aktif · Analiz durumu doğrulanamadı"), "success" if analytics_confirmed else "warning"
    elif state == "UNKNOWN" or not health.get("available", True):
        label, tone = "Runtime / Supervisor kullanılamıyor", "danger"
    elif health.get("stale", True):
        label, tone = "Güncel çalışma zamanı durumu alınamıyor", "warning"
    else:
        label, tone = "Çalışma zamanı hazır değil · " + reason_label(health.get("reason")), "warning"
    return {"label": label, "tone": tone, "state": state,
            "mode": "technical_stop" if state == "STOPPED" else "preview_only" if paused else "analytics_active",
            "analytics_paused": paused, "analytics_confirmed": analytics_confirmed,
            "desired_camera_revision": health.get("desired_camera_revision"),
            "last_status_at": health.get("updated_at"),
            "stale": bool(health.get("stale", True)),
            "restart_count": finite_number(control.get("restart_count")),
            "exit_code": finite_number(control.get("runtime_exit_code")),
            "reason": reason_label(health.get("reason"))}, health


def camera_cards(user, system, health):
    from services.pipeline_bridge.live_preview import preview_status
    previews = preview_status() if system["state"] in {"RUNNING", "STARTING"} else {}
    rows = []
    for camera in get_user_accessible_cameras(user).filter(source_kind="LIVE").select_related("location", "speed_config"):
        speed = camera.use_speed_detection and getattr(camera, "speed_config", None)
        speed = bool(speed and speed.enabled)
        fight = camera.use_fight_detection
        analysis = "Kavga + Hız" if fight and speed else "Kavga" if fight else "Hız" if speed else "Kapalı"
        source = (camera.source or "").lower()
        source_type = ("RTSP Kamera" if source.startswith(("rtsp://", "rtsps://")) else
                       "HTTP Kamera" if source.startswith(("http://", "https://")) else
                       "Yerel Kamera" if source.isdigit() else "Video Dosyası")
        observation = health.get("cameras", {}).get(camera.camera_id, {})
        preview = previews.get(camera.camera_id, {})
        age = finite_number(preview.get("age_sec"))
        if age is None and not health.get("stale", True):
            age = finite_number(observation.get("last_preview_publish_age_sec"))
            if age is not None:
                age += finite_number(health.get("snapshot_age_sec")) or 0
        if not camera.is_active:
            label, tone = "Pasif", "neutral"
        elif system["state"] == "STOPPED":
            label, tone = "Durduruldu", "neutral"
        elif age is not None and age <= 3:
            label, tone = "Canlı", "success"
        elif observation.get("health") == "EOF":
            label, tone = "Video tamamlandı", "neutral"
        elif (str(observation.get("source_state", "")).upper() in {"OFFLINE", "ERROR"}
              or observation.get("reconnect_count", 0) > 0
              or observation.get("health") == "FAILED" or age is not None):
            label, tone = "Görüntü alınamıyor", "danger"
        elif health.get("stale") or not health.get("available", True):
            label, tone = "Bağlantı yok · Durum alınamıyor", "warning"
        elif observation.get("reason") in {"startup_grace", "source_reconnecting"}:
            label, tone = "Bağlanıyor", "warning"
        else:
            label, tone = "Görüntü alınamıyor", "danger"
        def branch_label(name, configured):
            branch = observation.get(name, {})
            if system.get("analytics_paused") or not configured:
                return "Kapalı"
            if branch.get("failed"):
                return "Hata"
            shared = health.get("workers", {}).get("person" if name == "fight" else "vehicle", {})
            if shared.get("health") == "FAILED":
                return "Hata"
            if (not health.get("stale", True) and branch.get("enabled")
                    and shared.get("health") == "HEALTHY"
                    and not branch.get("waiting") and system["state"] == "RUNNING"):
                return "Aktif"
            return "Durum alınamıyor" if system["state"] in {"RUNNING", "STARTING"} else "Kapalı"
        analysis = "Kavga: " + branch_label("fight", fight) + " · Hız: " + branch_label("speed", speed)
        rows.append({"pk": camera.pk, "camera_id": camera.camera_id, "name": camera.name,
                     "monitorable": camera.is_active,
                     "location": camera.get_location_display(), "analysis": analysis,
                     "fight": fight, "speed": speed, "source_type": source_type,
                     "label": label, "tone": tone,
                     "preview_url": reverse("dashboard:live_preview", args=[camera.camera_id])})
    return rows


def visible_incidents(user):
    # Same camera authorization as user_can_view_incident/evidence, not route rows:
    # one incident appears once even if routed to several security units.
    return (Incident.objects.filter(camera__in=get_user_accessible_cameras(user), camera__source_kind="LIVE")
            .select_related("camera", "camera__location")
            .prefetch_related(Prefetch("ingest_records", queryset=IncidentIngestRecord.objects
                .filter(status=IncidentIngestRecord.STATUS_IMPORTED).only("incident_id", "raw_envelope")))
            .order_by("-detected_at", "-pk"))


def finite_number(value):
    try:
        number = float(value)
        return round(number, 1) if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def incident_card(incident):
    speed = incident.incident_type == Incident.TYPE_SPEED
    records = list(incident.ingest_records.all())
    values = records[0].raw_envelope.get("speed", {}) if records else {}
    if not isinstance(values, dict):
        values = {}
    suffix = PurePosixPath(incident.evidence_path.replace("\\", "/")).suffix.lower()
    media_kind = "image" if suffix in {".jpg", ".jpeg", ".png", ".webp"} else "video"
    return {"pk": incident.pk, "title": "Hız İhlali" if speed else "Kavga Algılandı" if incident.incident_type == "FIGHT" else "Olay",
            "kind": incident.incident_type, "tone": "warning" if speed else "danger",
            "camera": incident.camera.name, "location": incident.camera.get_location_display(),
            "detected_at": incident.detected_at,
            "confidence": None if speed else finite_number(incident.decision_score * 100),
            "speed": finite_number(values.get("speed_kmh")),
            "limit": finite_number(values.get("speed_limit_kmh")),
            "media_kind": media_kind,
            "evidence_url": reverse("dashboard:incident_evidence", args=[incident.pk]) if incident.evidence_valid else "",
            "url": reverse("dashboard:incident_detail", args=[incident.pk])}
