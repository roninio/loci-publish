"""Unit tests for STATE_DIR resolution and path helpers in loci_stats.

Persistent state (measurements, stats, cursor, logs) must live outside
the versioned plugin dir so it survives plugin upgrades. These tests
verify the resolution precedence and that every state-file-emitting
helper honours it.

Also includes a lint check: scan lib/ and hooks/ for references to
`PLUGIN_DIR/state/` or `${STATE_DIR}` writes that would route new data
back to the versioned location. An allowlist captures the small set of
known-good references (session-init.sh exporting LOCI_STATE_DIR, bash
fallback logic, skill doc comments).
"""

import importlib
import json
import re
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def reload_loci_stats(monkeypatch):
    """Re-import loci_stats so module-level STATE_DIR resolves under the
    current env. Returns the freshly-imported module."""

    def _reload():
        if "loci_stats" in sys.modules:
            del sys.modules["loci_stats"]
        return importlib.import_module("loci_stats")

    return _reload


def test_state_dir_honours_env_var(tmp_path, monkeypatch, reload_loci_stats):
    monkeypatch.setenv("LOCI_STATE_DIR", str(tmp_path / "custom-state"))
    mod = reload_loci_stats()
    assert mod.STATE_DIR == tmp_path / "custom-state"


def test_state_dir_defaults_to_project_local(tmp_path, monkeypatch, reload_loci_stats):
    """With no env override, state lives in <cwd>/.loci/state so all LOCI
    artifacts stay with the project being analyzed."""
    monkeypatch.delenv("LOCI_STATE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    mod = reload_loci_stats()
    assert mod.STATE_DIR == tmp_path / ".loci" / "state"
    # Directory should actually get created
    assert (tmp_path / ".loci" / "state").is_dir()
    # ...and shielded from the user's repo with a .gitignore guard.
    assert (tmp_path / ".loci" / "state" / ".gitignore").read_text() == "*\n"


def test_state_dir_falls_back_when_project_and_home_unwritable(
    tmp_path, monkeypatch, reload_loci_stats
):
    """If neither <cwd>/.loci/state nor ~/.loci/state can be mkdir'd (e.g. a
    read-only checkout and read-only HOME), resolution must fall through to
    <plugin>/state so the plugin still works."""
    monkeypatch.delenv("LOCI_STATE_DIR", raising=False)
    # cwd: pre-create a FILE named ".loci" so mkdir(".loci/state") raises
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".loci").write_text("not a dir")
    monkeypatch.chdir(proj)
    # home: same trick so the second fallback also fails
    fake_home = tmp_path / "readonly-home"
    fake_home.mkdir()
    (fake_home / ".loci").write_text("not a dir")
    # Patch Path.home directly — monkeypatching $HOME alone is not enough
    # on Windows, where ntpath.expanduser consults %USERPROFILE% first.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    mod = reload_loci_stats()
    # Falls through to the plugin-scoped path
    assert mod.STATE_DIR == mod.PLUGIN_DIR / "state"


def test_helper_paths_land_under_resolved_state_dir(
    tmp_path, monkeypatch, reload_loci_stats
):
    """_stats_path / _global_stats_path / _measurements_path all join
    STATE_DIR, so overriding the env shifts every writer in lockstep."""
    monkeypatch.setenv("LOCI_STATE_DIR", str(tmp_path / "state"))
    mod = reload_loci_stats()

    # Seed a minimal project-context so the helpers resolve
    ctx = tmp_path / "state" / "ctx.json"
    ctx.parent.mkdir(parents=True, exist_ok=True)
    ctx.write_text(json.dumps({
        "cwd_hash": "abc123",
        "branch_slug": "main",
        "project_root": str(tmp_path),
    }))
    monkeypatch.setenv("LOCI_CONTEXT_FILE", str(ctx))

    stats = mod._stats_path()
    meas = mod._measurements_path()
    glb = mod._global_stats_path()

    assert stats == tmp_path / "state" / "loci-stats-abc123-main.json"
    assert meas == tmp_path / "state" / "loci-measurements-abc123-main.jsonl"
    assert glb == tmp_path / "state" / "loci-stats-global.json"
    # Sanity: no paths leak into the versioned plugin dir
    for p in (stats, meas, glb):
        assert mod.PLUGIN_DIR not in p.parents


# ---------------------------------------------------------------------------
# Lint check: anything in lib/ or hooks/ that still writes under
# <plugin>/state/ is either an expected fallback or a regression.
# ---------------------------------------------------------------------------

# Patterns we consider "writes to the versioned state dir". Word boundaries
# keep the two Python patterns mutually exclusive so a single `_PLUGIN_DIR`
# occurrence is counted once, not twice.
_WRITE_PATTERNS = [
    re.compile(r'(?<![_\w])PLUGIN_DIR\s*/\s*"state"'),   # Python: PLUGIN_DIR / "state"
    re.compile(r'_PLUGIN_DIR\s*/\s*"state"'),             # Python module-private variant
    re.compile(r'\$\{?PLUGIN_DIR\}?/state'),              # Bash: $PLUGIN_DIR/state
]

# Files allowed to reference the legacy path (fallbacks + doc hints).
# Each entry: (file_relative_to_plugin_root, max_allowed_hits).
_ALLOWLIST = {
    # session-init.sh: fallback STATE_DIR when $HOME unwritable
    "hooks/session-init.sh": 1,
    # generate-summary.sh: fallback STATE_DIR when $HOME unwritable
    "lib/generate-summary.sh": 1,
    # asm_analyze.py: fallback loci-paths.json lookup for older installs
    "lib/asm_analyze.py": 1,
    # loci_stats.py: fallback return in _resolve_state_dir when HOME unwritable
    "lib/loci_stats.py": 1,
}


def _scan_file(rel: str) -> int:
    path = PLUGIN_ROOT / rel
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8")
    return sum(len(p.findall(text)) for p in _WRITE_PATTERNS)


def test_no_unapproved_plugin_state_dir_writes():
    """Every file in lib/ and hooks/ either avoids <plugin>/state entirely
    or is on the allowlist with an exact expected hit count."""
    offenders: list[tuple[str, int, int]] = []  # (file, found, allowed)
    for base in ("lib", "hooks"):
        for path in (PLUGIN_ROOT / base).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".sh"}:
                continue
            # Normalise to forward slashes so the allowlist lookup works on
            # Windows (relative_to() preserves the OS separator).
            rel = path.relative_to(PLUGIN_ROOT).as_posix()
            hits = _scan_file(rel)
            allowed = _ALLOWLIST.get(rel, 0)
            if hits > allowed:
                offenders.append((rel, hits, allowed))
    assert not offenders, (
        "Files referencing <plugin>/state beyond allowlist — new state writes "
        "must go through LOCI_STATE_DIR / ~/.loci/state to survive upgrades:\n"
        + "\n".join(f"  {f}: found {h}, allowed {a}" for f, h, a in offenders)
    )
