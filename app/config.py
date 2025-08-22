from pydantic_settings import BaseSettings
from typing import Literal

class Settings(BaseSettings):
    APP_ENV: str = "dev"
    SECRET_KEY: str
    BASE_URL: str

    MONGO_URI: str
    MONGO_DB: str

    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_REGION: str = "us-east-2"
    S3_BUCKET: str

    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None
    TWILIO_FROM_NUMBER: str | None = None

    TELNYX_TOOL_TOKEN: str | None = None

    IP_GEO_PROVIDER: Literal["ipinfo", "ipapi", "none"] = "none"
    IP_GEO_TOKEN: str | None = None
    IP_GEO_TTL_SEC: int = 86400

    class Config:
        env_file = ".env"

settings = Settings()
