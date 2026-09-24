"""Operator HTML contracts; no runtime, GPU, email or production database writes."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from adminx.models import Location, SecurityUnit, SecurityUnitCoverage, UserSecurityAssignment
from incidents.models import Incident, IncidentIngestRecord
from speed_detection.models import SpeedCameraConfig
from streams.models import Camera


class OperatorUITests(TestCase):
    def setUp(self):
        self.control = patch("guvenlik.presentation.get_pipeline_status", return_value={"runtime_state": "STOPPED"}).start()
        self.health = patch("guvenlik.presentation.get_runtime_health", return_value={"stale": True, "available": False}).start()
        self.addCleanup(patch.stopall)
        self.admin = User.objects.create_superuser("ui-admin", "admin@example.test", "test-pass")
        self.user = User.objects.create_user("ui-operator")
        self.user.profile.role = "operator"
        self.user.profile.status = "approved"
        self.user.profile.save()
        self.location = Location.objects.create(name="Ana Giriş", code="giris")
        unit = SecurityUnit.objects.create(name="Giriş Güvenlik", code="giris", location=self.location)
        SecurityUnitCoverage.objects.create(security_unit=unit, location=self.location)
        UserSecurityAssignment.objects.create(user=self.user, security_unit=unit)
        self.camera = Camera.objects.create(name="Giriş Kamerası", camera_id="ui-camera", source="rtsp://private:secret@host/video", location=self.location,
                                            use_fight_detection=True, use_speed_detection=True)
        SpeedCameraConfig.objects.create(camera=self.camera, enabled=True)
        self.client.force_login(self.admin)

    def incident(self, kind, camera=None):
        return Incident.objects.create(camera=camera or self.camera, run_id="ui-run", external_incident_id=f"internal-{kind}",
                                       incident_type=kind, detected_at=timezone.now(), finalized_at=timezone.now(), decision_score=.89)

    def test_consolidated_navigation_and_no_duplicate_modules(self):
        response = self.client.get(reverse("adminx:dashboard"))
        for label in ("Genel Bakış", "Kameralar", "Lokasyonlar", "Kullanıcılar", "Canlı İzleme", "Olaylar", "Sistem Durumu"):
            self.assertContains(response, label)
        for label in ("Kavga Kayıtları", "Hız Kayıtları", "Güvenlik Ekranı", "Hız Ekranı", "admin-live-status"):
            self.assertNotContains(response, label)
        self.assertContains(response, "Sistem Teknik Olarak Durduruldu")
        self.assertContains(response, 'badge neutral')

    def test_compatibility_routes(self):
        self.assertRedirects(self.client.get(reverse("adminx:incident_list")), reverse("dashboard:history"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("adminx:speed_record_list")), reverse("dashboard:history") + "?type=SPEED", fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("speed_detection:index")), reverse("dashboard:index") + "?capability=speed", fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("dashboard:index")), "Canlı İzleme")

    def test_authentication_and_approval_and_admin_boundaries(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("dashboard:index")).status_code, 302)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("dashboard:index")), "Giriş Kamerası")
        self.assertEqual(self.client.get(reverse("adminx:system_status")).status_code, 403)
        self.assertEqual(self.client.get(reverse("adminx:camera_list")).status_code, 403)
        self.user.profile.status = "pending"
        self.user.profile.save()
        self.assertEqual(self.client.get(reverse("dashboard:index")).status_code, 403)

    def test_unified_events_filters_and_speed_values(self):
        fight = self.incident("FIGHT")
        speed = self.incident("SPEED")
        IncidentIngestRecord.objects.create(incident=speed, event_id=str(speed.event_id), source_identifier="fixture", byte_offset=0,
            status="IMPORTED", raw_envelope={"speed": {"speed_kmh": 74, "speed_limit_kmh": 50}})
        response = self.client.get(reverse("dashboard:history"))
        self.assertContains(response, fight.camera.name)
        self.assertContains(response, "Kavga Algılandı")
        self.assertContains(response, "Hız İhlali")
        self.assertEqual(len(response.context["events"]), 2)
        response = self.client.get(reverse("dashboard:history"), {"type": "SPEED"})
        self.assertEqual([event["pk"] for event in response.context["events"]], [speed.pk])
        detail = self.client.get(reverse("dashboard:incident_detail", args=[speed.pk]))
        self.assertEqual(detail.context["event"]["speed"], 74)
        self.assertEqual(detail.context["event"]["limit"], 50)
        self.assertNotContains(detail, "secret")

    def test_operator_event_scope_and_no_ack_or_routing_controls(self):
        event = self.incident("FIGHT")
        outside = Camera.objects.create(name="Private", camera_id="private", source="0")
        hidden = self.incident("SPEED", outside)
        self.client.force_login(self.user)
        for url in (reverse("dashboard:index"), reverse("dashboard:history"), reverse("dashboard:incident_detail", args=[event.pk])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            for secret in ("Kabul Et", "ACKNOWLEDGED", "Çözüm", "routing_stage", "internal-FIGHT", "Teknik Detaylar", "secret", "Private"):
                self.assertNotContains(response, secret)
        self.assertEqual(self.client.get(reverse("dashboard:incident_detail", args=[hidden.pk])).status_code, 404)

    def test_camera_source_and_capability_are_friendly(self):
        response = self.client.get(reverse("adminx:camera_list"))
        self.assertContains(response, "RTSP Kamera")
        self.assertContains(response, "Kavga: Kapalı · Hız: Kapalı")
        self.assertNotContains(response, "private:secret")
        self.assertNotContains(response, "rtsp://")

    def test_camera_wizard_source_state_and_speed_fields(self):
        response = self.client.get(reverse("adminx:camera_create"))
        self.assertContains(response, 'class="wizard-step"', count=4)
        self.assertContains(response, 'id="manualSourcePanel" class="camera-source-panel is-visible"')
        self.assertNotContains(response, 'id="uploadSourcePanel"')
        self.assertContains(response, 'name="speed_limit_kmh"')
        invalid = self.client.post(reverse("adminx:camera_create"), {"source_mode": "upload", "name": "Test", "camera_id": "new"})
        self.assertNotContains(invalid, 'id="uploadSourcePanel"')
        self.assertContains(invalid, "Video Analizleri")
        self.assertFalse(Camera.objects.filter(camera_id="new").exists())
        edited = self.client.get(reverse("adminx:camera_edit", args=[self.camera.pk]))
        self.assertContains(edited, 'id="speed-cal-save-url"')
        self.assertNotContains(edited, "preview-frame/")

    def test_onboarding_and_location_route(self):
        self.camera.delete()
        response = self.client.get(reverse("adminx:dashboard"))
        self.assertContains(response, "Sistemi kullanmaya başlayın")
        self.assertContains(response, "İlk Kamerayı Ekle")
        self.assertContains(response, "Lokasyon hazır")
        self.assertContains(self.client.get(reverse("adminx:faculty_location_list")), "Lokasyon Yönetimi")
        self.assertContains(self.client.get(reverse("adminx:faculty_location_list")), "Ana Giriş")

    def test_missing_preview_friendly_no_capture(self):
        with patch("services.pipeline_bridge.live_preview.gateway_context", return_value=None), patch("cv2.VideoCapture", side_effect=AssertionError("no source open")):
            self.assertEqual(self.client.get(reverse("dashboard:live_preview", args=[self.camera.camera_id])).status_code, 404)
            page = self.client.get(reverse("dashboard:index"))
            self.assertContains(page, "Görüntü bekleniyor")
            self.assertContains(page, 'data-preview=')
            self.assertNotContains(page, 'src="rtsp:')

    def test_csrf_and_destructive_get_do_not_mutate(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        url = reverse("adminx:camera_delete", args=[self.camera.pk])
        self.assertContains(client.get(url), "Vazgeç")
        self.assertEqual(client.post(url).status_code, 403)
        self.assertTrue(Camera.objects.filter(pk=self.camera.pk).exists())
        self.assertEqual(client.get(reverse("dashboard:stop_detection")).status_code, 405)

    def test_status_is_redacted_and_unavailable_values_not_invented(self):
        self.control.return_value = {"runtime_state": "RUNNING", "config_path": "secret"}
        self.health.return_value = {"runtime_health": "HEALTHY", "stale": True, "workers": {"vehicle": {"health": "HEALTHY", "reason": "rtsp://secret"}}}
        response = self.client.get(reverse("dashboard:overview"), {"live": "1"})
        self.assertEqual(response.json()["system"]["tone"], "warning")
        self.assertNotContains(response, "secret")
        event = self.incident("SPEED")
        detail = self.client.get(reverse("dashboard:incident_detail", args=[event.pk]))
        self.assertIsNone(detail.context["event"]["speed"])
        self.assertContains(self.client.get(reverse("adminx:system_status")), "Güncel sağlık bilgisi alınamıyor")

    def test_location_form_uses_existing_hierarchy_and_rejects_cycles(self):
        response = self.client.post(reverse("adminx:location_create"), {"name": "Kat 1", "code": "kat-1", "parent": self.location.pk, "location_type": "floor", "active": "on"})
        self.assertEqual(response.status_code, 302)
        floor = Location.objects.get(code="kat-1")
        response = self.client.post(reverse("adminx:location_edit", args=[self.location.pk]), {"name": "Ana Giriş", "code": "giris", "parent": floor.pk, "location_type": "building", "active": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)

    def test_speed_only_start_uses_common_analytics_control(self):
        self.camera.use_fight_detection = False
        self.camera.save()
        response = self.client.get(reverse("adminx:system_status"))
        self.assertTrue(response.context["speed_only"])
        self.assertNotContains(response, reverse("dashboard:start_detection"))
        self.assertContains(response, reverse("dashboard:analytics_control", args=["start"]))

    def test_new_ui_post_preserves_existing_camera_fields(self):
        response = self.client.post(reverse("adminx:camera_create"), {
            "name": "Yeni Kamera", "camera_id": "new-camera", "source_mode": "manual",
            "source": "rtsp://example.test/video", "location": self.location.pk,
            "is_active": "on", "use_fight_detection": "on", "speed_limit_kmh": "50",
            "tolerance_kmh": "10", "roi_polygon_text": "[]", "save_snapshot": "on", "save_clip": "on",
        })
        self.assertEqual(response.status_code, 302)
        camera = Camera.objects.get(camera_id="new-camera")
        self.assertTrue(camera.use_fight_detection)
        self.assertFalse(camera.use_speed_detection)
        self.assertEqual(camera.location_id, self.location.pk)
        self.assertFalse(camera.speed_config.enabled)

    def test_history_pagination_and_invalid_date_are_safe(self):
        for index in range(20):
            Incident.objects.create(camera=self.camera, run_id="many", external_incident_id=str(index),
                                    detected_at=timezone.now(), finalized_at=timezone.now())
        response = self.client.get(reverse("dashboard:history"), {"type": "FIGHT", "q": "Giriş"})
        self.assertEqual(len(response.context["events"]), 18)
        self.assertTrue(response.context["page_obj"].has_next())
        response = self.client.get(reverse("dashboard:history"), {"from": "2026-02-31"})
        self.assertContains(response, "Geçerli bir tarih seçiniz.")
        self.assertFalse(response.context["events"])
