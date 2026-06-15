"""End-to-end CLI test for memmap map-file warning surface.

Regression context (bug #4 in the original report): when a user passed
``--map-file <path>`` to ``asm-analyze memmap`` and the parser couldn't
extract memory regions (file missing, unreadable, or unrecognized
format), the CLI exited 0 with ``memory_regions: null`` and no warning
of any kind. The agent had no signal to surface the failure, and a CI
gate keyed off exit code would pass a degraded report.

This test invokes the CLI exactly as the memory-report skill would,
through ``lib/asm_analyze.py`` as a subprocess, and asserts the JSON
payload carries a structured warning for each user-supplied failure
mode. The no-map-file control case must still yield an empty warnings
list so a downstream consumer can distinguish the cases.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _run_cli(args, plugin_root: Path) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(plugin_root / "lib" / "asm_analyze.py"), *args]
    env = {**os.environ, "_LOCI_BOOTSTRAP": "1", "PYTHONIOENCODING": "utf-8"}
    env.pop("LOCI_DEBUG", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)


def _elf(heap_fixtures_dir) -> Path:
    if heap_fixtures_dir is None:
        pytest.skip("heap fixtures not available")
    elf = heap_fixtures_dir / "heap_alloc_aarch64.elf"
    if not elf.is_file():
        pytest.skip("heap_alloc_aarch64.elf fixture missing")
    return elf


def test_memmap_no_map_file_emits_empty_warnings_list(
    plugin_root, heap_fixtures_dir, require_asmslicer,
):
    """Control case: no ``--map-file`` argument. ``memory_regions`` is
    null (always was) AND ``warnings`` is an empty list. The empty list
    lets a consumer tell "no map file requested" from "map file
    requested but failed to parse"."""
    elf = _elf(heap_fixtures_dir)
    proc = _run_cli(["memmap", "--elf-path", str(elf)], plugin_root)
    assert proc.returncode == 0, (proc.stdout[-500:], proc.stderr[-500:])
    payload = json.loads(proc.stdout)
    assert payload["memory_regions"] is None
    assert payload["warnings"] == []


def test_memmap_missing_map_file_emits_not_found_warning(
    plugin_root, heap_fixtures_dir, require_asmslicer, tmp_path,
):
    """Path was supplied but the file doesn't exist. The CLI must emit
    a MAP_FILE_NOT_FOUND warning so the skill prompt can surface it."""
    elf = _elf(heap_fixtures_dir)
    missing = tmp_path / "does_not_exist.map"
    proc = _run_cli(
        ["memmap", "--elf-path", str(elf), "--map-file", str(missing)],
        plugin_root,
    )
    assert proc.returncode == 0, (proc.stdout[-500:], proc.stderr[-500:])
    payload = json.loads(proc.stdout)
    assert payload["memory_regions"] is None
    warnings = payload["warnings"]
    assert len(warnings) >= 1
    map_warnings = [w for w in warnings if w["code"] == "MAP_FILE_NOT_FOUND"]
    assert len(map_warnings) == 1
    w = map_warnings[0]
    assert w["path"] == str(missing)
    assert "detail" in w


def test_memmap_unrecognized_format_emits_format_warning(
    plugin_root, heap_fixtures_dir, require_asmslicer, tmp_path,
):
    """The headline failure the bug report describes: a TI ARM Clang
    map file (or any text file that isn't one of the supported formats)
    must result in memory_regions=null AND a structured warning so the
    agent and skill template have a signal to surface to the user."""
    elf = _elf(heap_fixtures_dir)
    # Synthesize a TI-Clang-style header: banner of '*' separators
    # followed by the linker identifier line. The parser's first-line
    # diagnostic must skip the separator and report the identifier.
    ti_like = tmp_path / "basic_ble.map"
    ti_like.write_text(
        "*" * 80 + "\n"
        "            TI ARM Clang Linker PC v2.1.3                      \n"
        + "*" * 80 + "\n"
        ">> Linked Thu Mar 12 10:04:13 2026\n"
        "\n"
        "MEMORY CONFIGURATION\n"  # uppercase: GCC parser expects mixed-case
    )
    proc = _run_cli(
        ["memmap", "--elf-path", str(elf), "--map-file", str(ti_like)],
        plugin_root,
    )
    assert proc.returncode == 0, (proc.stdout[-500:], proc.stderr[-500:])
    payload = json.loads(proc.stdout)
    assert payload["memory_regions"] is None
    map_warnings = [
        w for w in payload["warnings"] if w["code"] == "MAP_FORMAT_UNRECOGNIZED"
    ]
    assert len(map_warnings) == 1
    w = map_warnings[0]
    assert w["path"] == str(ti_like)
    # The diagnostic must surface the meaningful header line, not the
    # asterisk separator. This is what tells the engineer *which*
    # format the file actually is.
    assert "TI ARM Clang Linker PC v2.1.3" in w["detail"]


def test_memmap_warnings_have_structured_shape(
    plugin_root, heap_fixtures_dir, require_asmslicer, tmp_path,
):
    """Pin the JSON contract: every entry in ``warnings`` is a dict
    with at least ``code`` and ``detail`` string fields. This is what
    the memory-report skill template renders."""
    elf = _elf(heap_fixtures_dir)
    bogus = tmp_path / "README.md"
    bogus.write_text("# not a map file\n")
    proc = _run_cli(
        ["memmap", "--elf-path", str(elf), "--map-file", str(bogus)],
        plugin_root,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert isinstance(payload["warnings"], list)
    assert payload["warnings"], "expected at least one warning"
    for w in payload["warnings"]:
        assert isinstance(w, dict), w
        assert isinstance(w.get("code"), str) and w["code"], w
        assert isinstance(w.get("detail"), str), w
