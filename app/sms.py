from __future__ import annotations
import httpx
from typing import Optional
from .config import settings

def _send_telnyx_sms(to_e164: str, body: str, *, media_url: Optional[str] = None) -> dict:
    """Send SMS via Telnyx Messaging API.
    Requires TELNYX_API_KEY and either TELNYX_MESSAGING_FROM_NUMBER or TELNYX_MESSAGING_PROFILE_ID.
    API docs: https://developers.telnyx.com/docs/api/v2/messaging/Messages
    """
    if not (settings.TELNYX_API_KEY and (settings.TELNYX_MESSAGING_FROM_NUMBER or settings.TELNYX_MESSAGING_PROFILE_ID)):
        raise RuntimeError("Telnyx messaging not configured")

    url = "https://api.telnyx.com/v2/messages"
    headers = {
        "Authorization": f"Bearer {settings.TELNYX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": to_e164,
        "text": body,
    }
    # Prefer a dedicated sending number if provided; otherwise use messaging_profile_id
    if settings.TELNYX_MESSAGING_FROM_NUMBER:
        payload["from"] = settings.TELNYX_MESSAGING_FROM_NUMBER
    elif settings.TELNYX_MESSAGING_PROFILE_ID:
        payload["messaging_profile_id"] = settings.TELNYX_MESSAGING_PROFILE_ID

    # Telnyx supports MMS via 'media_urls' array
    if media_url:
        payload["media_urls"] = [media_url]

    with httpx.Client(timeout=10.0) as client:
        r = client.post(url, json=payload, headers=headers)
        content = None
        try:
            content = r.json()
        except Exception:
            content = {"raw_text": r.text}
        # Raise for http errors, but still return rich info in exception path
        r.raise_for_status()
        return {"provider": "telnyx", "status_code": r.status_code, "data": content}

def _send_twilio_sms(to_e164: str, body: str, *, media_url: Optional[str] = None) -> dict:
    """Send SMS via Twilio REST API (fallback when Telnyx is not configured)."""
    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_FROM_NUMBER):
        raise RuntimeError("Twilio not configured")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    data = {"To": to_e164, "From": settings.TWILIO_FROM_NUMBER, "Body": body}
    if media_url:
        data["MediaUrl"] = media_url
    with httpx.Client(timeout=10.0) as client:
        r = client.post(url, data=data, auth=auth)
        content = None
        try:
            content = r.json()
        except Exception:
            content = {"raw_text": r.text}
        r.raise_for_status()
        return {"provider": "twilio", "status_code": r.status_code, "data": content}

def send_sms(to_e164: str, body: str, *, media_url: Optional[str] = None) -> dict:
    """Send an SMS using the first available provider.
    Preference order: Telnyx → Twilio → dev no-op.
    """
    # Try Telnyx first
    if settings.TELNYX_API_KEY and (settings.TELNYX_MESSAGING_FROM_NUMBER or settings.TELNYX_MESSAGING_PROFILE_ID):
        try:
            return _send_telnyx_sms(to_e164, body, media_url=media_url)
        except Exception as e:
            print(f"[WARN] Telnyx SMS failed: {e}")

    # Fallback to Twilio
    if settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_FROM_NUMBER:
        try:
            return _send_twilio_sms(to_e164, body, media_url=media_url)
        except Exception as e:
            print(f"[WARN] Twilio SMS failed: {e}")

    # Dev no-op
    print(f"[DEV] SMS to {to_e164}: {body} (media={media_url})")
    return {"provider": "dev-noop", "status_code": 200, "data": {"ok": True, "dev": True}}
