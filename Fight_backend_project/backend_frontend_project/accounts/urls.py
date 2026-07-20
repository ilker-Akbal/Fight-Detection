from django.urls import path

from .views import (
    login_view,
    admin_login_view,
    register_view,
    logout_view,
    account_settings,
    password_reset_request,
    password_reset_confirm,
    password_reset_invalid,
    splash_view,
)

app_name = "accounts"

urlpatterns = [
    path("splash/", splash_view, name="splash"),

    path("login/", login_view, name="login"),
    path("register/", register_view, name="register"),
    path("admin-login/", admin_login_view, name="admin_login"),
    path("logout/", logout_view, name="logout"),
    path("hesabim/", account_settings, name="account_settings"),

    path("sifremi-unuttum/", password_reset_request, name="password_reset_request"),

    path(
        "sifre-yenile/<uidb64>/<token>/<int:version>/",
        password_reset_confirm,
        name="password_reset_confirm",
    ),

    path(
        "sifre-yenile/<uidb64>/<token>/",
        password_reset_invalid,
        name="password_reset_invalid",
    ),
]