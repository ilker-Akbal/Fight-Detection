from functools import wraps
from django.http import HttpResponseForbidden


def role_required(allowed_roles=None):
    allowed_roles = allowed_roles or []

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return HttpResponseForbidden("Giriş yapmalısınız.")

            profile = getattr(request.user, "profile", None)
            if profile is None:
                return HttpResponseForbidden("Kullanıcı profili bulunamadı.")

            if not profile.is_approved:
                return HttpResponseForbidden("Hesabınız henüz onaylanmamış.")

            if profile.role not in allowed_roles:
                return HttpResponseForbidden("Bu işlem için yetkiniz yok.")

            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator