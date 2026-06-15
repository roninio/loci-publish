"""Agent Skills metadata contract for bundled LOCI skills."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = PLUGIN_ROOT / "skills"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

pytestmark = pytest.mark.unit


def _frontmatter(skill_md: Path) -> dict[str, str]:
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill_md} must start with YAML frontmatter"

    end = text.find("\n---", 4)
    assert end != -1, f"{skill_md} frontmatter must be closed with ---"

    result: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in text[4:end].splitlines():
        if not raw_line.strip():
            continue
        if raw_line[0].isspace():
            if current_key is not None:
                result[current_key] = f"{result[current_key]} {raw_line.strip()}".strip()
            continue

        key, sep, value = raw_line.partition(":")
        assert sep, f"{skill_md} frontmatter line is not key: value: {raw_line!r}"
        current_key = key.strip()
        value = value.strip()
        result[current_key] = "" if value in ("", ">", "|") else value

    return result


@pytest.mark.parametrize("skill_dir", sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()))
def test_skill_has_agent_skills_discovery_metadata(skill_dir: Path):
    metadata = _frontmatter(skill_dir / "SKILL.md")

    assert metadata.get("name") == skill_dir.name
    assert NAME_RE.fullmatch(metadata["name"])

    description = metadata.get("description", "").strip()
    assert description
    assert len(description) <= 1024