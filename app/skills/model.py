"""Skill model + skill.md frontmatter parsing (plan_mk1_core_harness.md §9).

A skill file is a folder ``skills/<scope>/<name>/skill.md``:

    ---
    name: launch_campaign
    trigger: "launch a marketing campaign"
    scope: dept/marketing
    owner_role: marketing_agent
    version: 1
    args_schema:
      campaign_name: {type: string, description: ...}
    steps:
      - Search for the customer
      - Create the campaign
    tool_calls:
      - tool: crm_create_campaign
        args: {name: "{{campaign_name}}", status: "launched"}
    ---
    (markdown body — human-readable narrative, shown in the dashboard editor)

The ``args_schema`` becomes the LLM-facing function-tool parameters; ``tool_calls``
is the deterministic sequence the harness executes when the skill is invoked.
``{{name}}`` placeholders in ``args`` are substituted from the invocation args.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

REQUIRED_FIELDS = ("name", "trigger", "scope", "tool_calls")


class SkillValidationError(ValueError):
    """Raised when a skill.md frontmatter is missing/invalid."""

    def __init__(self, name: str, code: str, detail: str = ""):
        self.code = code
        self.name = name
        message = f"skill {name!r}: {code}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class Skill:
    """A parsed, validated skill."""

    name: str
    trigger: str
    scope: str  # "central" | "dept/<name>"
    owner_role: str
    version: int
    args_schema: dict[str, Any]
    steps: list[str]
    tool_calls: list[dict[str, Any]]
    path: str
    body: str = ""  # markdown narrative for the dashboard/docs

    @property
    def policy_obj(self) -> str:
        """Casbin obj for this skill's visibility row (`skill_<name>`)."""
        return f"skill_{self.name}"

    def description(self) -> str:
        """LLM-facing tool description: trigger + step summary."""
        steps = "; ".join(self.steps) if self.steps else "; ".join(
            f"{tc.get('tool')}" for tc in self.tool_calls
        )
        return f"{self.trigger} — steps: {steps}"


def _ensure(name: str, data: dict[str, Any]) -> None:
    for field_name in REQUIRED_FIELDS:
        if not data.get(field_name):
            raise SkillValidationError(name, "missing_field", field_name)
    calls = data.get("tool_calls")
    if not isinstance(calls, list) or not all(
        isinstance(c, dict) and isinstance(c.get("tool"), str) for c in calls
    ):
        raise SkillValidationError(name, "invalid_tool_calls")
    scope = str(data.get("scope", ""))
    if scope != "central" and not scope.startswith("dept/"):
        raise SkillValidationError(name, "invalid_scope", scope)


def parse_skill(path: str | Path) -> Skill:
    """Read + parse one skill.md. Raises SkillValidationError on malformed files."""
    path = Path(path)
    skill = parse_skill_document(path.read_text(encoding="utf-8"), fallback_name=path.stem)
    # carry the real disk location so callers (e.g. SkillRegistry.delete) can act on it
    if not skill.path:
        skill = replace(skill, path=str(path))
    return skill


def parse_skill_document(text: str, *, fallback_name: str = "unnamed") -> Skill:
    """Parse a skill.md document string (frontmatter + body)."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise SkillValidationError(fallback_name, "no_frontmatter")
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillValidationError(fallback_name, "yaml_error", str(exc)) from exc
    if not isinstance(data, dict):
        raise SkillValidationError(fallback_name, "no_frontmatter")
    name = str(data.get("name") or fallback_name)
    _ensure(name, data)
    args_schema = data.get("args_schema") or {}
    if not isinstance(args_schema, dict):
        raise SkillValidationError(name, "invalid_args_schema")
    steps = data.get("steps") or []
    if not isinstance(steps, list):
        raise SkillValidationError(name, "invalid_steps")
    version = data.get("version")
    try:
        version = int(version) if version is not None else 1
    except (TypeError, ValueError):
        raise SkillValidationError(name, "invalid_version") from None
    return Skill(
        name=name,
        trigger=str(data.get("trigger", "")),
        scope=str(data.get("scope", "")),
        owner_role=str(data.get("owner_role", "")),
        version=version,
        args_schema=args_schema,
        steps=[str(s) for s in steps],
        tool_calls=[dict(c) for c in data["tool_calls"]],
        path="",
        body=match.group(2).strip() if match.group(2) else "",
    )


def render_skill(skill: Skill) -> str:
    """Serialize a Skill back to skill.md (used by the dashboard editor)."""
    frontmatter: dict[str, Any] = {
        "name": skill.name,
        "trigger": skill.trigger,
        "scope": skill.scope,
        "owner_role": skill.owner_role,
        "version": skill.version,
        "args_schema": skill.args_schema,
        "steps": skill.steps,
        "tool_calls": skill.tool_calls,
    }
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    body = skill.body.strip()
    return f"---\n{yaml_text}---\n\n{body}\n" if body else f"---\n{yaml_text}---\n"
