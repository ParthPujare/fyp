"""Composio settings — global fallback key; per-user BYO key wins (Mk9)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ComposioSettings(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)
