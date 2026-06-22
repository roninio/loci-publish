"""Plugin manifest advertises bundled agents and skills."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
AGENTS_DIR = PLUGIN_ROOT / "agents"
SKILLS_DIR = PLUGIN_ROOT / "skills"

pytestmark = pytest.mark.unit


def _manifest() -> dict:
    return json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))


def _skill_paths() -> list[str]:
    return [f"./skills/{p.name}" for p in sorted(SKILLS_DIR.iterdir()) if p.is_dir()]


def test_plugin_manifest_advertises_root_agents_folder():
    data = _manifest()

    assert data.get("agents") == ["./agents"]
    assert AGENTS_DIR.is_dir()
    assert any(AGENTS_DIR.glob("*.agent.md"))


def test_plugin_manifest_advertises_all_skill_folders():
    data = _manifest()

    assert data.get("skills") == _skill_paths()


@pytest.mark.parametrize("skill_path", _skill_paths())
def test_manifest_skill_paths_exist(skill_path: str):
    skill_dir = PLUGIN_ROOT / skill_path.removeprefix("./")

    assert skill_dir.is_dir()
    assert (skill_dir / "SKILL.md").is_file()