from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/pickup_api"
    supabase_url: str | None = None
    supabase_jwt_secret: str | None = None
    expo_access_token: str | None = None
    cors_origins: str = "http://localhost:8081,http://localhost:19006"
    push_poll_seconds: int = 3

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
