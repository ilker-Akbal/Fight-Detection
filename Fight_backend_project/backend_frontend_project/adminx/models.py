from django.db import models
from django.utils.text import slugify


class FacultyLocation(models.Model):
    name = models.CharField(
        max_length=150,
        unique=True,
        verbose_name="Fakülte / Mevki Adı",
    )

    code = models.SlugField(
        max_length=180,
        unique=True,
        blank=True,
        verbose_name="Kod",
    )

    description = models.TextField(
        blank=True,
        verbose_name="Açıklama",
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Aktif mi?",
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
        verbose_name = "Fakülte / Mevki"
        verbose_name_plural = "Fakülte / Mevkiler"
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.code:
            base_code = slugify(self.name) or "fakulte-mevki"
            code = base_code
            counter = 2

            while FacultyLocation.objects.filter(code=code).exclude(pk=self.pk).exists():
                code = f"{base_code}-{counter}"
                counter += 1

            self.code = code

        super().save(*args, **kwargs)

    def __str__(self):
        return self.name