"""Regression tests for the SessionStart hook's upgrade-survival behaviour.

A plugin version bump (0.1.65 -> 0.1.66) MUST NOT flip a previously-working
LOCI install into "first-time setup running". Concretely:

  * The venv lives at ~/.loci/venv (shared across versions), not under the
    versioned plugin cache dir. A fresh plugin cache dir with no .venv must
    still find the shared venv and emit a "ready" banner.
  * The setup marker is keyed by the sha256 fingerprint of requirements.txt,
    not by the plugin version string. Unchanged requirements across an upgrade
    keep the marker valid -> no rebuild, no "first-time setup" message.
  * A genuinely-fresh install (no shared venv anywhere) still drops into the
    setup path. The behaviour is gated on whether the shared venv exists and
    its marker matches, NOT on whether the plugin dir is new.

These tests stage a fake plugin dir + fake HOME and drive ``session-init.sh``
end-to-end with the setup-helper functions stubbed to fail loudly, so any
hidden venv rebuild attempt fails the test instead of silently masking the
upgrade-survival regression.
"""

from __future__ import annotations

import hashlib
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
    """Locate a bash that understands `/c/Users/...` style paths.

    On Windows, `bash` may resolve to the WSL launcher in WindowsApps, which
    treats `/c/...` as a literal POSIX path (not a Windows drive letter).
    Prefer Git Bash explicitly when available; fall back to PATH otherwise.
    """
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
    """Convert a Path to a form Git Bash on Windows can resolve.

    `C:\\foo\\bar` -> `/c/foo/bar`. On non-Windows the input is already
    POSIX-style and as_posix() is a no-op.
    """
    s = p.as_posix()  # forward slashes
    # Map drive-letter prefix `C:/foo` to `/c/foo` for MSYS/Cygwin bash.
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    if m:
        return f"/{m.group(1).lower()}/{m.group(2)}"
    return s


@pytest.fixture
def staged_plugin(tmp_path: Path) -> dict:
    """Stage a fake plugin dir + fake HOME and return the bag of paths.

    Copies the real session-init.sh, find-venv-python.sh, lib/, requirements.txt
    and a minimal plugin.json into a throwaway PLUGIN_DIR. The fake HOME is
    where ~/.loci/{venv,state} will land — keeping the test out of the user's
    real state.
    """
    plugin = tmp_path / "plugin"
    home = tmp_path / "home"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "lib").mkdir(parents=True)
    (plugin / ".claude-plugin").mkdir(parents=True)
    home.mkdir()

    shutil.copy(PLUGIN_ROOT / "hooks" / "session-init.sh", plugin / "hooks")
    shutil.copy(PLUGIN_ROOT / "hooks" / "find-venv-python.sh", plugin / "hooks")
    shutil.copy(PLUGIN_ROOT / "requirements.txt", plugin)
    # Copy lib/ — session-init.sh sources lib/loci_log.sh and lib/detect-project.sh.
    for src in (PLUGIN_ROOT / "lib").iterdir():
        if src.is_file():
            shutil.copy(src, plugin / "lib")
        elif src.is_dir() and src.name not in {"__pycache__"}:
            shutil.copytree(src, plugin / "lib" / src.name)

    (plugin / ".claude-plugin" / "plugin.json").write_text(
        '{"name":"loci","version":"0.1.68"}'
    )

    return {"plugin": plugin, "home": home}


def _make_venv_stub(home: Path) -> Path:
    """Create a fake ~/.loci/venv with a python stub that satisfies the
    health probes inside session-init.sh (Python version 3.12, asmslicer
    importable). Returns the venv dir."""
    venv = home / ".loci" / "venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    py = bin_dir / "python"
    py.write_text(dedent("""\
        #!/usr/bin/env bash
        # Stub Python: satisfies the venv health checks in session-init.sh
        case "$*" in
            *import\\ sys*) printf '3.12\\n' ;;
            *loci.service.asmslicer*) exit 0 ;;
            *) exit 0 ;;
        esac
    """))
    py.chmod(0o755)
    return venv


def _requirements_fingerprint() -> str:
    """Mirror _requirements_fingerprint in session-init.sh."""
    h = hashlib.sha256((PLUGIN_ROOT / "requirements.txt").read_bytes()).hexdigest()
    return h[:16]


