import os
from dataclasses import dataclass

@dataclass(frozen=True)
class _Settings:
    ACCESS_TOKEN: str
    VERIFY_TOKEN: str
    APP_SECRET: str | None
    PHONE_NUMBER_ID: str | None
    OPENAI_API_KEY: str
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE: str
    GRAPH_API_VERSION: str
    TZ: str
    DEBUG: bool

def load_settings() -> _Settings:
    return _Settings(
        ACCESS_TOKEN=os.getenv("ACCESS_TOKEN", ""),
        VERIFY_TOKEN=os.getenv("VERIFY_TOKEN", ""),
        APP_SECRET=os.getenv("APP_SECRET"),  # opcional si verificás firma
        PHONE_NUMBER_ID=os.getenv("YOUR_PHONE_NUMBER") or os.getenv("PHONE_NUMBER_ID"),
        OPENAI_API_KEY=os.getenv("OPENAI_API_KEY", ""),
        SUPABASE_URL=os.getenv("SUPABASE_URL", ""),
        SUPABASE_SERVICE_ROLE=os.getenv("SUPABASE_SERVICE_ROLE", ""),
        GRAPH_API_VERSION=os.getenv("GRAPH_API_VERSION", "23.0"),
        TZ=os.getenv("TZ", "America/Montevideo"),
        DEBUG=(os.getenv("DEBUG", "false").lower() == "true"),
    )

SETTINGS = load_settings()
