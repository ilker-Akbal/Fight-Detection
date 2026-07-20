from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


class AuthRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

        self.public_paths = [
            "/accounts/splash/",
            "/accounts/login/",
            "/accounts/admin-login/",
            "/accounts/register/",
            "/accounts/logout/",
            "/accounts/teacher-register/",
            "/accounts/sifremi-unuttum/",
            "/accounts/sifre-yenile/",
            "/static/",
            "/media/",
            "/djadmin/login/",
            "/favicon.ico",
        ]

        self.admin_alias_paths = [
            "/accounts/admin",
            "/accounts/admin/",
            "/accounts/admin-panel",
            "/accounts/admin-panel/",
            "/admin-panel",
            "/admin-panel/",
            "/panel",
            "/panel/",
        ]

    def __call__(self, request):
        prefix = settings.URL_PREFIX or ""

        # path_info bazı durumlarda prefix'i içerir, bazen içermez.
        # FORCE_SCRIPT_NAME setli olsa bile gunicorn --script-name
        # geçirilmediği için path_info prefix'li gelebiliyor.
        # Her iki durumda da path'i prefix'siz hale getir.
        raw_path = request.path_info
        if prefix and raw_path.startswith(prefix):
            path = raw_path[len(prefix):] or "/"
        else:
            path = raw_path

        if path in self.admin_alias_paths:
            if request.user.is_authenticated:
                return redirect(f"{prefix}/admin/")

            return redirect(f"{reverse('accounts:admin_login')}?next={prefix}/admin/")

        is_public = any(path.startswith(p) for p in self.public_paths)

        if not request.user.is_authenticated and not is_public:
            full_path = f"{prefix}{path}"
            if request.META.get("QUERY_STRING"):
                full_path = f"{prefix}{path}?{request.META['QUERY_STRING']}"

            if (
                path.startswith("/admin/")
                or path.startswith("/panel/")
                or path.startswith("/admin-panel/")
            ):
                return redirect(f"{reverse('accounts:admin_login')}?next={full_path}")

            return redirect(f"{reverse('accounts:splash')}?next={full_path}")

        response = self.get_response(request)

        if request.user.is_authenticated and not is_public:
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"

        return response