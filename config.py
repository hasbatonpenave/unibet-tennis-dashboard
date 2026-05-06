"""Configuration via environment variables (UNIBET_ prefix)."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "UNIBET_", "extra": "ignore"}

    port: int = 5333
    db_path: str = "unibet_tennis_prices.db"

    poll_interval: float = 3.0
    delta_poll_interval: float = 2.0
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


settings = Settings()
