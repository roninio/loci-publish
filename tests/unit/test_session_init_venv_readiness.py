"""Regression test for the broken-venv fast-path-skip bug.

Scenario reported by an engineer after a plugin version update:
  * `~/.loci/venv` exists and runs Python 3.12 (fine).
  * `from loci.service.asmslicer import asmslicer` raises ModuleNotFoundError
    — the asmslicer install is gone (uv pip install killed mid-flight,
    antivirus quarantine, or manual cleanup).
  * The setup marker fingerprint still matches requirements.txt.
  * Session-after-session, the hook reports "asm-analyze unavailable
    (first-time setup running — restart after ~60 s)" but no setup is
    actually running. The venv stays broken indefinitely.

Root cause: `_first_time_setup`'s fast-path skip only ran `_venv_is_py312`
which verified Python version but never imported asmslicer. A venv with
intact Python and a stale-but-fingerprint-matching marker passed the skip
every time, so the slow-path repair in `_setup_venv` (which DOES check
the import and rebuilds when needed) was never reached.

Fix: rename `_venv_is_py312` → `_venv_is_ready`, add the asmslicer import
check, and use it for both the skip and the marker-clear conditions.

This test stages a venv whose `python` shim succeeds for the Python version
probe but fails the asmslicer import, and asserts the hook clears the
setup marker so the slow path runs on the next session.
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


def _stage_plugin(tmp_path: Path, asmslicer_works: bool) -> dict:
    """Stage a fake plugin + venv where the fake `python` shim either
    succeeds or fails the asmslicer import probe. Marker fingerprint always
    matches requirements.txt so the fast-path skip is otherwise eligible.

    A failing `uv` shim is also staged on PATH so that when the broken-venv
    case triggers a slow-path rebuild, the rebuild fails fast and the marker
    stays cleared — letting the test observe the marker-clear behaviour
    without the rebuild succeeding and re-writing the marker."""
    plugin = tmp_path / "plugin"
    home = tmp_path / "home"
    fakebin = tmp_path / "fakebin"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "lib").mkdir(parents=True)
    (plugin / ".claude-plugin").mkdir(parents=True)
    home.mkdir()
    fakebin.mkdir()
    uv_shim = fakebin / "uv"
    uv_shim.write_text("#!/usr/bin/env bash\nexit 1\n")
    uv_shim.chmod(0o755)

    shutil.copy(PLUGIN_ROOT / "hooks" / "session-init.sh", plugin / "hooks")
    shutil.copy(PLUGIN_ROOT / "hooks" / "find-venv-python.sh", plugin / "hooks")
    shutil.copy(PLUGIN_ROOT / "requirements.txt", plugin)
    for src in (PLUGIN_ROOT / "lib").iterdir():
        if src.is_file():
            shutil.copy(src, plugin / "lib")
        elif src.is_dir() and src.name != "__pycache__":
            shutil.copytree(src, plugin / "lib" / src.name)

    (plugin / ".claude-plugin" / "plugin.json").write_text(
        '{"name":"loci","version":"0.1.71"}'
    )

    asmslicer_exit = "0" if asmslicer_works else "1"
    venv = home / ".loci" / "venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    py = bin_dir / "python"
    py.write_text(dedent(f"""\
        #!/usr/bin/env bash
        case "$*" in
            *import\\ sys*) printf '3.12\\n' ;;
            *loci.service.asmslicer*) exit {asmslicer_exit} ;;
            *) exit 0 ;;
        esac
    """))
    py.chmod(0o755)
    fp = hashlib.sha256((PLUGIN_ROOT / "requirements.txt").read_bytes()).hexdigest()[:16]
    (venv / ".setup-complete").write_text(fp)

    # Pre-stage a valid token so the hook takes the no-mint branch — keeps
    # this test focused on the venv-readiness path only.
    token = home / ".loci" / "impact-token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(
        '{"token":"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.x",'
        '"issued_at":"2026-01-01T00:00:00+00:00"}'
    )

    return {"plugin": plugin, "home": home, "venv": venv, "fakebin": fakebin}


def _run(plugin: Path, home: Path, fakebin: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": _to_bash_path(home),
        "LOCI_STATE_DIR": _to_bash_path(home / ".loci" / "state"),
    }
    env.pop("LOCI_VENV_DIR", None)
    # Prepend a fakebin whose `uv` shim exits 1 — keeps the system PATH
    # otherwise intact (bash needs dirname/uname/jq) but blocks any real
    # `uv` from succeeding the slow-path rebuild during the test.
    env["PATH"] = _to_bash_path(fakebin) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [_find_bash(), _to_bash_path(plugin / "hooks" / "session-init.sh")],
        env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.skipif(not _has_bash(), reason="bash not available")
def test_broken_asmslicer_clears_marker(tmp_path):
    """**Regression coverage for the broken-venv fast-path bug.** A venv
    with intact Python 3.12 but broken `loci.service.asmslicer` import
    must cause the setup marker to be cleared, so the next session enters
    the slow-path rebuild instead of indefinitely passing the skip."""
    staged = _stage_plugin(tmp_path, asmslicer_works=False)
    plugin, home, venv, fakebin = (
        staged["plugin"], staged["home"], staged["venv"], staged["fakebin"],
    )
    marker = venv / ".setup-complete"
    assert marker.exists(), "fixture should pre-stage the marker"

    res = _run(plugin, home, fakebin)
    assert res.returncode == 0, res.stderr

    assert not marker.exists(), (
        "_first_time_setup must clear the stale marker when the venv is "
        "missing asmslicer — otherwise the fast-path skip will pass every "
        "subsequent session and the broken venv is never repaired. "
        "Pre-fix bug: marker stayed in place because _venv_is_py312 only "
        "checked Python version and never imported asmslicer."
    )


@pytest.mark.skipif(not _has_bash(), reason="bash not available")
def test_healthy_venv_keeps_marker_and_advertises_cmd(tmp_path):
    """Counterpoint: when the venv is genuinely healthy (Python 3.12 AND
    asmslicer importable), the fast-path skip must hold — marker preserved
    and the asm-analyze command surfaced in the SessionStart CONTEXT."""
    staged = _stage_plugin(tmp_path, asmslicer_works=True)
    plugin, home, venv, fakebin = (
        staged["plugin"], staged["home"], staged["venv"], staged["fakebin"],
    )
    marker = venv / ".setup-complete"

    res = _run(plugin, home, fakebin)
    assert res.returncode == 0, res.stderr

    assert marker.exists(), "healthy venv must not clear the marker"
    assert "asm-analyze command:" in res.stdout, (
        "Healthy venv must advertise asm-analyze in the CONTEXT block."
    )
