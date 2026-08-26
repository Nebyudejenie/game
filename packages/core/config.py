"""Central configuration for every service in the monorepo.

One Settings object, populated from environment variables (see .env.example).
Nothing here should ever hold a literal secret -- only the *names* of the env
vars that carry them.
"""

from decimal import Decimal
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
    # Public HTTPS base URL this deployment is reachable at -- used to build
    # the webhook/return URLs handed to a payment provider at checkout
    # creation. Empty means honestly refuse to start a deposit rather than
    # hand a provider a URL pointing nowhere (same "not available yet"
    # discipline as miniapp_url before Phase 4).
    public_base_url: str = ""
    min_deposit_etb: Decimal = Decimal("10.00")
    daily_deposit_cap_etb: Decimal = Decimal("50000.00")
    min_withdraw_etb: Decimal = Decimal("50.00")
    # Auto-approved without landing in the admin review queue.
    auto_approve_withdraw_etb: Decimal = Decimal("2000.00")
    # kyc_level must be >= 2 for any withdrawal above this amount.
    kyc_required_above_etb: Decimal = Decimal("5000.00")
    # A succeeded deposit within this window blocks a withdrawal request --
    # the chargeback window on a reversible rail (spec 8.3).
    withdraw_chargeback_window_minutes: int = 30
    # spec 8.4's anti-fraud table: "Withdrawal velocity > 3/day -> Review".
    # The Nth request within a rolling 24h window that would exceed this
    # count is forced to the admin review queue rather than auto-approved,
    # regardless of amount or KYC level.
    max_withdrawals_per_day: int = 3

    # Phase 7 -- comma-separated IPs; empty means unrestricted (dev-friendly
    # default). Set for production: admin console should only be reachable
    # from a known office/VPN range.
    admin_ip_allowlist: str = ""

    # 64 hex characters (32 bytes) -- packages/core/phone_crypto.py derives
    # both the phone-number encryption key and the lookup-hash key from
    # this one root secret via HKDF. Unlike CHAPA_API_KEY etc., this has no
    # safe empty default: registration cannot function without it in any
    # environment, dev/test included, the same way DATABASE_URL/REDIS_URL
    # don't have empty defaults either. Generate one with:
    # python -c "import secrets; print(secrets.token_hex(32))"
    phone_encryption_key: str = ""

    # Observability -- empty means reconcile_job.py only logs its result
    # (still fully correct: a real scheduler already alerts on the process's
    # own non-zero exit code); set to enable pushing the mismatch count to a
    # real Prometheus Pushgateway too, since reconcile_job is a one-shot CLI
    # job with no long-running /metrics endpoint of its own to scrape.
    pushgateway_url: str = ""
    # Base OTLP endpoint (e.g. http://localhost:4318) for the deposit and
    # payout traces spec section 10.4 asks for. Empty means tracing calls
    # are all still safe to make (OpenTelemetry's own no-op default
    # tracer), just discarded rather than exported anywhere.
    otel_exporter_endpoint: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
