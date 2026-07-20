from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User

from adminx.models import FacultyLocation

from .models import UserProfile


def get_faculty_location_choices(include_empty=False, current_value=None):
    choices = []

    if include_empty:
        choices.append(("", "Fakülte / mevki seçiniz"))

    items = (
        FacultyLocation.objects
        .filter(is_active=True)
        .order_by("name")
    )

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


class UserRegisterForm(forms.Form):
    faculty = forms.ChoiceField(
        label="Fakülte / Mevki",
        choices=[],
        required=True,
        widget=forms.Select(),
    )

    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "placeholder": "ornek@togu.edu.tr",
            }
        )
    )

    password1 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "placeholder": "Şifrenizi girin",
            }
        )
    )

    password2 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "placeholder": "Şifrenizi tekrar girin",
            }
        )
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["faculty"].choices = get_faculty_location_choices(
            include_empty=True,
        )

    def clean_faculty(self):
        faculty = self.cleaned_data.get("faculty")

        if not faculty:
            raise forms.ValidationError("Lütfen fakülte / mevki seçiniz.")

        exists = FacultyLocation.objects.filter(
            code=faculty,
            is_active=True,
        ).exists()

        if not exists:
            raise forms.ValidationError(
                "Seçilen fakülte / mevki geçerli değil veya pasif durumda."
            )

        return faculty

    def clean_email(self):
        email = self.cleaned_data.get("email")

        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("Bu mail adresi zaten kayıtlı.")

        return email

    def clean(self):
        cleaned_data = super().clean()

        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Şifreler eşleşmiyor.")

        return cleaned_data

    def save(self):
        email = self.cleaned_data["email"]
        password = self.cleaned_data["password1"]
        faculty = self.cleaned_data["faculty"]

        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
        )

        user.profile.faculty = faculty
        user.profile.save(update_fields=["faculty"])

        return user


class AccountUpdateForm(forms.ModelForm):
    faculty = forms.ChoiceField(
        label="Fakülte / Mevki",
        choices=[],
        required=False,
        widget=forms.Select(
            attrs={
                "class": "account-input",
            }
        ),
    )

    class Meta:
        model = User
        fields = ["username", "email"]

        labels = {
            "username": "Kullanıcı Adı",
            "email": "E-posta",
        }

        widgets = {
            "username": forms.TextInput(
                attrs={
                    "class": "account-input",
                    "placeholder": "Kullanıcı adınızı girin",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "account-input",
                    "placeholder": "E-posta adresinizi girin",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        self.profile = kwargs.pop("profile", None)

        super().__init__(*args, **kwargs)

        current_value = None

        if self.profile:
            current_value = self.profile.faculty

        self.fields["faculty"].choices = get_faculty_location_choices(
            include_empty=True,
            current_value=current_value,
        )

        if self.profile:
            self.fields["faculty"].initial = self.profile.faculty

    def clean_faculty(self):
        faculty = self.cleaned_data.get("faculty")

        if not faculty:
            return ""

        exists = FacultyLocation.objects.filter(
            code=faculty,
            is_active=True,
        ).exists()

        if not exists:
            raise forms.ValidationError(
                "Seçilen fakülte / mevki geçerli değil veya pasif durumda."
            )

        return faculty

    def clean_username(self):
        username = self.cleaned_data.get("username")
        user_id = self.instance.id

        if User.objects.exclude(id=user_id).filter(username=username).exists():
            raise forms.ValidationError("Bu kullanıcı adı zaten kullanılıyor.")

        return username

    def clean_email(self):
        email = self.cleaned_data.get("email")
        user_id = self.instance.id

        if email and User.objects.exclude(id=user_id).filter(email=email).exists():
            raise forms.ValidationError("Bu e-posta adresi zaten kullanılıyor.")

        return email

    def save(self, commit=True):
        user = super().save(commit=commit)

        if self.profile:
            self.profile.faculty = self.cleaned_data.get("faculty") or None

            if commit:
                self.profile.save(update_fields=["faculty"])

        return user


class CustomPasswordChangeForm(PasswordChangeForm):
    old_password = forms.CharField(
        label="Mevcut Şifre",
        widget=forms.PasswordInput(
            attrs={
                "class": "account-input",
                "placeholder": "Mevcut şifrenizi girin",
            }
        ),
    )

    new_password1 = forms.CharField(
        label="Yeni Şifre",
        widget=forms.PasswordInput(
            attrs={
                "class": "account-input",
                "placeholder": "Yeni şifrenizi girin",
            }
        ),
    )

    new_password2 = forms.CharField(
        label="Yeni Şifre Tekrar",
        widget=forms.PasswordInput(
            attrs={
                "class": "account-input",
                "placeholder": "Yeni şifrenizi tekrar girin",
            }
        ),
    )