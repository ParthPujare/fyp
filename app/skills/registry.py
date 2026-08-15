"""SkillRegistry — scans the folder/markdown registry from disk (plan.md §9).

Hot-reload by design: every ``load_all`` re-reads the ``skills/<scope>/<name>/skill.md``
files, so an edit made via the dashboard editor (or by hand) takes effect on the very
next agent run — the Week-11 deliverable "edit a skill → behavior changes on next run".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.skills.model import Skill, parse_skill, render_skill


class SkillRegistry:
    def __init__(self, root: str | Path | None = None):
        from app.config import get_settings

        self.root = Path(root) if root is not None else get_settings().skills.skills_dir

    # ---- reading (hot reload: re-scan disk every call) ----

    def _skill_files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.rglob("skill.md") if p.is_file())

    def load_all(self) -> list[Skill]:
        """Parse every skill on disk. Invalid files are skipped (see load_with_errors)."""
        skills, _ = self.load_with_errors()
        return skills

    def load_with_errors(self) -> tuple[list[Skill], list[str]]:
        """Return (valid skills, error strings for malformed skill.md files)."""
        skills: list[Skill] = []
        errors: list[str] = []
        for path in self._skill_files():
            try:
                skills.append(parse_skill(path))
            except Exception as exc:  # noqa: BLE001 - surface malformed skill files
                errors.append(str(exc))
        return skills, errors

    def by_name(self) -> dict[str, Skill]:
        return {s.name: s for s in self.load_all()}

    def get(self, name: str) -> Skill | None:
        return self.by_name().get(name)

    # ---- writing (dashboard editor / tests) ----

    def _dir_for(self, skill: Skill) -> Path:
        # skills/<scope-path>/<name>/  e.g. skills/central/x/  skills/dept/marketing/x/
        scope_segments = skill.scope.replace("/", "/").split("/")
        return self.root.joinpath(*scope_segments, skill.name)

    def save(self, skill: Skill) -> Path:
        """Write a skill.md to disk (create/update), hot-reloaded on next read.

        The resolved path must stay inside the registry root: a crafted
        skill name/scope containing ``..`` segments must not escape it (the
        session_007 delete-bug class applied to writes — hardening, week 13-14).
        """
        root = Path(self.root).resolve()
        skill_dir = self._dir_for(skill)
        if not skill_dir.resolve().is_relative_to(root):
            raise ValueError(f"skill path {skill_dir} escapes the registry root {root}")
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "skill.md"
        path.write_text(render_skill(skill), encoding="utf-8")
        return path

    def delete(self, name: str) -> bool:
        """Delete a skill folder. Returns False if the skill does not exist."""
        skill = self.get(name)
        if skill is None:
            return False
        root = Path(self.root).resolve()
        skill_dir = Path(skill.path).parent.resolve()
        # safety: never delete anything outside the registry root (skill.path must
        # resolve to a real skill folder, not the bare cwd / empty path)
        if skill_dir == root or not skill_dir.is_relative_to(root):
            return False
        if skill_dir.exists():
            for child in skill_dir.rglob("*"):
                if child.is_file():
                    child.unlink()
            skill_dir.rmdir()
            self._prune_empty_parents(skill_dir.parent)
        return True

    @staticmethod
    def _prune_empty_parents(path: Path) -> None:
        while path != path.parent and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
            path = path.parent

    # ---- schema ----

    @staticmethod
    def schema_for(skill: Skill) -> dict[str, Any]:
        """OpenAI-style function tool declaration (same shape as agent.tools.tool_schema)."""
        return {
            "type": "function",
            "function": {
                "name": skill.name,
                "description": skill.description(),
                "parameters": {
                    "type": "object",
                    "properties": skill.args_schema,
                    "required": list(skill.args_schema),
                },
            },
        }
