"""Central configuration for every service in the monorepo.

One Settings object, populated from environment variables (see .env.example).
Nothing here should ever hold a literal secret -- only the *names* of the env
vars that carry them.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = Field(
        default="postgresql://jobingo:jobingo@localhost:5433/jobingo"
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg2://jobingo:jobingo@localhost:5433/jobingo"
    )
    redis_url: str = Field(default="redis://localhost:6380/0")

    # Phase 1
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_bot_username: str = ""
    # Set once the Mini App (Phase 4) is deployed; empty means the bot's
    # Play button honestly reports the game screen isn't open yet instead
    # of shipping a web_app button pointing nowhere.
    miniapp_url: str = ""

    # Phase 5-6
    chapa_api_key: str = ""
    santimpay_api_key: str = ""
    arifpay_api_key: str = ""

    # Phase 7 -- comma-separated IPs; empty means unrestricted (dev-friendly
    # default). Set for production: admin console should only be reachable
    # from a known office/VPN range.
    admin_ip_allowlist: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
