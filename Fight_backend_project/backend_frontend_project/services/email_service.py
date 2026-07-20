import requests
from django.conf import settings


class EmailServiceError(Exception):
    pass


def send_email(to: str, subject: str, body: str) -> dict:
    if not getattr(settings, "MAIL_SERVICE_ENABLED", False):
        return {
            "ok": False,
            "skipped": True,
            "message": "Mail servisi kapalı.",
        }

    mail_api_url = getattr(settings, "MAIL_API_URL", "")
    mail_api_key = getattr(settings, "MAIL_API_KEY", "")

    if not mail_api_url:
        raise EmailServiceError("MAIL_API_URL tanımlı değil.")

    if not mail_api_key:
        raise EmailServiceError("MAIL_API_KEY tanımlı değil.")

    if not to:
        raise EmailServiceError("Alıcı mail adresi boş olamaz.")

    payload = {
        "to": to,
        "subject": subject,
        "body": body,
    }

    headers = {
        "accept": "application/json",
        "X-API-KEY": mail_api_key,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            mail_api_url,
            json=payload,
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise EmailServiceError(f"Mail servisine ulaşılamadı: {exc}") from exc

    if response.status_code >= 400:
        raise EmailServiceError(
            f"Mail gönderimi başarısız. HTTP {response.status_code}: {response.text}"
        )

    try:
        response_data = response.json()
    except ValueError:
        response_data = {"raw_response": response.text}

    return {
        "ok": True,
        "status_code": response.status_code,
        "data": response_data,
    }