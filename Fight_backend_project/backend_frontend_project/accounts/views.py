from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import redirect, render
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from services.email_service import EmailServiceError, send_email

from .forms import AccountUpdateForm, CustomPasswordChangeForm, UserRegisterForm
from .models import LoginActivity, UserProfile


def _safe_next_url(next_url):
    next_url = (next_url or "").strip()
    prefix = settings.URL_PREFIX or ""

    if not next_url.startswith("/"):
        return ""

    if next_url.startswith("//"):
        return ""

    valid_paths = ["/admin/", "/dashboard/"]

    for vp in valid_paths:
        if next_url.startswith(vp) or next_url.startswith(f"{prefix}{vp}"):
            return next_url

    return ""


def _get_client_ip(request):
    ip = request.META.get("HTTP_X_FORWARDED_FOR")

    if ip:
        return ip.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR")


def _create_login_activity(request, user, profile):
    LoginActivity.objects.create(
        user=user,
        role_at_login=profile.role,
        ip_address=_get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )


def _logo_url():
    return getattr(
        settings,
        "MAIL_LOGO_URL",
        f"{settings.FRONTEND_URL}/static/images/togu-logo.png",
    )


def _base_mail_template(title, description, button_text=None, button_url=None, extra_note=None):
    button_html = ""

    if button_text and button_url:
        button_html = f"""
        <a
          href="{button_url}"
          style="display:inline-block;background:#0f4c81;color:#ffffff;text-decoration:none;padding:14px 26px;border-radius:14px;font-size:15px;font-weight:700;"
        >
          {button_text}
        </a>

        <p style="margin:24px 0 0;color:#64748b;font-size:13px;line-height:1.6;">
          Buton çalışmazsa aşağıdaki bağlantıyı tarayıcınıza kopyalayabilirsiniz:
        </p>

        <p style="margin:10px 0 0;word-break:break-all;color:#1e3a8a;font-size:13px;line-height:1.6;">
          {button_url}
        </p>
        """

    note_html = ""

    if extra_note:
        note_html = f"""
        <div style="margin-top:28px;padding-top:18px;border-top:1px solid #e5e7eb;">
          <p style="margin:0;color:#94a3b8;font-size:12px;line-height:1.6;">
            {extra_note}
          </p>
        </div>
        """

    return f"""
<div style="margin:0;padding:0;background:#f4f7fb;font-family:Arial,sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:34px 18px;">
    <div style="background:#ffffff;border-radius:24px;padding:36px 30px;border:1px solid #e5e7eb;box-shadow:0 10px 30px rgba(15,23,42,0.08);text-align:center;">

      <div style="margin-bottom:26px;">
        <img
          src="{_logo_url()}"
          alt="TOGÜ Logo"
          style="width:150px;height:auto;margin:0 auto;display:block;"
        />
      </div>

      <h1 style="margin:0 0 12px;color:#0f172a;font-size:26px;font-weight:800;">
        {title}
      </h1>

      <p style="margin:0 0 24px;color:#64748b;font-size:15px;line-height:1.75;">
        {description}
      </p>

      {button_html}

      {note_html}

    </div>
  </div>
</div>
"""

@never_cache
def splash_view(request):
    next_url = request.GET.get("next", f"{settings.URL_PREFIX}/dashboard/")

    return render(
        request,
        "accounts/splash.html",
        {
            "next_url": next_url,
        },
    )

@never_cache
@require_http_methods(["GET", "POST"])
def login_view(request):
    next_url = _safe_next_url(
        request.POST.get("next") or request.GET.get("next") or f"{settings.URL_PREFIX}/dashboard/"
    )

    if request.user.is_authenticated:
        return redirect(f"{settings.URL_PREFIX}/dashboard/")

    error = None

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip()

        if not username or not password:
            error = "Kullanıcı adı ve şifre zorunludur."
        else:
            user = authenticate(request, username=username, password=password)

            if user is None:
                error = "Kullanıcı adı veya şifre hatalı."
            elif not user.is_active:
                error = "Bu hesap pasif durumda."
            else:
                profile, _ = UserProfile.objects.get_or_create(user=user)

                if profile.status == "pending":
                    error = "Hesabınız admin onayı bekliyor."
                elif profile.status == "rejected":
                    error = "Hesabınız reddedildi."
                else:
                    login(request, user)
                    request.session.set_expiry(60 * 60 * 8)
                    _create_login_activity(request, user, profile)

                    if next_url:
                        return redirect(next_url)

                    return redirect(f"{settings.URL_PREFIX}/dashboard/")

    return render(
        request,
        "accounts/login.html",
        {
            "error": error,
            "next_url": next_url,
        },
    )


