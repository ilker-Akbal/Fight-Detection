from django.apps import apps
from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver


class UserProfile(models.Model):
    ROLE_CHOICES = (
        ("admin", "Admin"),
        ("operator", "Operator"),
        ("viewer", "Viewer"),
    )

    STATUS_CHOICES = (
        ("pending", "Onay Bekliyor"),
        ("approved", "Onaylandı"),
        ("rejected", "Reddedildi"),
    )

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile",
    )

    # Artık sabit choices yok.
    # Burada adminx.FacultyLocation.code değeri tutulacak.
    # Örn: muhendislik-ve-mimarlik-fakultesi
    faculty = models.CharField(
        max_length=180,
        blank=True,
        null=True,
        verbose_name="Fakülte / Mevki",
    )

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default="viewer",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
    )

    password_reset_version = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.user.username} - {self.role}"

    @property
    def is_approved(self):
        return self.status == "approved"

    def get_faculty_display(self):
        """
        Eski template/view kodları profile.get_faculty_display çağırıyorsa
        kırılmasın diye manuel display metodu.
        """
        if not self.faculty:
            return "-"

        try:
            FacultyLocation = apps.get_model("adminx", "FacultyLocation")
            item = FacultyLocation.objects.filter(code=self.faculty).first()

            if item:
                return item.name

        except Exception:
            pass

        return self.faculty

    @property
    def faculty_label(self):
        return self.get_faculty_display()


class LoginActivity(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="login_activities",
    )

    role_at_login = models.CharField(
        max_length=20,
        blank=True,
        null=True,
    )

    ip_address = models.GenericIPAddressField(
        blank=True,
        null=True,
    )

    user_agent = models.TextField(
        blank=True,
        null=True,
    )

    login_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-login_at"]

    def __str__(self):
        return f"{self.user.username} - {self.login_at}"


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    if hasattr(instance, "profile"):
        instance.profile.save()