"""Environment configuration. Every value can be overridden with an env var
(see backend/.env.example). Nothing here assumes real credentials exist."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Indocount Smart Factory Intelligence"
    # Local development falls back to SQLite; Docker Compose sets PostgreSQL.
    database_url: str = "sqlite:///./indocount.db"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Auth
    jwt_secret: str = "indocount-demo-secret-change-me-in-production"
    jwt_expire_minutes: int = 12 * 60

    # Simulation
    sim_autostart: bool = True
    sim_tick_seconds: float = 3.0
    sim_start_hour: int = 11          # simulated clock starts at 11:00 on the seed day
    sim_default_speed: int = 1        # simulated minutes per tick
    sim_seed: int = 42

    # Business rules (illustrative, configurable)
    detention_charge_usd: float = 350.0
    detention_free_minutes: int = 120
    changeover_standard_minutes: float = 3.0
    shift2_differential_usd: float = 1.0
    extra_break_minutes: int = 0      # additional configurable breaks per shift

    # AI assistant: "mock" (local, no network) or "anthropic"
    llm_provider: str = "mock"
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    anthropic_api_key: str | None = None

    # Computer vision: "simulated" or "opencv" (OpenCV contour counting on frames)
    cv_provider: str = "opencv"
    cv_video_source: str | None = None   # optional RTSP/file source for a real camera

    # Future integrations (not connected in the POC)
    sap_b1_url: str | None = None
    hrms_url: str | None = None
    iot_broker_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