@never_cache
@require_http_methods(["GET", "POST"])
def admin_login_view(request):
    next_url = _safe_next_url(
        request.POST.get("next") or request.GET.get("next") or f"{settings.URL_PREFIX}/admin/"
    )

    if request.user.is_authenticated:
        return redirect(f"{settings.URL_PREFIX}/admin/")

    error = None

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip()

        if not username or not password:
            error = "Kullanıcı adı ve şifre zorunludur."
        else:
            user = authenticate(request, username=username, password=password)

            if user is None:
                error = "Kullanıcı adı veya şifre hatalı."
            elif not user.is_active:
                error = "Bu hesap pasif durumda."
            else:
                profile, _ = UserProfile.objects.get_or_create(user=user)

                if user.is_superuser or user.is_staff:
                    profile.role = "admin"
                    profile.status = "approved"
                    profile.save()

                if profile.status == "pending":
                    error = "Hesabınız admin onayı bekliyor."
                elif profile.status == "rejected":
                    error = "Hesabınız reddedildi."
                elif profile.role != "admin":
                    error = "Bu alan yalnızca admin içindir."
                else:
                    login(request, user)
                    request.session.set_expiry(60 * 60 * 8)
                    _create_login_activity(request, user, profile)

                    if next_url:
                        return redirect(next_url)

                    return redirect(f"{settings.URL_PREFIX}/admin/")

    return render(
        request,
        "accounts/admin_login.html",
        {
            "error": error,
            "next_url": next_url,
        },
    )