def _run_session_init(plugin: Path, home: Path, stub_setup: bool = True) -> subprocess.CompletedProcess:
    """Run session-init.sh in the staged sandbox.

    When stub_setup=True, _install_uv and _setup_venv are overridden via an
    exported-function trick to fail loudly if reached. Any "FAIL:" line in
    stderr means a code path tried to rebuild the venv when it shouldn't have.
    """
    env = {
        **os.environ,
        # HOME must be POSIX-form so the bash script's ${HOME}/.loci/venv
        # expansion lands on a path bash can actually create.
        "HOME": _to_bash_path(home),
        "LOCI_STATE_DIR": _to_bash_path(home / ".loci" / "state"),
    }
    # Pop any inherited LOCI_VENV_DIR that might point at the real venv
    env.pop("LOCI_VENV_DIR", None)

    if stub_setup:
        # Patch the script in-place: right after the first-time-setup banner
        # line, bail out with a sentinel. If the upgrade-survival path is
        # working, this branch is never reached and stderr stays clean.
        # If the regression returns, stderr will contain "FAIL: setup path
        # reached" and the test fails with a clear message.
        path = plugin / "hooks" / "session-init.sh"
        script = path.read_text(encoding="utf-8")
        anchor = "    printf 'LOCI: first-time setup (v%s)...\\n' \"$ver\"\n"
        if anchor not in script:
            raise RuntimeError("session-init.sh layout changed — adjust patch anchor")
        injection = (
            anchor
            + '    printf "FAIL: setup path reached but venv should already be ready\\n" >&2\n'
            + '    rm -rf "$lock" 2>/dev/null; trap - EXIT; return 0\n'
        )
        path.write_text(script.replace(anchor, injection, 1), encoding="utf-8")

    return subprocess.run(
        [_find_bash(), _to_bash_path(plugin / "hooks" / "session-init.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(not _has_bash(), reason="bash not available")
def test_upgrade_with_existing_shared_venv_reports_ready(staged_plugin):
    """Bug repro: a plugin version bump with a healthy shared venv must emit
    'asm-analyze command: ...' (ready), NOT 'asm-analyze: unavailable
    (first-time setup running)'.

    Pre-fix behaviour: session-init only checked ${PLUGIN_DIR}/.venv, so an
    upgrade to a fresh plugin cache dir always missed the existing venv and
    re-ran setup. Post-fix: shared venv at ~/.loci/venv is discovered, the
    requirements-fingerprint marker matches, and the banner reports ready.
    """
    plugin = staged_plugin["plugin"]
    home = staged_plugin["home"]

    # Stage the "already-set-up" state from a previous plugin version
    venv = _make_venv_stub(home)
    (venv / ".setup-complete").write_text(_requirements_fingerprint())

    res = _run_session_init(plugin, home, stub_setup=True)

    # The banner is on stdout (the hookSpecificOutput JSON).
    assert "first-time setup running" not in res.stdout, (
        "Upgrade-survival regressed: banner advertises first-time setup "
        "even though a healthy shared venv exists.\n--- stdout ---\n"
        + res.stdout[:2000]
    )
    assert "asm-analyze command:" in res.stdout, (
        "Expected asm-analyze command in banner; got:\n" + res.stdout[:2000]
    )
    # The setup-path stub must never have fired.
    assert "FAIL: setup path reached" not in res.stderr, (
        "session-init.sh tried to rebuild the venv on a clean upgrade:\n"
        + res.stderr[:2000]
    )


@pytest.mark.skipif(not _has_bash(), reason="bash not available")
def test_truly_fresh_install_still_runs_setup(staged_plugin):
    """The fix must not skip setup when no venv exists anywhere. A truly
    first-time install (no shared venv, no per-version venv) must still
    enter the setup path so the user actually gets a working LOCI."""
    plugin = staged_plugin["plugin"]
    home = staged_plugin["home"]
    # Note: NOT creating ~/.loci/venv — this is a clean machine.

    res = _run_session_init(plugin, home, stub_setup=True)

    # The setup-path stub MUST fire here — otherwise we silently skipped
    # initial install, which would also be a regression.
    assert "FAIL: setup path reached" in res.stderr, (
        "Fresh-install path didn't enter setup — would leave the user without "
        "a venv and silently skip the bootstrap.\n--- stderr ---\n"
        + res.stderr[:2000]
    )


@pytest.mark.skipif(not _has_bash(), reason="bash not available")
def test_marker_mismatch_triggers_rebuild(staged_plugin):
    """If the shared venv exists but its marker doesn't match the current
    requirements fingerprint (e.g., requirements.txt changed in the new
    plugin version), setup must run to rebuild the venv.

    This guards against the inverse bug: blindly reusing an out-of-date venv
    after a real dependency bump."""
    plugin = staged_plugin["plugin"]
    home = staged_plugin["home"]

    venv = _make_venv_stub(home)
    # Marker with a different fingerprint — simulates pre-bump requirements
    (venv / ".setup-complete").write_text("0000000000000000")

    res = _run_session_init(plugin, home, stub_setup=True)

    assert "FAIL: setup path reached" in res.stderr, (
        "Marker mismatch must trigger rebuild, but setup path didn't fire.\n"
        "--- stderr ---\n" + res.stderr[:2000]
    )


@pytest.mark.skipif(not _has_bash(), reason="bash not available")
def test_loci_venv_dir_exported_for_downstream_hooks(staged_plugin):
    """session-init.sh must export LOCI_VENV_DIR so PreToolUse/PostToolUse/Stop
    hooks and the asm_analyze.py / build_metadata.py auto-bootstrap can find
    the same venv without re-deriving the path."""
    plugin = staged_plugin["plugin"]
    home = staged_plugin["home"]
    venv = _make_venv_stub(home)
    (venv / ".setup-complete").write_text(_requirements_fingerprint())

    # Inspect the running env via a small probe at the end of the script.
    # We can't intercept the exported env across processes, but we can grep
    # the script source for the export.
    script = (plugin / "hooks" / "session-init.sh").read_text()
    assert 'export LOCI_VENV_DIR=' in script, (
        "session-init.sh must export LOCI_VENV_DIR — downstream hooks rely on "
        "it to resolve the shared venv without duplicating the location logic."
    )


def test_find_venv_python_helper_probes_shared_first():
    """The helper that PreToolUse / PostToolUse / Stop hooks use to locate
    the venv python MUST check ~/.loci/venv before the per-version plugin-dir
    fallback. Otherwise a plugin upgrade leaves those hooks pointing at the
    stale per-version venv (or finding nothing at all)."""
    helper = (PLUGIN_ROOT / "hooks" / "find-venv-python.sh").read_text()
    home_pos = helper.find('${HOME}/.loci/venv')
    plugin_pos = helper.find('${PLUGIN_DIR}/.venv')
    assert home_pos != -1, "find-venv-python.sh must probe ~/.loci/venv"
    assert plugin_pos != -1, (
        "find-venv-python.sh must keep the per-version fallback for backward "
        "compatibility with venvs from older plugin installs"
    )
    assert home_pos < plugin_pos, (
        "Shared venv must be probed before per-version fallback so upgrades "
        "use the shared venv, not the stale per-version copy."
    )
