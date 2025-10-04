# app/config.py
from typing import Optional, Literal
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Base
    APP_ENV: str = "dev"
    SECRET_KEY: str                         # REQUIRED
    BASE_URL: str                           # REQUIRED (e.g., https://yourservice.onrender.com)

    # MongoDB
    MONGO_URI: str                          # REQUIRED
    MONGO_DB: str                           # REQUIRED

    # AWS S3 (optional; safe to leave unset if you're not uploading images)
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_REGION: str = "us-east-2"
    S3_BUCKET: Optional[str] = None

    # Twilio (optional now that you're using Telnyx)
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_FROM_NUMBER: Optional[str] = None

    # Telnyx tool token (required for your webhooks)
    TELNYX_TOOL_TOKEN: str                  # REQUIRED

    # Optional: County → office phone routing
    # Provide JSON like: {"harris":"+18324101662","brazoria":"+18325550123"}
    OFFICE_ROUTES_JSON: Optional[str] = None
    # Optional: Fallback transfer number if county not mapped
    DEFAULT_OFFICE_NUMBER: Optional[str] = None

    # IP geolocation (optional)
    IP_GEO_PROVIDER: Literal["ipinfo", "ipapi", "none"] = "none"
    IP_GEO_TOKEN: Optional[str] = None

    class Config:
        env_file = ".env"

settings = Settings()