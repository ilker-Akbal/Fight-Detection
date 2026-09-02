from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
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


class Location(models.Model):
    """Physical location tree used by camera authorization.

    ``location_type`` deliberately remains a free text field.  The constants are
    useful defaults for forms and seed data without preventing a deployment from
    introducing its own physical location types.
    """

    TYPE_CAMPUS = "campus"
    TYPE_FACULTY = "faculty"
    TYPE_BUILDING = "building"
    TYPE_FLOOR = "floor"
    TYPE_CORRIDOR = "corridor"
    TYPE_ENTRANCE = "entrance"
    TYPE_PARKING = "parking"
    TYPE_OTHER = "other"

    name = models.CharField(max_length=180, verbose_name="Lokasyon adı")
    code = models.SlugField(max_length=180, unique=True, verbose_name="Kod")
    location_type = models.CharField(
        max_length=50,
        default=TYPE_OTHER,
        verbose_name="Lokasyon tipi",
        help_text="Örn: campus, faculty, building, floor, corridor, entrance, parking",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="children",
        blank=True,
        null=True,
        verbose_name="Üst lokasyon",
    )
    active = models.BooleanField(default=True, verbose_name="Aktif mi?")
    description = models.TextField(blank=True, verbose_name="Açıklama")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Fiziksel lokasyon"
        verbose_name_plural = "Fiziksel lokasyonlar"
        indexes = [
            models.Index(fields=["parent", "active"], name="location_parent_active_idx"),
            models.Index(fields=["location_type", "active"], name="location_type_active_idx"),
        ]

    def clean(self):
        super().clean()
        if self.parent_id is None:
            return
        if self.pk is not None and self.parent_id == self.pk:
            raise ValidationError({"parent": "Bir lokasyon kendisinin üst lokasyonu olamaz."})

        visited = {self.pk} if self.pk is not None else set()
        current_id = self.parent_id
        while current_id is not None:
            if current_id in visited:
                raise ValidationError({"parent": "Lokasyon ağacında döngü oluşturulamaz."})
            visited.add(current_id)
            current_id = (
                type(self).objects
                .filter(pk=current_id)
                .values_list("parent_id", flat=True)
                .first()
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def get_ancestors(self, include_self=False):
        ids = [self.pk] if include_self and self.pk is not None else []
        visited = {self.pk} if self.pk is not None else set()
        current_id = self.parent_id

        while current_id is not None and current_id not in visited:
            ids.append(current_id)
            visited.add(current_id)
            current_id = (
                type(self).objects
                .filter(pk=current_id)
                .values_list("parent_id", flat=True)
                .first()
            )

        return type(self).objects.filter(pk__in=ids)

    def get_descendants(self, include_self=False, active_only=False):
        if self.pk is None:
            return type(self).objects.none()
        if active_only and not self.active:
            return type(self).objects.none()

        rows = list(type(self).objects.values_list("pk", "parent_id", "active"))
        children_by_parent = {}
        for location_id, parent_id, is_active in rows:
            children_by_parent.setdefault(parent_id, []).append((location_id, is_active))

        descendant_ids = [self.pk] if include_self else []
        stack = [self.pk]
        visited = {self.pk}
        while stack:
            parent_id = stack.pop()
            for child_id, is_active in children_by_parent.get(parent_id, []):
                if child_id in visited:
                    continue
                visited.add(child_id)
                if active_only and not is_active:
                    continue
                descendant_ids.append(child_id)
                stack.append(child_id)

        queryset = type(self).objects.filter(pk__in=descendant_ids)
        if active_only:
            queryset = queryset.filter(active=True)
        return queryset

    def is_descendant_of(self, other):
        if self.pk is None or other is None or other.pk is None or self.pk == other.pk:
            return False
        return self.get_ancestors().filter(pk=other.pk).exists()

    def is_effectively_active(self):
        if not self.active:
            return False
        return not self.get_ancestors().filter(active=False).exists()

    def __str__(self):
        return f"{self.name} ({self.code})"


class SecurityUnit(models.Model):
    TYPE_CENTRAL = "central"
    TYPE_CAMPUS = "campus"
    TYPE_FACULTY = "faculty"
    TYPE_LOCAL = "local"
    TYPE_OTHER = "other"

    name = models.CharField(max_length=180, verbose_name="Birim adı")
    code = models.SlugField(max_length=180, unique=True, verbose_name="Kod")
    unit_type = models.CharField(max_length=50, default=TYPE_OTHER, verbose_name="Birim tipi")
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name="security_units",
        blank=True,
        null=True,
        verbose_name="Ana lokasyon",
    )
    active = models.BooleanField(default=True, verbose_name="Aktif mi?")
    is_central = models.BooleanField(default=False, verbose_name="Merkezi birim mi?")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Güvenlik birimi"
        verbose_name_plural = "Güvenlik birimleri"
        indexes = [models.Index(fields=["active", "is_central"], name="security_unit_active_idx")]

    def __str__(self):
        return f"{self.name} ({self.code})"


class SecurityUnitCoverage(models.Model):
    security_unit = models.ForeignKey(
        SecurityUnit,
        on_delete=models.PROTECT,
        related_name="coverages",
        verbose_name="Güvenlik birimi",
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name="security_coverages",
        verbose_name="Lokasyon",
    )
    include_descendants = models.BooleanField(default=True, verbose_name="Alt lokasyonları dahil et")
    active = models.BooleanField(default=True, verbose_name="Aktif mi?")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["security_unit__name", "location__name"]
        verbose_name = "Güvenlik birimi kapsamı"
        verbose_name_plural = "Güvenlik birimi kapsamları"
        constraints = [
            models.UniqueConstraint(
                fields=["security_unit", "location"],
                condition=Q(active=True),
                name="unique_active_unit_location_coverage",
            ),
        ]
        indexes = [models.Index(fields=["security_unit", "active"], name="coverage_unit_active_idx")]

    def __str__(self):
        suffix = " + altları" if self.include_descendants else ""
        return f"{self.security_unit} → {self.location}{suffix}"


class UserSecurityAssignment(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="security_assignments",
        verbose_name="Kullanıcı",
    )
    security_unit = models.ForeignKey(
        SecurityUnit,
        on_delete=models.PROTECT,
        related_name="user_assignments",
        verbose_name="Güvenlik birimi",
    )
    active = models.BooleanField(default=True, verbose_name="Aktif mi?")
    role_in_unit = models.CharField(max_length=80, blank=True, verbose_name="Birim içi rol")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user__username", "security_unit__name"]
        verbose_name = "Kullanıcı güvenlik ataması"
        verbose_name_plural = "Kullanıcı güvenlik atamaları"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "security_unit"],
                condition=Q(active=True),
                name="unique_active_user_security_unit",
            ),
        ]
        indexes = [models.Index(fields=["user", "active"], name="assignment_user_active_idx")]

    def __str__(self):
        return f"{self.user} → {self.security_unit}"
