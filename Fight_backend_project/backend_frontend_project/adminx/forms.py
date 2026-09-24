from pathlib import Path

from django import forms
from django.contrib.auth.models import User
from django.db.models import Q
from django.db import transaction

from accounts.models import UserProfile
from streams.models import Camera
from speed_detection.models import SpeedCameraConfig

from .models import FacultyLocation, Location, SecurityUnit, UserSecurityAssignment


ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
MAX_UPLOAD_SIZE_MB = 1024


def faculty_location_choices(include_empty=True, current_value=None):
    choices = []

    if include_empty:
        choices.append(("", "Fakülte / mevki seçiniz"))

    items = FacultyLocation.objects.filter(is_active=True).order_by("name")

    for item in items:
        choices.append((item.code, item.name))

    if current_value:
        exists_in_choices = any(value == current_value for value, label in choices)

        if not exists_in_choices:
            old_item = FacultyLocation.objects.filter(code=current_value).first()

            if old_item:
                choices.append((old_item.code, f"{old_item.name} (Pasif)"))
            else:
                choices.append((current_value, f"{current_value} (Eski kayıt)"))

    return choices


class CameraForm(forms.ModelForm):
    SOURCE_MODE_MANUAL = "manual"
    SOURCE_MODE_UPLOAD = "upload"

    SOURCE_MODE_CHOICES = [
        (SOURCE_MODE_MANUAL, "Manuel kaynak"),
    ]

    source_mode = forms.ChoiceField(
        label="Kaynak Tipi",
        choices=SOURCE_MODE_CHOICES,
        required=True,
        widget=forms.RadioSelect(),
    )

    faculty = forms.ChoiceField(
        label="Fakülte / Mevki",
        required=False,
        choices=[],
        widget=forms.Select(
            attrs={
                "class": "camera-select",
            }
        ),
    )

    location = forms.ModelChoiceField(
        label="Fiziksel Lokasyon",
        required=False,
        queryset=Location.objects.none(),
        empty_label="Fiziksel lokasyon seçiniz",
        help_text="Yeni yetkilendirme kapsamı bu lokasyon ağacı üzerinden hesaplanır.",
    )

    uploaded_video = forms.FileField(
        label="Video Dosyası",
        required=False,
        widget=forms.FileInput(
            attrs={
                "accept": ".mp4,.avi,.mov,.mkv,.webm,video/*",
                "class": "camera-file-input",
            }
        ),
    )

    class Meta:
        model = Camera
        fields = [
            "name",
            "camera_id",
            "source",
            "uploaded_video",
            "description",
            "location",
            "faculty",
            "is_active",
            "use_fight_detection",
            "use_speed_detection",
        ]

        labels = {
            "name": "Kamera Adı",
            "camera_id": "Camera ID",
            "source": "Kaynak",
            "uploaded_video": "Video Dosyası",
            "description": "Açıklama",
            "location": "Fiziksel Lokasyon",
            "faculty": "Fakülte / Mevki",
            "is_active": "Aktif mi?",
            "use_fight_detection": "Kavga Tespiti",
            "use_speed_detection": "Hız Tespiti",
        }

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "placeholder": "Örnek: Giriş Kapısı Kamera 1",
                }
            ),
            "camera_id": forms.TextInput(
                attrs={
                    "placeholder": "Örnek: cam_01, giris_kapi, fight_test",
                }
            ),
            "source": forms.TextInput(
                attrs={
                    "placeholder": "Örnek: 0, rtsp://..., http://..., C:/.../fight.mp4",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "Kamera açıklaması",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        current_faculty = None

        if self.instance and self.instance.pk:
            current_faculty = getattr(self.instance, "faculty", None)

        choices = faculty_location_choices(
            include_empty=True,
            current_value=current_faculty,
        )

        self.fields["faculty"].choices = choices
        self.fields["faculty"].widget.choices = choices

        location_filter = Q(active=True)
        if self.instance and self.instance.pk and self.instance.location_id:
            location_filter |= Q(pk=self.instance.location_id)
        self.fields["location"].queryset = (
            Location.objects
            .filter(location_filter)
            .select_related("parent")
            .order_by("name")
        )

        if self.instance and self.instance.pk and self.instance.uploaded_video:
            self.fields["source_mode"].initial = self.SOURCE_MODE_UPLOAD
        else:
            self.fields["source_mode"].initial = self.SOURCE_MODE_MANUAL

        self.fields["source"].required = False
        self.fields["source"].widget.attrs["placeholder"] = "rtsp://kamera/adres veya https://kamera/akis"
        self.fields["camera_id"].label = "Kamera Kimliği"

    def clean_faculty(self):
        faculty = self.cleaned_data.get("faculty")
        if self.cleaned_data.get("location"):
            return self.cleaned_data["location"].code

        if not faculty:
            return None

        item = FacultyLocation.objects.filter(code=faculty).first()

        if not item:
            raise forms.ValidationError("Seçilen fakülte / mevki bulunamadı.")

        if not item.is_active:
            current_value = None

            if self.instance and self.instance.pk:
                current_value = getattr(self.instance, "faculty", None)

            if faculty != current_value:
                raise forms.ValidationError("Seçilen fakülte / mevki pasif durumda.")

        return faculty

    def clean_uploaded_video(self):
        uploaded_video = self.cleaned_data.get("uploaded_video")

        if not uploaded_video:
            return uploaded_video

        ext = Path(uploaded_video.name).suffix.lower()

        if ext not in ALLOWED_VIDEO_EXTENSIONS:
            allowed = ", ".join(sorted(ALLOWED_VIDEO_EXTENSIONS))
            raise forms.ValidationError(
                f"Desteklenmeyen video formatı. İzin verilen formatlar: {allowed}"
            )

        max_size = MAX_UPLOAD_SIZE_MB * 1024 * 1024

        if uploaded_video.size > max_size:
            raise forms.ValidationError(
                f"Video dosyası çok büyük. Maksimum {MAX_UPLOAD_SIZE_MB} MB yükleyebilirsin."
            )

        return uploaded_video

    def clean(self):
        cleaned_data = super().clean()

        from streams.source_kind import is_live_source
        if not is_live_source(cleaned_data.get("source"), cleaned_data.get("uploaded_video")):
            self.add_error("source", "Yalnızca canlı RTSP/HTTP veya cihaz kaynağı kullanın. Dosyalar için Video Analizleri bölümünü açın.")
        if str(cleaned_data.get("camera_id", "")).startswith("offline_"):
            self.add_error("camera_id", "Bu kimlik öneki geçmiş video çalışmaları için ayrılmıştır.")

        source_mode = cleaned_data.get("source_mode")
        source = (cleaned_data.get("source") or "").strip()
        uploaded_video = cleaned_data.get("uploaded_video")

        has_existing_upload = (
            self.instance
            and self.instance.pk
            and bool(self.instance.uploaded_video)
        )

        if source_mode == self.SOURCE_MODE_MANUAL:
            if not source:
                self.add_error(
                    "source",
                    "Manuel kaynak seçiliyken kaynak alanı zorunludur.",
                )

        elif source_mode == self.SOURCE_MODE_UPLOAD:
            if not uploaded_video and not has_existing_upload:
                self.add_error(
                    "uploaded_video",
                    "Video yükleme seçiliyken video dosyası zorunludur.",
                )

        else:
            self.add_error(
                "source_mode",
                "Lütfen kaynak tipini seçiniz.",
            )

        return cleaned_data

    def save(self, commit=True):
        source_mode = self.cleaned_data.get("source_mode")
        instance = super().save(commit=False)

        if instance.location_id:
            # Temporary write bridge for reports/templates that still read the
            # legacy string field during the migration period.
            instance.faculty = instance.location.code

        if source_mode == self.SOURCE_MODE_MANUAL:
            instance.source = (self.cleaned_data.get("source") or "").strip()

            # Manuel kaynağa geçildiyse eski upload referansını temizliyoruz.
            instance.uploaded_video = None

            if commit:
                instance.save()
                self.save_m2m()

            return instance

        if source_mode == self.SOURCE_MODE_UPLOAD:
            if commit:
                instance.save()

                if instance.uploaded_video:
                    instance.source = instance.uploaded_video.path
                    instance.save(update_fields=["source"])

                self.save_m2m()

            return instance

        if commit:
            instance.save()
            self.save_m2m()

        return instance


class SpeedCameraConfigForm(forms.ModelForm):
    roi_polygon_text = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )

    class Meta:
        model = SpeedCameraConfig
        fields = [
            "enabled",
            "speed_limit_kmh",
            "tolerance_kmh",
            "calibration_path",
            "roi_enabled",
            "save_snapshot",
            "save_clip",
        ]

        labels = {
            "enabled": "Bu kamerada hız tespiti aktif",
            "speed_limit_kmh": "Hız Limiti (km/h)",
            "tolerance_kmh": "Tolerans (km/h)",
            "calibration_path": "Kalibrasyon Dosyası",
            "roi_enabled": "ROI aktif",
            "save_snapshot": "Snapshot kaydet",
            "save_clip": "Clip kaydet",
        }

    def __init__(self, *args, **kwargs):
        import json

        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self.fields["roi_polygon_text"].initial = json.dumps(
                self.instance.roi_polygon or [],
                ensure_ascii=False,
            )

    def clean_roi_polygon_text(self):
        import json

        value = self.cleaned_data.get("roi_polygon_text", "").strip()

        if not value:
            return []

        try:
            parsed = json.loads(value)
        except Exception:
            raise forms.ValidationError("ROI verisi okunamadı. Lütfen ROI alanını yeniden seç.")

        if not isinstance(parsed, list):
            raise forms.ValidationError("ROI verisi liste formatında olmalı.")

        clean_points = []

        for point in parsed:
            if (
                not isinstance(point, list)
                or len(point) != 2
                or not all(isinstance(v, (int, float)) for v in point)
            ):
                raise forms.ValidationError("ROI noktaları geçersiz.")

            clean_points.append([int(point[0]), int(point[1])])

        return clean_points

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.roi_polygon = self.cleaned_data.get("roi_polygon_text", [])

        if commit:
            instance.save()

        return instance


class UserEditForm(forms.ModelForm):
    security_units = forms.ModelMultipleChoiceField(
        label="Kamera / Güvenlik Erişimi", queryset=SecurityUnit.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Seçilen birimlerin mevcut lokasyon kapsamı erişim verir. Profil lokasyonu erişim vermez.")
    username = forms.CharField(label="Kullanıcı Adı")
    email = forms.EmailField(label="E-posta", required=False)

    faculty = forms.ChoiceField(
        label="Fakülte / Mevki",
        required=False,
        choices=[],
        widget=forms.Select(),
    )

    class Meta:
        model = UserProfile
        fields = ["faculty", "role", "status"]

        labels = {
            "faculty": "Fakülte / Mevki",
            "role": "Rol",
            "status": "Durum",
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user_instance")

        super().__init__(*args, **kwargs)
        self.user_instance = user
        self.fields["security_units"].queryset = SecurityUnit.objects.filter(active=True).order_by("name")
        self.fields["security_units"].initial = list(user.security_assignments.filter(
            active=True, security_unit__active=True).values_list("security_unit_id", flat=True)) if user.pk else []

        self.fields["role"].choices = [("admin", "Yönetici"), ("operator", "Operatör"), ("viewer", "İzleyici")]
        self.fields["faculty"].label = "Lokasyon Etiketi"

        self.fields["username"].initial = user.username
        self.fields["email"].initial = user.email

        current_value = None

        if self.instance and self.instance.pk:
            current_value = getattr(self.instance, "faculty", None)

        choices = faculty_location_choices(
            include_empty=True,
            current_value=current_value,
        )

        self.fields["faculty"].choices = choices
        self.fields["faculty"].widget.choices = choices

    def clean_faculty(self):
        faculty = self.cleaned_data.get("faculty")

        if not faculty:
            return None

        item = FacultyLocation.objects.filter(code=faculty).first()

        if not item:
            raise forms.ValidationError("Seçilen fakülte / mevki bulunamadı.")

        if not item.is_active:
            current_value = None

            if self.instance and self.instance.pk:
                current_value = getattr(self.instance, "faculty", None)

            if faculty != current_value:
                raise forms.ValidationError("Seçilen fakülte / mevki pasif durumda.")

        return faculty

    def clean_username(self):
        value = self.cleaned_data["username"]
        if User.objects.filter(username=value).exclude(pk=self.user_instance.pk).exists():
            raise forms.ValidationError("Bu kullanıcı adı zaten kullanılıyor.")
        return value

    @transaction.atomic
    def save(self, user_instance, commit=True):
        user_instance.username = self.cleaned_data["username"]
        user_instance.email = self.cleaned_data["email"]

        if commit:
            if user_instance.pk:
                User.objects.select_for_update().get(pk=user_instance.pk)
            user_instance.save()
            if not self.instance.pk:
                self.instance.pk = UserProfile.objects.get(user=user_instance).pk
                self.instance.user = user_instance
        profile = super().save(commit=commit)
        if commit:
            units = self.cleaned_data["security_units"]
            assignments = UserSecurityAssignment.objects.filter(user=user_instance)
            assignments.exclude(security_unit__in=units).update(active=False)
            for unit in units:
                existing = assignments.filter(security_unit=unit).order_by("-active", "pk").first()
                if existing is None:
                    UserSecurityAssignment.objects.create(user=user_instance, security_unit=unit)
                elif not existing.active:
                    existing.active = True
                    existing.save(update_fields=["active"])
        return profile


class UserCreateForm(UserEditForm):
    password1 = forms.CharField(label="Şifre", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Şifre tekrar", widget=forms.PasswordInput)

    def clean(self):
        from django.contrib.auth.password_validation import validate_password
        data = super().clean()
        if data.get("password1") != data.get("password2"):
            self.add_error("password2", "Şifreler eşleşmiyor.")
        if data.get("password1"):
            candidate = User(username=data.get("username", ""), email=data.get("email", ""))
            try:
                validate_password(data["password1"], candidate)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return data

    def save(self, user_instance, commit=True):
        user_instance.set_password(self.cleaned_data["password1"])
        return super().save(user_instance, commit=commit)


class FacultyLocationForm(forms.ModelForm):
    class Meta:
        model = FacultyLocation
        fields = [
            "name",
            "description",
            "is_active",
        ]

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "placeholder": "Örn: Mühendislik Fakültesi veya Ana Kampüs Giriş",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "placeholder": "İsteğe bağlı açıklama. Örn: Kamera ve kullanıcı eşleştirmelerinde kullanılacak bölge.",
                    "rows": 4,
                }
            ),
        }

        labels = {
            "name": "Fakülte / Mevki Adı",
            "description": "Açıklama",
            "is_active": "Aktif",
        }


class LocationForm(forms.ModelForm):
    """Presentation form for the existing physical hierarchy; no new schema."""
    class Meta:
        model = Location
        fields = ["name", "code", "parent", "location_type", "description", "active"]
        labels = {"name": "Lokasyon Adı", "code": "Kısa Kod", "parent": "Üst Lokasyon",
                  "location_type": "Lokasyon Türü", "description": "Açıklama", "active": "Aktif"}
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["parent"].queryset = Location.objects.exclude(pk=self.instance.pk)
