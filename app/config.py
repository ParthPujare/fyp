"""Central application configuration (pydantic-settings, backed by .env)."""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-secret-change-me-0123456789abcdef"  # >=32 bytes for HS256
# Public dev-only key (it is in the repo): never use it to protect real API keys.
DEFAULT_FERNET_KEY = "vqehzoXFoWkA1WYmxrpBDn_4CabfBOvUue9-GStFo9k="


class JWTSettings(BaseModel):
    secret: str = DEFAULT_JWT_SECRET
    algorithm: str = "HS256"
    expiry_minutes: int = Field(default=60, ge=1)
    # Mk18 Phase D: browser session cookies (httpOnly). Bearer headers keep
    # working for API clients/tests; browsers migrate to cookies. Dual slots
    # (`<base>_admin` / `<base>_member`) preserve the identity switcher; the
    # readable `<base>_active` cookie tracks the active slot. Derived names —
    # see app/harness/cookies.py.
    cookie_name: str = "harness_token"
    cookie_secure: bool = True
    cookie_samesite: str = "lax"


class LLMSettings(BaseModel):
    # "mock" (offline/CI) | openai | gemini | anthropic | groq | openrouter | custom_openai
    # ("litellm" is accepted as a legacy alias and resolved from the model prefix)
    provider: str = "mock"
    model: str = "gemini/gemini-3.6-flash"
    api_key: str | None = None
    base_url: str | None = None
    fernet_key: str = DEFAULT_FERNET_KEY
    temperature: float = 0.2
    max_tokens: int = 1024
    # Mk18 Phase H: per-user/day spend caps (LLM__DAILY_*). Over either cap the
    # turn falls back to Mock (gateway answers 429 pre-run). Cost is estimated
    # flat per generate call — no per-model pricing tables to drift.
    daily_call_cap: int = Field(default=500, ge=1)
    daily_cost_cents_cap: int = Field(default=500, ge=1)
    est_cost_cents_per_call: int = Field(default=1, ge=0)


class ServiceSettings(BaseModel):
    data_dir: Path = Path("data")
    audit_path: Path = Path("data/audit.jsonl")
    members_path: Path = Path("data/members.sqlite")
    memory_path: Path = Path("data/memory.sqlite")
    checkpoint_path: Path = Path("data/checkpoints.sqlite")
    policy_path: Path = Path("app/harness/policies/policy.csv")
    auth_url: str = "http://127.0.0.1:8000"
    gateway_url: str = "http://127.0.0.1:8001"
    dashboard_url: str = "http://127.0.0.1:8006"
    crm_url: str = "http://127.0.0.1:8002"
    hr_url: str = "http://127.0.0.1:8003"
    devops_url: str = "http://127.0.0.1:8004"
    finance_url: str = "http://127.0.0.1:8005"
    # Optional: HMAC key for the audit hash chain (tamper-evidence against someone who can
    # edit files but doesn't hold this key). Mk19 Phase 5: REQUIRED when ENV!=dev (startup
    # refuses without it); dev runs unkeyed with a warning. Mixed-mode verify: lines written
    # before a key was set stay plain-SHA and keep verifying — set the key at ANY time, no
    # migration, no chain rewrite (ref_docs/runbook_backup.md "enabling HMAC on an existing chain").
    audit_hmac_key: str | None = None
    # Mk18 Phase H: retired audit key(s) for rotation. Comma-separated; lines signed
    # under a retired key still verify (rotation = new current + old appended here).
    # See ref_docs/runbook_backup.md "HMAC rotation".
    audit_hmac_key_prev: str | None = None
    # Mk18 Phase H: optional HMAC for the trace chain (same rotation semantics).
    # Blank = plain SHA-256 (default, byte-identical behavior) in dev only;
    # Mk19 Phase 5: required when ENV!=dev, same mixed-mode verify as audit.
    trace_hmac_key: str | None = None
    trace_hmac_key_prev: str | None = None


