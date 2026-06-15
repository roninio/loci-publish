"""Workspace custom agents mirror the bundled LOCI skills."""

from __future__ import annotations

from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = PLUGIN_ROOT / "skills"
AGENTS_DIR = PLUGIN_ROOT / "agents"

pytestmark = pytest.mark.unit


def _skill_names() -> set[str]:
    return {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}


def _agent_names() -> set[str]:
    return {p.name.removesuffix(".agent.md") for p in AGENTS_DIR.glob("*.agent.md")}


def _frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} must start with YAML frontmatter"
    end = text.find("\n---", 4)
    assert end != -1, f"{path} frontmatter must be closed with ---"
    return text[4:end]


def test_each_skill_has_matching_agent():
    assert _agent_names() == _skill_names()


@pytest.mark.parametrize("skill_name", sorted(_skill_names()))
def test_agent_references_matching_skill(skill_name: str):
    agent_md = AGENTS_DIR / f"{skill_name}.agent.md"
    text = agent_md.read_text(encoding="utf-8")
    frontmatter = _frontmatter(agent_md)

    assert "description:" in frontmatter
    assert "user-invocable: true" in frontmatter
    assert "disable-model-invocation: false" in frontmatter
    assert f"skills/{skill_name}/SKILL.md" in text