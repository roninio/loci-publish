"""Plugin version must be the same in pyproject.toml and plugin.json.

Background: every release bumps both `pyproject.toml` (Python package
metadata) and `.claude-plugin/plugin.json` (the manifest Claude Code's
marketplace actually reads). When the two drift, the marketplace
publishes one version while the Python package reports another, which
breaks upgrade logic (`hooks/session-init.sh` reads the manifest to
detect upgrades) and confuses bug reports.

This lint pins the contract: both files must report the same string.
Mirrors the prior convention visible in commits like c38c039
("bump 0.1.70 -> 0.1.71") where both files move in lockstep.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = PLUGIN_ROOT / "pyproject.toml"
PLUGIN_MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"


def _pyproject_version() -> str:
    """Extract `version = "..."` from the top-level `[project]` table.

    Hand-rolled instead of `tomllib` so the test runs identically on
    Python 3.10/3.11 wheels too; the regex is anchored to the
    `[project]` table to avoid matching a `version =` line in some
    other table (e.g. a tool-specific subtable).
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(
        r"\[project\][^\[]*?^\s*version\s*=\s*\"([^\"]+)\"",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert m, f"Could not find version in [project] table of {PYPROJECT}"
    return m.group(1)


def _plugin_manifest_version() -> str:
    data = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    version = data.get("version")
    assert isinstance(version, str) and version, (
        f"plugin.json is missing a string `version` field: {PLUGIN_MANIFEST}"
    )
    return version


def test_pyproject_and_plugin_manifest_versions_match():
    py = _pyproject_version()
    manifest = _plugin_manifest_version()
    assert py == manifest, (
        f"Version drift between pyproject.toml ({py}) and "
        f"{PLUGIN_MANIFEST.relative_to(PLUGIN_ROOT)} ({manifest}). "
        "Every release must bump both files in lockstep — the manifest "
        "is what the Claude Code marketplace reads and what "
        "session-init.sh uses to detect upgrades."
    )