class MemorySettings(BaseModel):
    """3-tier + episodic memory bounds (see ref_docs/plans/plan_mk1_core_harness.md §5.8)."""

    central_bytes: int = Field(default=8192, ge=1)
    dept_bytes: int = Field(default=16384, ge=1)
    user_bytes: int = Field(default=4096, ge=1)
    snapshot_max_chars: int = Field(default=6000, ge=1)
    recall_top_k: int = Field(default=5, ge=1)
    episodic_scope: str = "dept"  # "dept" (team recall) — locked decision (session_005)
    # Mk17: per-fact cap (MEMORY__MAX_FACT_CHARS). Tier caps stay; a single
    # huge fact must not wipe the tier. Code-config now, UI later.
    max_fact_chars: int = Field(default=2000, ge=1)


class GuardrailSettings(BaseModel):
    """Loop guardrail bounds (see plan_mk1_core_harness.md §6)."""

    max_steps: int = Field(default=10, ge=1)
    tool_budget: int = Field(default=8, ge=1)
    call_timeout_seconds: float = Field(default=30.0, gt=0)
    run_timeout_seconds: float = Field(default=120.0, gt=0)


class ApprovalSettings(BaseModel):
    """HITL approval routing (Mk15, see ref_docs/plans/plan_mk15_two_level_dept_approvals.md)."""

    # `risk=critical` tools always suspend. Set this to also suspend `risk=write` department
    # tools for the department head (these are the tool actions a head can "always allow").
    gate_write_tools: bool = False


class SkillSettings(BaseModel):
    """Skills registry location (folder/markdown registry, see plan_mk1_core_harness.md §9)."""

    skills_dir: Path = Path("skills")


class ComposioSettings(BaseModel):
    """External tool aggregator (Mk9) — global fallback; per-user BYO key wins."""

    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)
    gmail_auth_config: str | None = None
    github_auth_config: str | None = None


class SystemLLMSettings(BaseModel):
    """Platform-owned background LLM (Mk16) — NOT part of BYOK.

    One shared OpenRouter key serves every user server-side for memory
    extraction only. Chat itself stays BYOK-only (`LLM__*` + per-member keys).
    Canonical env is `SYSTEM__OPENROUTER_API_KEY`; a legacy top-level
    `OPENROUTER_API_KEY` is also accepted (backfilled in `Settings`).
    `model` is pinned to a `:free` id — the singleton refuses anything else.
    """

    openrouter_api_key: str | None = None
    model: str = "qwen/qwen3.8-27b:free"
    temperature: float = 0.0
    max_tokens: int = 512
    timeout_seconds: float = Field(default=15.0, gt=0)


