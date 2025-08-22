from __future__ import annotations
import httpx
from typing import Optional
from .config import settings

def send_sms(to_e164: str, body: str, *, media_url: Optional[str] = None) -> dict:
    """Simple Twilio REST call via HTTPX.
    Falls back to no-op if Twilio creds are not configured.
    """
    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_FROM_NUMBER):
        # In dev, just print so the app doesn't crash
        print(f"[DEV] SMS to {to_e164}: {body} (media={media_url})")
        return {"ok": True, "dev": True}

    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    data = {"To": to_e164, "From": settings.TWILIO_FROM_NUMBER, "Body": body}
    if media_url:
        data["MediaUrl"] = media_url
    with httpx.Client(timeout=10.0) as client:
        r = client.post(url, data=data, auth=auth)
        r.raise_for_status()
        return r.json()
