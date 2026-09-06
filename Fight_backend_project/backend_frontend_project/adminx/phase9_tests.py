from __future__ import annotations

from django.test import TestCase

from services.pipeline_bridge.camera_registry import (
    CameraRegistryReconciler,
    desired_camera_snapshot,
)
from streams.models import Camera


class FakeSupervisorClient:
    def __init__(self, cameras=None, revision=4):
        self.cameras = list(cameras or [])
        self.revision = revision
        self.updates = []

    def desired_cameras(self):
        return {
            "schema_version": 1,
            "revision": self.revision,
            "cameras": self.cameras,
        }

    def update_desired_cameras(self, payload):
        self.updates.append(payload)
        self.cameras = payload["cameras"]
        self.revision = payload["revision"]
        return {**payload, "accepted": True}


class Phase9CameraRegistryTests(TestCase):
    def test_snapshot_contains_only_active_fight_cameras(self):
        Camera.objects.create(
            camera_id="fight", name="Fight", source="rtsp://host/fight"
        )
        Camera.objects.create(
            camera_id="inactive",
            name="Inactive",
            source="rtsp://host/inactive",
            is_active=False,
        )
        Camera.objects.create(
            camera_id="speed",
            name="Speed",
            source="rtsp://host/speed",
            use_fight_detection=False,
            use_speed_detection=True,
        )
        snapshot = desired_camera_snapshot()
        self.assertEqual([camera["camera_id"] for camera in snapshot], ["fight"])

    def test_unchanged_snapshot_does_not_spam_supervisor_updates(self):
        Camera.objects.create(camera_id="A", name="A", source="rtsp://host/A")
        desired = desired_camera_snapshot()
        client = FakeSupervisorClient(desired)
        reconciler = CameraRegistryReconciler(client)
        first = reconciler.tick()
        second = reconciler.tick()
        self.assertFalse(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(client.updates, [])

    def test_changed_snapshot_advances_revision_once(self):
        Camera.objects.create(camera_id="A", name="A", source="rtsp://host/A")
        client = FakeSupervisorClient([], revision=7)
        result = CameraRegistryReconciler(client).tick()
        self.assertTrue(result["changed"])
        self.assertEqual(result["revision"], 8)
        self.assertEqual(len(client.updates), 1)
        self.assertEqual(client.updates[0]["schema_version"], 1)
