from django.db import models
from streams.models import Camera


class SpeedCameraConfig(models.Model):
    camera = models.OneToOneField(
        Camera,
        on_delete=models.CASCADE,
        related_name="speed_config",
    )

    enabled = models.BooleanField(default=False)

    speed_limit_kmh = models.FloatField(default=50.0)
    tolerance_kmh = models.FloatField(default=10.0)

    calibration_path = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Örnek: HizTespiti/calibration/out_calibration/cam_001_calibration.json",
    )

    roi_enabled = models.BooleanField(default=False)
    roi_polygon = models.JSONField(default=list, blank=True)

    save_snapshot = models.BooleanField(default=True)
    save_clip = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Hız Tespiti Kamera Ayarı"
        verbose_name_plural = "Hız Tespiti Kamera Ayarları"

    def __str__(self):
        return f"{self.camera.camera_id} hız ayarı"