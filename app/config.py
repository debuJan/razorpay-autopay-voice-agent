"""Configuration loaded from environment variables (see .env.example)."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    vapi_api_key: str = field(default_factory=lambda: _env("VAPI_API_KEY"))
    vapi_assistant_id: str = field(default_factory=lambda: _env("VAPI_ASSISTANT_ID"))
    vapi_phone_number_id: str = field(default_factory=lambda: _env("VAPI_PHONE_NUMBER_ID"))
    demo_phone_number: str = field(default_factory=lambda: _env("DEMO_PHONE_NUMBER"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./autopay.db"))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL"))
    vapi_voice_provider: str = field(default_factory=lambda: _env("VAPI_VOICE_PROVIDER", "11labs"))
    vapi_voice_id: str = field(default_factory=lambda: _env("VAPI_VOICE_ID"))
    # Shared secret for authenticating Vapi webhooks (X-Webhook-Secret header).
    vapi_webhook_secret: str = field(default_factory=lambda: _env("VAPI_WEBHOOK_SECRET"))


def get_settings() -> Settings:
    return Settings()


def db_path_from_url(url: str) -> str:
    """Convert a sqlite:// URL to a filesystem path."""
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    if url.startswith("sqlite://"):
        return url[len("sqlite://"):]
    return url


def default_db_path() -> str:
    return db_path_from_url(get_settings().database_url)
