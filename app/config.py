"""Central application configuration (pydantic-settings, backed by .env)."""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class JWTSettings(BaseModel):
    secret: str = "dev-secret-change-me-0123456789abcdef"  # >=32 bytes for HS256
    algorithm: str = "HS256"
    expiry_minutes: int = Field(default=60, ge=1)


class LLMSettings(BaseModel):
    provider: str = "mock"  # "mock" (offline/CI) | "litellm" (real model)
    model: str = "gemini/gemini-2.0-flash"
    api_key: str | None = None
    temperature: float = 0.2
    max_tokens: int = 1024


class ServiceSettings(BaseModel):
    data_dir: Path = Path("data")
    auth_url: str = "http://127.0.0.1:8000"
    gateway_url: str = "http://127.0.0.1:8001"
    crm_url: str = "http://127.0.0.1:8002"
    hr_url: str = "http://127.0.0.1:8003"
    devops_url: str = "http://127.0.0.1:8004"
    finance_url: str = "http://127.0.0.1:8005"


class GuardrailSettings(BaseModel):
    """Loop guardrail bounds (see plan.md §6)."""

    max_steps: int = Field(default=10, ge=1)
    tool_budget: int = Field(default=8, ge=1)
    call_timeout_seconds: float = Field(default=30.0, gt=0)
    run_timeout_seconds: float = Field(default=120.0, gt=0)


class Settings(BaseSettings):
    app_name: str = "fyp-agent-harness"
    env: str = "dev"

    jwt: JWTSettings = JWTSettings()
    llm: LLMSettings = LLMSettings()
    services: ServiceSettings = ServiceSettings()
    guardrails: GuardrailSettings = GuardrailSettings()

    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