class Settings(BaseSettings):
    app_name: str = "fyp-agent-harness"
    env: str = "dev"

    # Shared Postgres (Neon) connection string — Mk13. Unset/blank = every store keeps its
    # local SQLite file under data/ (the default for offline dev and the test suite).
    # Use Neon's *pooled* host (`…-pooler…`) here; see app/db/pool.py.
    database_url: str | None = None

    # Legacy top-level platform key (Mk16). Canonical is
    # `SYSTEM__OPENROUTER_API_KEY`; this alias exists because users already
    # carry `OPENROUTER_API_KEY` in `.env` (pydantic reads `.env`/shell into
    # declared fields only — `extra="ignore"` drops undeclared names, which is
    # why the key must be declared to be seen at all). Copied onto
    # `system.openrouter_api_key` by `_backfill_system_key` when the canonical
    # name is unset; never read directly elsewhere.
    openrouter_api_key: str | None = None

    jwt: JWTSettings = JWTSettings()
    llm: LLMSettings = LLMSettings()
    services: ServiceSettings = ServiceSettings()
    guardrails: GuardrailSettings = GuardrailSettings()
    memory: MemorySettings = MemorySettings()
    skills: SkillSettings = SkillSettings()
    approvals: ApprovalSettings = ApprovalSettings()
    composio: ComposioSettings = ComposioSettings()
    system: SystemLLMSettings = SystemLLMSettings()

    # Mk19 Phase 4: explicit hatch to run with the repo-public dev secrets
    # (JWT__SECRET, LLM__FERNET_KEY). Applies ONLY when ENV=dev; see
    # _no_dev_secrets_outside_dev. docker-compose.yml and scripts/run_dev.sh
    # default it to 1; every other deployment must set real secrets.
    allow_dev_secrets: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")

    @field_validator("database_url", mode="before")
    @classmethod
    def _blank_database_url_is_unset(cls, value: object) -> object:
        # `DATABASE_URL=` in .env arrives as "" — that means "not configured", not a DSN.
        if value is None or not str(value).strip():
            return None
        return str(value).strip()

    @model_validator(mode="after")
    def _backfill_system_key(self) -> "Settings":
        """Wire the platform key from either env name (blank = unset).

        Canonical `SYSTEM__OPENROUTER_API_KEY` (nested onto
        `system.openrouter_api_key`) wins; legacy top-level `OPENROUTER_API_KEY`
        (declared field above, so pydantic loads it from shell or `.env`) is
        the fallback. A stray shell export is also honored.
        """
        import os

        candidates = [
            (self.system.openrouter_api_key or "").strip(),
            (self.openrouter_api_key or "").strip(),
            (os.environ.get("OPENROUTER_API_KEY") or "").strip(),
        ]
        self.system.openrouter_api_key = next((c for c in candidates if c), None)
        if isinstance(self.openrouter_api_key, str) and not self.openrouter_api_key.strip():
            self.openrouter_api_key = None
        return self

    @model_validator(mode="after")
    def _no_unkeyed_chains_outside_dev(self) -> "Settings":
        """Mk19 Phase 5: audit + trace chains must be HMAC-keyed outside dev.

        A plain-SHA chain is forgeable by anyone who can edit the file. Dev
        keeps the unkeyed default with a startup warning (open_audit_log);
        every other env refuses to boot until both keys are set. Mixed-mode
        VERIFY already accepts old plain lines (resolve_key_chain keeps None),
        so adding a key to an existing chain needs no migration.
        """
        if self.env.strip().lower() != "dev":
            missing = [
                name
                for name, blank in (
                    ("SERVICES__AUDIT_HMAC_KEY", not (self.services.audit_hmac_key or "").strip()),
                    ("SERVICES__TRACE_HMAC_KEY", not (self.services.trace_hmac_key or "").strip()),
                )
                if blank
            ]
            if missing:
                raise ValueError(
                    f"ENV={self.env!r} requires keyed audit/trace chains; set "
                    f"{', '.join(missing)} (any value; existing plain lines keep verifying)"
                )
        return self

    @model_validator(mode="after")
    def _no_dev_secrets_outside_dev(self) -> "Settings":
        """Fail fast: built-in dev secrets need an explicit hatch (Mk19 Phase 4).

        The hatch is `ENV=dev` **and** `ALLOW_DEV_SECRETS=1`. Both conditions
        are required: `ENV=prod` (or any other env) refuses the built-ins even
        with the hatch set — a stray `ALLOW_DEV_SECRETS=1` leaking into prod is
        itself a misconfiguration and raises, rather than silently weakening the
        check. docker-compose.yml and scripts/run_dev.sh default the hatch to 1
        so bare-metal/compose dev keeps booting.
        """
        env = self.env.strip().lower()
        if self.allow_dev_secrets and env != "dev":
            raise ValueError(
                f"ALLOW_DEV_SECRETS=1 is only valid with ENV=dev (ENV={self.env!r}); "
                "unset it and set real JWT__SECRET / LLM__FERNET_KEY values"
            )
        if not (env == "dev" and self.allow_dev_secrets):
            unsafe = [
                name
                for name, bad in (
                    ("JWT__SECRET", self.jwt.secret == DEFAULT_JWT_SECRET),
                    ("LLM__FERNET_KEY", self.llm.fernet_key == DEFAULT_FERNET_KEY),
                )
                if bad
            ]
            if unsafe:
                raise ValueError(
                    f"ENV={self.env!r} but {', '.join(unsafe)} still use the built-in dev default; "
                    "set your own values (or ALLOW_DEV_SECRETS=1 with ENV=dev)"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