@never_cache
@require_http_methods(["GET", "POST"])
def register_view(request):
    form = UserRegisterForm(request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            user = form.save()

            try:
                send_email(
                    to=user.email,
                    subject="Kayıt Başvurunuz Alındı",
                    body=_base_mail_template(
                        title="Kayıt Başvurunuz Alındı",
                        description=(
                            "Fight Detection sistemine kayıt başvurunuz başarıyla alınmıştır. "
                            "Hesabınız admin onayından sonra aktif hale gelecektir."
                        ),
                        extra_note=(
                            f"Kayıtlı e-posta adresiniz: <strong>{user.email}</strong>. "
                            "Bu işlem size ait değilse bu e-postayı dikkate almayın."
                        ),
                    ),
                )
            except EmailServiceError as exc:
                print(f"Kayıt maili gönderilemedi: {exc}")

            return redirect("accounts:login")

    return render(
        request,
        "accounts/register.html",
        {
            "form": form,
        },
    )


@never_cache
@require_POST
def logout_view(request):
    logout(request)
    request.session.flush()

    response = redirect("accounts:login")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response


@login_required
def account_settings(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)

    if request.method == "POST" and "account_submit" in request.POST:
        account_form = AccountUpdateForm(
            request.POST,
            instance=request.user,
            profile=profile,
        )

        password_form = CustomPasswordChangeForm(request.user)

        if account_form.is_valid():
            account_form.save()
            messages.success(request, "Hesap bilgileriniz başarıyla güncellendi.")
            return redirect("accounts:account_settings")

    elif request.method == "POST" and "password_submit" in request.POST:
        account_form = AccountUpdateForm(
            instance=request.user,
            profile=profile,
        )

        password_form = CustomPasswordChangeForm(
            request.user,
            request.POST,
        )

        if password_form.is_valid():
            user = password_form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Şifreniz başarıyla güncellendi.")
            return redirect("accounts:account_settings")

    else:
        account_form = AccountUpdateForm(
            instance=request.user,
            profile=profile,
        )

        password_form = CustomPasswordChangeForm(request.user)

    return render(
        request,
        "accounts/account_settings.html",
        {
            "account_form": account_form,
            "password_form": password_form,
            "profile": profile,
        },
    )

@never_cache
@require_http_methods(["GET", "POST"])
def password_reset_request(request):
    if request.method == "POST":
        email = request.POST.get("email", "").strip()

        user = User.objects.filter(email=email).first()

        if user:
            profile, _ = UserProfile.objects.get_or_create(user=user)

            profile.password_reset_version += 1
            profile.save(update_fields=["password_reset_version"])

            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            version = profile.password_reset_version

            reset_link = (
                f"{settings.FRONTEND_URL}"
                f"/accounts/sifre-yenile/{uid}/{token}/{version}/"
            )

            try:
                send_email(
                    to=user.email,
                    subject="Şifre Yenileme Bağlantısı",
                    body=_base_mail_template(
                        title="Şifre Yenileme Talebi",
                        description=(
                            "Fight Detection platformundaki hesabınız için şifre yenileme talebi alınmıştır. "
                            "Hesabınızın güvenliğini korumak ve yeni şifrenizi oluşturmak için aşağıdaki butonu kullanabilirsiniz."
                        ),
                        button_text="Şifremi Yenile",
                        button_url=reset_link,
                        extra_note=(
                            "Bu işlemi siz yapmadıysanız bu e-postayı dikkate almayın. "
                            "Güvenliğiniz için bağlantıyı kimseyle paylaşmayın. "
                            "Yeni bir şifre yenileme bağlantısı talep ederseniz önceki bağlantılar geçersiz hale gelir."
                        ),
                    ),
                )

                messages.success(
                    request,
                    "Şifre yenileme bağlantısı e-posta adresinize gönderildi.",
                )

            except EmailServiceError as exc:
                print(f"Şifre yenileme maili gönderilemedi: {exc}")

                messages.error(
                    request,
                    "Şifre yenileme bağlantısı gönderilemedi. Lütfen daha sonra tekrar deneyin.",
                )

        else:
            messages.success(
                request,
                "Eğer bu e-posta sisteme kayıtlıysa şifre yenileme bağlantısı gönderildi.",
            )

        return redirect("accounts:login")

    return render(request, "accounts/password_reset_request.html")

@never_cache
@require_http_methods(["GET", "POST"])
def password_reset_confirm(request, uidb64, token, version):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
        profile, _ = UserProfile.objects.get_or_create(user=user)
    except Exception:
        user = None
        profile = None

    if (
        user is None
        or profile is None
        or version != profile.password_reset_version
        or not default_token_generator.check_token(user, token)
    ):
        messages.error(request, "Şifre yenileme bağlantısı geçersiz veya süresi dolmuş.")
        return redirect("accounts:password_reset_request")

    if request.method == "POST":
        password1 = request.POST.get("password1", "")
        password2 = request.POST.get("password2", "")

        if not password1 or not password2:
            messages.error(request, "Lütfen tüm alanları doldurun.")
            return redirect(
                "accounts:password_reset_confirm",
                uidb64=uidb64,
                token=token,
                version=version,
            )

        if password1 != password2:
            messages.error(request, "Şifreler eşleşmiyor.")
            return redirect(
                "accounts:password_reset_confirm",
                uidb64=uidb64,
                token=token,
                version=version,
            )

        if len(password1) < 8:
            messages.error(request, "Şifre en az 8 karakter olmalıdır.")
            return redirect(
                "accounts:password_reset_confirm",
                uidb64=uidb64,
                token=token,
                version=version,
            )

        user.set_password(password1)
        user.save()

        profile.password_reset_version += 1
        profile.save(update_fields=["password_reset_version"])

        messages.success(
            request,
            "Şifreniz başarıyla yenilendi. Yeni şifrenizle giriş yapabilirsiniz.",
        )
        return redirect("accounts:login")

    return render(
        request,
        "accounts/password_reset_confirm.html",
        {
            "uidb64": uidb64,
            "token": token,
            "version": version,
        },
    )


@never_cache
@require_http_methods(["GET"])
def password_reset_invalid(request, uidb64=None, token=None):
    return render(
        request,
        "accounts/password_reset_invalid.html",
        status=410,
    )