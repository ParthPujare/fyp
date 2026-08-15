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
    audit_path: Path = Path("data/audit.jsonl")
    members_path: Path = Path("data/members.sqlite")
    memory_path: Path = Path("data/memory.sqlite")
    checkpoint_path: Path = Path("data/checkpoints.sqlite")
    auth_url: str = "http://127.0.0.1:8000"
    gateway_url: str = "http://127.0.0.1:8001"
    dashboard_url: str = "http://127.0.0.1:8006"
    crm_url: str = "http://127.0.0.1:8002"
    hr_url: str = "http://127.0.0.1:8003"
    devops_url: str = "http://127.0.0.1:8004"
    finance_url: str = "http://127.0.0.1:8005"


class MemorySettings(BaseModel):
    """3-tier + episodic memory bounds (see ref_docs/plans/plan_mk1_core_harness.md §5.8)."""

    central_bytes: int = Field(default=8192, ge=1)
    dept_bytes: int = Field(default=16384, ge=1)
    user_bytes: int = Field(default=4096, ge=1)
    snapshot_max_chars: int = Field(default=6000, ge=1)
    recall_top_k: int = Field(default=5, ge=1)
    episodic_scope: str = "dept"  # "dept" (team recall) — locked decision (session_005)


class GuardrailSettings(BaseModel):
    """Loop guardrail bounds (see plan_mk1_core_harness.md §6)."""

    max_steps: int = Field(default=10, ge=1)
    tool_budget: int = Field(default=8, ge=1)
    call_timeout_seconds: float = Field(default=30.0, gt=0)
    run_timeout_seconds: float = Field(default=120.0, gt=0)


class SkillSettings(BaseModel):
    """Skills registry location (folder/markdown registry, see plan_mk1_core_harness.md §9)."""

    skills_dir: Path = Path("skills")


class Settings(BaseSettings):
    app_name: str = "fyp-agent-harness"
    env: str = "dev"

    jwt: JWTSettings = JWTSettings()
    llm: LLMSettings = LLMSettings()
    services: ServiceSettings = ServiceSettings()
    guardrails: GuardrailSettings = GuardrailSettings()
    memory: MemorySettings = MemorySettings()
    skills: SkillSettings = SkillSettings()

    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
