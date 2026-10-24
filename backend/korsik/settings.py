from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator, field_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KORSIK_", extra="ignore")
    database_url: str = "sqlite:///./korsik.db"
    database_host: str = ""
    database_name: str = "korsik"
    database_user: str = ""
    database_password_file: str = ""

    @model_validator(mode="after")
    def resolve_database(self):
        if self.database_host:
            from sqlalchemy.engine import URL

            self.database_url = URL.create(
                "postgresql+psycopg",
                username=self.database_user,
                password=read_secret(self.database_password_file),
                host=self.database_host,
                database=self.database_name,
            ).render_as_string(hide_password=False)
        return self

    public_url: str = "http://localhost:8080"
    blob_root: str = "./data/blobs"
    secret_key_file: str = "./secrets/session_key"
    admin_email: str = "admin"
    admin_password_file: str = "./secrets/admin_password"
    product_mode: str = "CORE"
    generation_enabled: bool = False
    generator_model_subdir: str = "generator-4b"
    max_mode: str = "disabled"
    max_api_url: str = "https://platform-api.max.ru"
    max_bot_url: str = "https://max.ru/id9729381390_4_bot"

    @field_validator("max_bot_url")
    @classmethod
    def validate_bot_url(cls, value):
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        try:
            valid_port = parsed.port in (None, 443)
        except ValueError:
            valid_port = False
        if (
            parsed.scheme != "https"
            or parsed.hostname != "max.ru"
            or not valid_port
            or parsed.username
            or parsed.password
            or not parsed.path.strip("/")
            or parsed.query
            or parsed.fragment
            or any(ch.isspace() for ch in value)
        ):
            raise ValueError(
                "MAX bot URL must be an HTTPS max.ru bot link without query, fragment or credentials"
            )
        return value.rstrip("/")

    max_token_file: str = "/run/secrets/max_token"
    max_webhook_secret_file: str = "/run/secrets/max_webhook_secret"
    internal_secret_file: str = "/run/secrets/internal_key"
    secure_cookies: bool = False
    testing: bool = False
    seed_source: str = "./seed/source.xlsx"
    model_manifest_dir: str = "./models"
    encoder_url: str = "http://encoder:8000"
    reranker_url: str = "http://reranker:8000"
    generator_url: str = "http://generator:8000"
    qdrant_url: str = "http://qdrant:6333"
    scheduler_tick_seconds: float = 5
    session_ttl_seconds: int = 43200
    generation_max_waiting: int = 8
    generation_per_conversation_pending: int = 1
    generation_queue_wait_seconds: int = 20
    chat_deadline_seconds: int = 60


@lru_cache
def get_settings():
    return Settings()


def read_secret(path: str, required=True) -> str:
    p = Path(path)
    if p.is_file():
        value = p.read_text().strip()
        if value:
            return value
    if required:
        raise RuntimeError(f"Required secret file is unavailable: {p.name}")
    return ""
