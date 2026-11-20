from functools import lru_cache

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    secret_key: SecretStr = SecretStr("development-only-change-this-key-before-deployment")
    access_token_minutes: int = 60
    mongodb_uri: str | None = None
    mongodb_database: str = "agricultural_advisory"
    cors_origins: str = "http://localhost:5173"
    chroma_path: str = "./.chroma"
    chroma_server_host: str | None = None
    chroma_server_port: int = 8000
    upload_dir: str = "./uploads"
    max_upload_bytes: int = 10 * 1024 * 1024
    llm_provider: str = "openai"
    llm_model_id: str = "gpt-4o-mini"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str | None = None
    weather_api_url: str | None = None
    weather_api_key: SecretStr | None = None
    market_api_url: str | None = None
    market_api_key: SecretStr | None = None
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("llm_provider")
    @classmethod
    def supported_llm_provider(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {"openai", "openai_compatible", "azure_openai", "groq"}:
            raise ValueError("LLM_PROVIDER must be openai, openai_compatible, azure_openai, or groq")
        return normalized

    @property
    def allowed_origins(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "azure_openai":
            return bool(self.azure_openai_endpoint and self.llm_api_key)
        return bool(self.llm_api_key)

    @model_validator(mode="after")
    def production_secrets(self) -> "Settings":
        if self.app_env == "production":
            if (
                len(self.secret_key.get_secret_value()) < 32
                or "development-only" in self.secret_key.get_secret_value()
            ):
                raise ValueError(
                    "SECRET_KEY must be a unique random value with at least 32 characters in production"
                )
            if not self.mongodb_uri:
                raise ValueError("MONGODB_URI is required in production")
            if not self.chroma_server_host:
                raise ValueError("Use a server-backed Chroma deployment in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
