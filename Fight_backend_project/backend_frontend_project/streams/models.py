from pathlib import Path
from uuid import uuid4

from django.apps import apps
from django.db import models
from django.utils.text import slugify


def camera_video_upload_to(instance, filename):
    ext = Path(filename).suffix.lower()
    camera_part = slugify(instance.camera_id or instance.name or "camera")
    unique_name = f"{camera_part}_{uuid4().hex[:12]}{ext}"

    return f"camera_uploads/{unique_name}"


class Camera(models.Model):
    name = models.CharField(
        max_length=120,
        verbose_name="Kamera Adı",
    )

    camera_id = models.CharField(
        max_length=100,
        unique=True,
        verbose_name="Camera ID",
    )

    source = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Kaynak",
        help_text="RTSP/HTTP/IP kamera adresi, cihaz index'i veya yüklenen video dosyasının path'i.",
    )

    uploaded_video = models.FileField(
        upload_to=camera_video_upload_to,
        blank=True,
        null=True,
        verbose_name="Yüklenen Video",
    )

    description = models.TextField(
        blank=True,
        verbose_name="Açıklama",
    )

    faculty = models.CharField(
        max_length=180,
        blank=True,
        null=True,
        verbose_name="Fakülte / Mevki",
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Aktif mi?",
    )

    use_fight_detection = models.BooleanField(
        default=True,
        verbose_name="Kavga Tespiti",
        help_text="Bu kamera kavga tespiti modülünde kullanılsın.",
    )

    use_speed_detection = models.BooleanField(
        default=False,
        verbose_name="Hız Tespiti",
        help_text="Bu kamera hız tespiti modülünde kullanılsın.",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Oluşturulma Tarihi",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Güncellenme Tarihi",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Kamera"
        verbose_name_plural = "Kameralar"

    def get_faculty_display(self):
        if not self.faculty:
            return "Fakülte / mevki belirtilmedi"

        try:
            FacultyLocation = apps.get_model("adminx", "FacultyLocation")
            item = FacultyLocation.objects.filter(code=self.faculty).first()

            if item:
                return item.name

        except Exception:
            pass

        return str(self.faculty)

    @property
    def faculty_label(self):
        return self.get_faculty_display()

    def get_runtime_source(self):
        """
        Pipeline'a verilecek gerçek kaynak.
        Upload varsa dosyanın fiziksel path'ini döndürür.
        Upload yoksa manuel source değerini döndürür.
        """
        if self.uploaded_video:
            try:
                return self.uploaded_video.path
            except Exception:
                pass

        return self.source or ""

    def __str__(self):
        return f"{self.name} ({self.camera_id}) - {self.get_faculty_display()}"