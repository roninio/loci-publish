"""Regression test for the one-time welcome banner surviving plugin upgrades.

Scenario reported by a user after a plugin version update:
  * LOCI is already installed, has been running fine for weeks.
  * User updates the plugin (e.g., 0.1.71 -> 0.1.72).
  * Closes Claude Code, starts it again.
  * The "LOCI is ready. Try: ..." welcome banner appears AGAIN, even though
    the user has seen it many times before.

Root cause: ``_welcome_text`` in hooks/session-init.sh stored the
``.welcome-shown`` marker at ``${PLUGIN_DIR}/.welcome-shown``, i.e. inside
the versioned plugin cache dir (``~/.claude/plugins/cache/loci/loci/X.Y.Z/``).
Every plugin upgrade lands in a fresh per-version dir with no marker, so
the banner shows again. This is the same upgrade-survival pattern that
AAD-7123 fixed for the venv (PR #164) and STATE_DIR — the marker simply
hadn't been migrated yet.

Fix: marker lives at ``${HOME}/.loci/.welcome-shown`` (shared across plugin
versions), falling back to PLUGIN_DIR only if ``~/.loci`` is unwritable.

Test strategy: stage two distinct plugin dirs and a single shared HOME,
run session-init.sh from plugin-A, confirm the banner appears once and
the shared marker is written; then run from plugin-B (simulating the
upgrade) and confirm the banner does NOT appear a second time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent


def _find_bash() -> str | None:
    if sys.platform == "win32":
        for cand in (
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ):
            if Path(cand).is_file():
                return cand
    return shutil.which("bash")


def _has_bash() -> bool:
    return _find_bash() is not None


def _to_bash_path(p: Path) -> str:
    s = p.as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    if m:
        return f"/{m.group(1).lower()}/{m.group(2)}"
    return s


def _stage_plugin_dir(parent: Path, name: str, version: str) -> Path:
    plugin = parent / name
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "lib").mkdir(parents=True)
    (plugin / ".claude-plugin").mkdir(parents=True)
    shutil.copy(PLUGIN_ROOT / "hooks" / "session-init.sh", plugin / "hooks")
    shutil.copy(PLUGIN_ROOT / "hooks" / "find-venv-python.sh", plugin / "hooks")
    shutil.copy(PLUGIN_ROOT / "requirements.txt", plugin)
    for src in (PLUGIN_ROOT / "lib").iterdir():
        if src.is_file():
            shutil.copy(src, plugin / "lib")
        elif src.is_dir() and src.name != "__pycache__":
            shutil.copytree(src, plugin / "lib" / src.name)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "loci", "version": version})
    )
    return plugin


def _stage_ready_venv(home: Path) -> Path:
    """Stage a venv whose stub python passes both the version and asmslicer
    probes — keeps _first_time_setup on the fast-path skip so the test
    exercises the welcome logic, not the setup logic."""
    venv = home / ".loci" / "venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    py = bin_dir / "python"
    py.write_text(dedent("""\
        #!/usr/bin/env bash
        case "$*" in
            *import\\ sys*) printf '3.12\\n' ;;
            *loci.service.asmslicer*) exit 0 ;;
            *) exit 0 ;;
        esac
    """))
    py.chmod(0o755)
    fp = hashlib.sha256((PLUGIN_ROOT / "requirements.txt").read_bytes()).hexdigest()[:16]
    (venv / ".setup-complete").write_text(fp)
    # Pre-stage a valid impact token so the hook takes the no-mint branch
    # and the test stays focused on the welcome behaviour.
    token = home / ".loci" / "impact-token.json"
    token.write_text(
        '{"token":"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.x",'
        '"issued_at":"2026-01-01T00:00:00+00:00"}'
    )
    return venv


def _run(plugin: Path, home: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": _to_bash_path(home),
        "LOCI_STATE_DIR": _to_bash_path(home / ".loci" / "state"),
    }
    env.pop("LOCI_VENV_DIR", None)
    return subprocess.run(
        [_find_bash(), _to_bash_path(plugin / "hooks" / "session-init.sh")],
        env=env, capture_output=True, text=True, timeout=60,
    )


def _has_welcome(stdout: str) -> bool:
    """The welcome banner shows up as a top-level ``systemMessage`` field
    in the SessionStart hook's JSON output. Absent field == no banner."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    msg = payload.get("systemMessage", "")
    return isinstance(msg, str) and "LOCI is ready." in msg


@pytest.mark.skipif(not _has_bash(), reason="bash not available")
def test_welcome_survives_plugin_version_upgrade(tmp_path):
    """**Regression coverage for the welcome-re-shown-on-upgrade bug.**
    First session in plugin-A: welcome appears (initial install).
    Second session in plugin-B (simulating a version bump that landed in
    a fresh per-version cache dir): welcome must NOT appear again."""
    home = tmp_path / "home"
    home.mkdir()
    _stage_ready_venv(home)

    plugin_a = _stage_plugin_dir(tmp_path, "plugin-a", "0.1.72")
    plugin_b = _stage_plugin_dir(tmp_path, "plugin-b", "0.1.73")

    res_a = _run(plugin_a, home)
    assert res_a.returncode == 0, res_a.stderr
    assert _has_welcome(res_a.stdout), (
        "First session must show the welcome banner.\n--- stdout ---\n"
        + res_a.stdout[:2000]
    )

    # Shared marker must have been written outside the versioned plugin dir.
    shared_marker = home / ".loci" / ".welcome-shown"
    assert shared_marker.exists(), (
        "Welcome marker must be written to ~/.loci/.welcome-shown so it "
        "survives plugin upgrades — pre-fix it landed in ${PLUGIN_DIR}/"
        ".welcome-shown and was discarded on the next version bump."
    )
    per_version_marker_a = plugin_a / ".welcome-shown"
    assert not per_version_marker_a.exists(), (
        "Welcome marker must NOT be written inside the versioned plugin "
        "dir — that's the bug we're fixing. Found stray marker at "
        f"{per_version_marker_a}."
    )

    # Simulate the user updating the plugin: a brand-new plugin dir with
    # no marker of its own, same HOME.
    res_b = _run(plugin_b, home)
    assert res_b.returncode == 0, res_b.stderr
    assert not _has_welcome(res_b.stdout), (
        "Welcome banner re-appeared after a plugin upgrade — the marker "
        "did not survive the version bump. Pre-fix bug.\n--- stdout ---\n"
        + res_b.stdout[:2000]
    )


@pytest.mark.skipif(not _has_bash(), reason="bash not available")
def test_welcome_shows_on_genuinely_fresh_install(tmp_path):
    """Counterpoint: a clean machine (no ~/.loci/.welcome-shown anywhere)
    must still see the welcome banner — the fix must not silently suppress
    the legitimate first-time UX."""
    home = tmp_path / "home"
    home.mkdir()
    _stage_ready_venv(home)
    # Ensure no pre-existing marker exists.
    marker = home / ".loci" / ".welcome-shown"
    assert not marker.exists()

    plugin = _stage_plugin_dir(tmp_path, "plugin", "0.1.73")
    res = _run(plugin, home)
    assert res.returncode == 0, res.stderr
    assert _has_welcome(res.stdout), (
        "Genuinely fresh install must show the welcome.\n--- stdout ---\n"
        + res.stdout[:2000]
    )
    assert marker.exists(), (
        "Welcome marker must be written on first show."
    )
