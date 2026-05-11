"""Configuration via environment variables (UNIBET_ prefix)."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "UNIBET_", "extra": "ignore"}

    port: int = 5333
    db_path: str = "unibet_sports_prices.db"

    poll_interval: float = 0.5
    delta_poll_interval: float = 0.5
    match_refresh_min: float = 5.0
    max_match_age_h: float = 48.0

    db_flush_interval: float = 0.5
    db_batch_size: int = 100

    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown: float = 300.0

    ssl_verify: bool = False
    connection_pool_limit: int = 20
    connection_pool_per_host: int = 10

    feed_queue_maxsize: int = 20_000
    sse_queue_maxsize: int = 500

    # ── SPORT CONFIG ────────────────────────────────────────────────────────────
    tennis_enabled: bool = True
    soccer_enabled: bool = True

    tennis_path_ids: list[str] = ["p239", "p58484924", "p58484929"]
    soccer_path_ids: list[str] = ["p240"]

    tennis_market_ids: dict[int, str] = {68: "Face a Face"}
    soccer_market_ids: dict[int, str] = {1: "1X2", 18: "Over/Under 2.5", 24: "BTTS"}

    sport_codes: dict[str, str] = {"TENN": "tennis", "FOOT": "soccer"}


settings = Settings()
