#!/usr/bin/env python3
"""LOCI asm-analyze CLI — local ELF binary analysis tool.

Wraps the asm-analyze library to provide ELF binary analysis from the
command line. Intended to be called by agents via the local CLI.

Subcommands:
  slice-elf          — Full ELF analysis (asm, symbols, blocks, segments, callgraph, elfinfo)
  extract-assembly   — Per-function assembly in timing-backend-ready format
  extract-symbols    — Symbol map from an ELF
  diff-elfs          — Compare two ELF binaries
  blocks-to-timing   — Transform blocks CSV to timing-backend CSV format
  stack-depth        — Worst-case stack depth analysis via call-graph traversal
"""

# ---------------------------------------------------------------------------
# Venv auto-bootstrap: re-launch under the plugin's .venv Python if needed.
# This runs before any non-stdlib imports so it works with system Python.
#
# `from __future__ import annotations` defers annotation evaluation to strings
# (PEP 563). Without it, the module's `str | None` / `tuple[int, int]` hints
# would require Python 3.10+ at parse time — a user running this with RHEL 9
# system python3 (3.9) or macOS default python3 (3.9 on 13/14) would hit a
# SyntaxError before the bootstrap could re-exec into the 3.12 venv.
# ---------------------------------------------------------------------------
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Force UTF-8 for all Python I/O (and any child Python processes we spawn).
# Windows consoles default to cp1252, which can't encode the Unicode
# characters the plugin prints (→, ·, µ, ↳, ⚠, ✗, ✅). This env var is the
# one reliable cross-platform knob: it survives subprocess.run without an
# explicit env=, it applies before any reconfigure() call can run, and it
# propagates through the venv-bootstrap re-exec below.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_REQUIRED_PY = (3, 12)


def _venv_dir_candidates() -> list[Path]:
    """Return candidate venv directories in resolution priority order.

    The shared ~/.loci/venv location (default since the upgrade-survival fix)
    is preferred so plugin upgrades reuse the existing venv. The per-version
    plugin-dir location is kept as a fallback so venvs created by older
    plugin versions keep working until the user's next session bootstrap.
    Also honours LOCI_VENV_DIR if session-init.sh exported it.
    """
    cands: list[Path] = []
    env_dir = os.environ.get("LOCI_VENV_DIR")
    if env_dir:
        cands.append(Path(env_dir))
    cands.append(Path.home() / ".loci" / "venv")
    cands.append(_PLUGIN_DIR / ".venv")
    return cands


def _file_key(f: Path) -> str:
    """Extract the logical key from a slicer output filename.

    The slicer may produce filenames like 'asm.csv' (simple) or
    'foo.o~bar.o.diff.csv' (compound). Path.stem only strips the last
    extension, giving 'foo.o~bar.o.diff' instead of 'diff'. This helper
    returns the last dot-segment of the stem so the key is always the
    logical output type (e.g. 'diff', 'asm', 'symmap').
    """
    stem = f.stem  # strips .csv
    last_dot = stem.rfind(".")
    if last_dot != -1:
        return stem[last_dot + 1:]
    return stem


def _venv_python_version(vp: str) -> tuple[int, int] | None:
    """Return (major, minor) for the given Python executable, or None on error."""
    try:
        out = subprocess.check_output(
            [vp, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            stderr=subprocess.DEVNULL, timeout=10,
        ).decode().strip()
        maj, minor = out.split(".", 1)
        return int(maj), int(minor)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _find_venv_python(require_version: tuple[int, int] | None = _REQUIRED_PY):
    """Return path to the venv Python if it matches require_version, else None.

    Walks every candidate venv directory (shared, then per-version fallback)
    so a plugin upgrade reuses the existing shared venv without rebuilding.
    Pass require_version=None to accept any version (used only for error messages).
    """
    for vdir in _venv_dir_candidates():
        for p in [
            vdir / "Scripts" / "python.exe",  # Windows
            vdir / "bin" / "python3",          # Unix
            vdir / "bin" / "python",           # Unix fallback
        ]:
            if not p.is_file():
                continue
            if require_version is None:
                return str(p)
            ver = _venv_python_version(str(p))
            if ver == require_version:
                return str(p)
    return None


def _in_venv():
    """Check whether we are already running inside any LOCI venv candidate."""
    try:
        sp = str(Path(sys.prefix).resolve())
    except (OSError, ValueError):
        return False
    for vdir in _venv_dir_candidates():
        try:
            if sp.startswith(str(vdir.resolve())):
                return True
        except (OSError, ValueError):
            continue
    return False


# Guard: only attempt re-exec once (env var prevents infinite loop).
# Version check is belt-and-suspenders: setup.sh + session-init.sh already enforce
# 3.12, but any stale venv (plugin downgrade, partial install, user tinkering)
# would otherwise be silently used. Refuse the wrong version and rebuild.
_current_py = (sys.version_info.major, sys.version_info.minor)
_wrong_version_in_venv = _in_venv() and _current_py != _REQUIRED_PY

if _wrong_version_in_venv and not os.environ.get("_LOCI_BOOTSTRAP"):
    # We're running under the venv's own wrong-version Python. Rebuilding the
    # venv from here would fail on Windows (python.exe is locked by our own
    # process) and is fragile everywhere. Exit clean and let session-init.sh
    # rebuild on the next Claude Code session start.
    required_str = f"{_REQUIRED_PY[0]}.{_REQUIRED_PY[1]}"
    actual_str = f"{_current_py[0]}.{_current_py[1]}"
    print(json.dumps({
        "error": (
            f"LOCI venv is running Python {actual_str} but requires {required_str}. "
            "Restart Claude Code — session-init will rebuild the venv automatically. "
            f"Or run: bash {_PLUGIN_DIR}/setup/setup.sh"
        ),
    }))
    sys.exit(1)

if not _in_venv() and not os.environ.get("_LOCI_BOOTSTRAP"):
    os.environ["_LOCI_BOOTSTRAP"] = "1"
    vp = _find_venv_python()
    if vp is None:
        # Venv missing OR wrong Python version — run setup.sh, which rebuilds
        # the venv with Python 3.12 when it detects a mismatch. Safe here
        # because we are NOT running under the venv ourselves.
        setup = _PLUGIN_DIR / "setup" / "setup.sh"
        if setup.is_file():
            subprocess.run(
                ["bash", str(setup)],
                capture_output=True, timeout=300,
            )
            vp = _find_venv_python()
    if vp:
        result = subprocess.run([vp] + sys.argv)
        sys.exit(result.returncode)
    else:
        any_vp = _find_venv_python(require_version=None)
        actual = _venv_python_version(any_vp) if any_vp else None
        actual_str = f"{actual[0]}.{actual[1]}" if actual else "unavailable"
        required_str = f"{_REQUIRED_PY[0]}.{_REQUIRED_PY[1]}"
        print(json.dumps({
            "error": (
                f"LOCI requires Python {required_str} but the venv has "
                f"Python {actual_str}. Rebuild with: "
                f"bash {_PLUGIN_DIR}/setup/setup.sh"
            ),
        }))
        sys.exit(1)

# ---------------------------------------------------------------------------
# Normal imports (now guaranteed to run inside the venv)
# ---------------------------------------------------------------------------
import argparse
import contextlib
import csv
import io
import logging
import re
import tempfile
import traceback

# Shared file-only logger (no-op unless LOCI_LOG_LEVEL is set).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import loci_log  # noqa: E402

# Ensure Unicode output works on Windows consoles (cp1252 can't encode → etc.)
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower().replace("-", "") != "utf8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Prepend the cxxfilt_dir detected by setup.sh (written to loci-paths.json).
# This ensures the GNU c++filt (which supports -r) is found before any
# system-installed version that may not (e.g. Apple's /usr/bin/c++filt).
#
# State lives under LOCI_STATE_DIR (set by session-init.sh) or, by default,
# in the project-local <cwd>/.loci/state so all LOCI artifacts stay with the
# project being analyzed. asm-analyze always runs with cwd = the project root.
_STATE_DIR_FOR_PATHS = Path(
    os.environ.get("LOCI_STATE_DIR", Path.cwd() / ".loci" / "state")
)
_PATHS_FILE = _STATE_DIR_FOR_PATHS / "loci-paths.json"
if not _PATHS_FILE.exists():
    # Fallback to the plugin-dir legacy location (older installs)
    _PATHS_FILE = _PLUGIN_DIR / "state" / "loci-paths.json"
try:
    _loci_paths = json.loads(_PATHS_FILE.read_text(encoding="utf-8"))
    _cxxfilt_dir = _loci_paths.get("cxxfilt_dir", "")
    if _cxxfilt_dir and _cxxfilt_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _cxxfilt_dir + os.pathsep + os.environ.get("PATH", "")
except (OSError, json.JSONDecodeError):
    pass

import pandas as pd


# ---------------------------------------------------------------------------
# Stdout hygiene during analysis.
# ---------------------------------------------------------------------------
# asm-analyze's contract is: stdout is a single JSON document. Callers parse
# it with json.load(...). But asmslicer and its dependencies (pyvex, angr,
# pyelftools, capstone) can emit diagnostic prints to stdout from deep
# library code we don't own — e.g. asmslicer.py's DWARF mismatch debug
# block, or third-party libs printing on unusual ELFs. When those bytes
# land ahead of our JSON, `json.load` fails at position 0 with a cryptic
# "Expecting value" error and the user has no way to diagnose it.
#
# We guard against this by capturing stdout during the analysis and only
# restoring it to emit the final JSON document. Anything captured is
# forwarded to stderr when LOCI_DEBUG=1 so users can still inspect it
# when something goes wrong.
#
# LOCI_DEBUG=1 — also enables DEBUG-level Python logging to stderr.

class _WarningBuffer(logging.Handler):
    """Capture WARNING-level records (only — ERROR/CRITICAL still hit stderr).

    Why: every byte written to stderr can land in the JSON stream when a
    caller uses `2>&1 |` to pipe to jq. Asmslicer routinely emits non-fatal
    warnings (e.g. "cxxfilt unavailable, using mangled names") that are
    informational, not actionable. Buffering them here keeps stderr clean
    by default, while still surfacing the messages in the JSON output's
    "warnings" field so callers that care can read them programmatically.

    The buffer is capped (MAX_RECORDS) so a pathological ELF that fires a
    per-symbol warning thousands of times can't OOM the process or bloat
    the JSON output. Overflow is summarized as a single trailing entry.
    """

    MAX_RECORDS = 100

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []
        self.suppressed: int = 0
        # Filter: capture WARNING only, let ERROR/CRITICAL flow to stderr.
        self.addFilter(lambda r: r.levelno < logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if len(self.records) >= self.MAX_RECORDS:
                self.suppressed += 1
                return
            self.records.append(self.format(record))
        except Exception:  # never raise from a log handler
            pass

    def snapshot(self) -> list[str]:
        """Return captured messages plus an overflow notice if any were dropped."""
        out = list(self.records)
        if self.suppressed:
            out.append(f"(... {self.suppressed} more warnings suppressed)")
        return out


_warning_buffer: _WarningBuffer | None = None


def _configure_logging() -> None:
    """Route logging so the JSON-on-stdout contract survives `2>&1 |`.

    Default mode (no LOCI_DEBUG):
      - WARNING records → captured by _warning_buffer (never to stderr).
        Surfaced via the "warnings" field on the JSON output.
      - ERROR / CRITICAL → straight to stderr (real problems must be visible).
      - INFO / DEBUG → suppressed via logging.disable() (asmslicer's child
        loggers re-set their own level otherwise).

    LOCI_DEBUG=1:
      - All levels (DEBUG and up) go to stderr. Use this for interactive
        debugging — output may corrupt JSON consumers, that is the trade.
    """
    global _warning_buffer
    debug = bool(os.environ.get("LOCI_DEBUG"))

    root = logging.getLogger()
    root.handlers = []  # wipe handlers from any prior call

    if debug:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.DEBUG)
        root.handlers = [handler]
        root.setLevel(logging.DEBUG)
        logging.disable(logging.NOTSET)
        _warning_buffer = None
        return

    # Default: ERROR+ to stderr, WARNING captured for JSON inclusion.
    err_handler = logging.StreamHandler(sys.stderr)
    err_handler.setLevel(logging.ERROR)
    _warning_buffer = _WarningBuffer()
    root.handlers = [err_handler, _warning_buffer]
    root.setLevel(logging.WARNING)
    logging.disable(logging.INFO)


def _drain_warnings() -> list[str]:
    """Return captured WARNING messages and clear the buffer.

    Includes a trailing "N more warnings suppressed" entry when the
    bounded buffer overflowed during the run.
    """
    if _warning_buffer is None:
        return []
    msgs = _warning_buffer.snapshot()
    _warning_buffer.records.clear()
    _warning_buffer.suppressed = 0
    return msgs


# Configure logging at import time so any logger created by downstream
# imports (asmslicer, pyelftools) inherits the muted level immediately.
# main() calls _configure_logging() again, which is idempotent.
_configure_logging()


@contextlib.contextmanager
def _capture_stdout():
    """Route any writes to sys.stdout into a buffer for the duration.

    Yields the buffer; contents can be re-emitted to stderr after the
    context exits (see LOCI_DEBUG handling in main()).
    """
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = real_stdout


def _diagnose_elf(elf_path: str) -> str:
    """Inspect an ELF file and return an actionable diagnostic string.

    Only called on error paths when asm-analyze produces no output.
    Checks for the most common causes in order of likelihood:
    1. Empty object file (code compiled out by preprocessor conditionals)
    2. Missing DWARF debug sections (compiled without -g)
    3. DWARF present but still failing (ELF format / architecture issue)
    """
    try:
        from elftools.elf.elffile import ELFFile
        from elftools.elf.sections import SymbolTableSection
    except ImportError:
        return (
            "Could not inspect the ELF file (pyelftools not installed). "
            "Ensure the file was compiled with -g and that all required "
            "preprocessor defines (-D flags) are present."
        )
    try:
        with open(elf_path, "rb") as f:
            elf = ELFFile(f)

            # Check 1: Does the file contain any code at all?
            text_size = 0
            for section in elf.iter_sections():
                if section.name == ".text":
                    text_size = section.data_size
                    break
            func_count = 0
            for section in elf.iter_sections():
                if isinstance(section, SymbolTableSection):
                    for sym in section.iter_symbols():
                        if sym.entry.st_info.type == "STT_FUNC":
                            func_count += 1

            if text_size == 0 and func_count == 0:
                return (
                    "The object file contains no code (empty .text section, "
                    "0 functions). This usually means the source was compiled "
                    "out by preprocessor conditionals (#if / #ifdef). Check "
                    "that the standalone compilation includes all required "
                    "-D defines from the project build system."
                )

            # Check 2: Is DWARF debug info present?
            if not elf.has_dwarf_info(strict=True):
                if func_count > 0:
                    return (
                        f"The object file has {func_count} function(s) but no "
                        f"DWARF debug sections. Compile with -g to emit debug "
                        f"info (e.g. <compiler> -g <flags> -c <source> -o "
                        f"<output>)."
                    )
                return (
                    "No DWARF debug sections found. Ensure the file was "
                    "compiled with -g."
                )

            # Check 3: DWARF is present — report version for diagnostics
            dwarf_info = elf.get_dwarf_info()
            versions = set()
            for cu in dwarf_info.iter_CUs():
                versions.add(cu.header.get("version", 0))
            ver_str = ", ".join(str(v) for v in sorted(versions)) if versions else "unknown"
            return (
                f"DWARF version {ver_str} present, {func_count} function(s) "
                f"found. The failure may be caused by an unsupported ELF "
                f"format or architecture."
            )
    except Exception as exc:
        return (
            f"Could not inspect the ELF file ({type(exc).__name__}: {exc}). "
            f"Ensure the file was compiled with -g and that all required "
            f"preprocessor defines (-D flags) are present."
        )

# ---------------------------------------------------------------------------
# Architecture mapping to timing backend
# ---------------------------------------------------------------------------
ARCH_TO_TIMING = {
    "aarch64": "aarch64",
    "cortexm": "armv7e-m",
    "tricore": "tc399",
}
TIMING_TO_ARCH = {v: k for k, v in ARCH_TO_TIMING.items()}

# Accepted architecture aliases (user input → canonical plugin name).
# Canonical names are the plugin-internal set used by stack_depth and
# cfg_formatter: aarch64, cortexm, tricore.
ARCH_ALIASES = {
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "cortex-a53": "aarch64",
    "armv8-a": "aarch64",
    "cortexm": "cortexm",
    "cortex-m": "cortexm",
    "cortex-m0": "cortexm",
    "cortex-m0+": "cortexm",
    "cortex-m4": "cortexm",
    "armv7e-m": "cortexm",
    "armv6-m": "cortexm",
    "armv7-m": "cortexm",
    "armv8-m.main": "cortexm",
    "armv8-m.base": "cortexm",
    "cortex-m33": "cortexm",
    "cortex-m23": "cortexm",
    "cortex-m55": "cortexm",
    "cortex-m85": "cortexm",
    "thumb": "cortexm",
    "tricore": "tricore",
    "tc399": "tricore",
    "tc3xx": "tricore",
}

# Canonical plugin arch → name expected by loci-service-asmslicer's
# set_elf_architecture() validation (asmslicer.py Architecture StrEnum).
# A mismatch here causes asmslicer to raise UNSUPPORTED_FEATURE_ERROR; that
# is why the canonical "cortexm" must be remapped to "armcortexm" before
# the asmslicer boundary.
ARCH_TO_ASMSLICER = {
    "aarch64": "aarch64",
    "cortexm": "armcortexm",
    "tricore": "tricore",
}

# User input → timing backend name, preserving Cortex-M sub-arch distinction.
# resolve_arch() + timing_arch() would collapse "armv6-m" to "cortexm" and
# then to "armv7e-m" — wrong for Cortex-M0/M0+/M23 targets. This map is
# consulted first so the user's sub-arch survives to the timing backend.
ARCH_ALIAS_TO_TIMING = {
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "cortex-a53": "aarch64",
    "armv8-a": "aarch64",
    "armv6-m": "armv6-m",
    "armv7-m": "armv7e-m",
    "armv7e-m": "armv7e-m",
    "armv8-m.main": "armv7e-m",
    "armv8-m.base": "armv6-m",
    "cortex-m": "armv7e-m",
    "cortex-m0": "armv6-m",
    "cortex-m0+": "armv6-m",
    "cortex-m4": "armv7e-m",
    "cortex-m23": "armv6-m",
    "cortex-m33": "armv7e-m",
    "cortex-m55": "armv7e-m",
    "cortex-m85": "armv7e-m",
    "cortexm": "armv7e-m",
    "thumb": "armv7e-m",
    "tricore": "tc399",
    "tc399": "tc399",
    "tc3xx": "tc399",
}


def resolve_arch(arch_input: str | None) -> str | None:
    """Resolve a user-provided architecture string to canonical plugin name."""
    if arch_input is None:
        return None
    return ARCH_ALIASES.get(arch_input.lower().strip())


def asmslicer_arch(canonical: str | None) -> str | None:
    """Translate canonical plugin arch to the name asmslicer accepts."""
    if canonical is None:
        return None
    return ARCH_TO_ASMSLICER.get(canonical, canonical)


def timing_arch(arch: str) -> str:
    """Map canonical architecture name to timing backend name."""
    return ARCH_TO_TIMING.get(arch, arch)


def resolve_timing_arch(arch_input: str | None) -> str | None:
    """Resolve a user-supplied arch string directly to a timing backend name.

    Preserves Cortex-M sub-arch (armv6-m vs armv7e-m) when the caller is
    specific, unlike timing_arch(resolve_arch(x)) which always collapses to
    armv7e-m via the cortexm canonical.
    """
    if arch_input is None:
        return None
    return ARCH_ALIAS_TO_TIMING.get(arch_input.lower().strip())


# ---------------------------------------------------------------------------
# Output type mappings
# ---------------------------------------------------------------------------
VALID_OUTPUT_TYPES = {"asm", "symbols", "blocks", "segments", "callgraph", "elfinfo"}

# Map output_type names to asm-analyze output file stems
OUTPUT_TYPE_TO_STEM = {
    "asm": "asm",
    "symbols": "symmap",
    "blocks": "blocks",
    "segments": "segments",
    "callgraph": "callgraph",
    "elfinfo": "elfinfo",
}

# Map output_type names to asm-analyze process() keyword argument names
OUTPUT_TYPE_TO_KWARG = {
    "asm": "out_asm_file",
    "symbols": "out_sym_map_file",
    "blocks": "blocks_file_path",
    "segments": "output_file_path",
    "callgraph": "out_plot_file",
    "elfinfo": "out_elfinfo_file",
}


# ---------------------------------------------------------------------------
# asm-analyze wrapper
# ---------------------------------------------------------------------------
def run_analysis(elf_path: str, architecture: str | None = None) -> dict:
    """Run asm-analyze process() and return {arch, files} with raw output content.

    Returns dict with:
        arch: detected/specified architecture (canonical name)
        files: dict mapping output type to file content string
    """
    from loci.service.asmslicer import asmslicer

    elf = Path(elf_path)
    if not elf.is_file():
        raise FileNotFoundError(f"ELF file not found: {elf_path}")

    with tempfile.TemporaryDirectory(prefix="loci-asm-analyze-") as tmpdir:
        kwargs = {
            "elf_file_path": str(elf),
            "log": logging.getLogger("loci.asm-analyze"),
        }
        provided_arch = asmslicer_arch(architecture) if architecture else None
        if provided_arch:
            kwargs["architecture"] = provided_arch

        # Set individual output file paths for all output types
        for otype, kwarg in OUTPUT_TYPE_TO_KWARG.items():
            stem = OUTPUT_TYPE_TO_STEM[otype]
            kwargs[kwarg] = os.path.join(tmpdir, f"{stem}.csv")

        try:
            asmslicer.process(**kwargs)
        except Exception as exc:
            # Defensive: an out-of-spec asmslicer wheel (e.g. an older
            # build that predates `Architecture.ARM_CORTEXM`, or a
            # Python-version mismatch where StrEnum semantics differ)
            # may reject the value asmslicer_arch() produced even though
            # the wheel could have auto-detected it from EM_ARM. Retry
            # once without `architecture` so cortexm callers don't hit
            # an env-specific failure they can't act on.
            msg = str(exc)
            unsupported = (
                provided_arch
                and "unsupported" in msg.lower()
                and provided_arch in msg
            )
            if not unsupported:
                raise
            logger = logging.getLogger("loci.asm-analyze")
            logger.warning(
                "asmslicer rejected architecture=%r (%s); retrying with "
                "auto-detection. Update your venv to pick up a current "
                "loci-service-asmslicer wheel.",
                provided_arch, msg,
            )
            kwargs.pop("architecture", None)
            asmslicer.process(**kwargs)

        # Read all generated output files
        files = {}
        for f in Path(tmpdir).iterdir():
            if f.is_file():
                files[_file_key(f)] = f.read_text(encoding="utf-8")

        # Detect architecture from elfinfo if not specified
        detected_arch = architecture
        if not detected_arch and "elfinfo" in files:
            elfinfo = files["elfinfo"]
            for arch_key in ARCH_TO_TIMING:
                if arch_key.lower() in elfinfo.lower():
                    detected_arch = arch_key
                    break

        return {"arch": detected_arch, "files": files}


# ---------------------------------------------------------------------------
# Assembly parsing helpers
# ---------------------------------------------------------------------------
FUNC_HEADER_RE = re.compile(r"^([0-9a-fA-F]+)\s+<(.+?)>:\s*$", re.MULTILINE)


def parse_functions_from_asm(asm_text: str) -> dict:
    """Parse objdump-style assembly into per-function blocks.

    Returns dict: {function_name: {"assembly": str, "start_address": str, "instructions": list}}
    """
    functions = {}
    headers = list(FUNC_HEADER_RE.finditer(asm_text))

    for i, match in enumerate(headers):
        addr = match.group(1)
        name = match.group(2)
        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(asm_text)
        body = asm_text[start:end].rstrip("\n")

        # Filter out empty function bodies
        lines = [ln for ln in body.split("\n") if ln.strip()]
        if not lines:
            continue

        functions[name] = {
            "assembly": "\n".join(lines),
            "start_address": f"0x{addr}",
            "instructions": lines,
        }

    return functions


def parse_symbols(symmap_text: str) -> list:
    """Parse symmap CSV into list of symbol dicts."""
    symbols = []
    reader = csv.DictReader(io.StringIO(symmap_text))
    for row in reader:
        symbols.append({
            "name": row.get("name", ""),
            "long_name": row.get("long_name", ""),
            "start_address": row.get("start_address", ""),
            "size": int(row.get("size", 0)) if row.get("size", "").isdigit() else 0,
            "namespace": row.get("namespace", ""),
        })
    return symbols


def match_function(query: str, sym_name: str, sym_long_name: str) -> bool:
    """Check if a query matches a symbol's name or long_name.

    Supports exact match and prefix match (ignoring parameter lists).
    """
    if query == sym_name or query == sym_long_name:
        return True
    # Match demangled name without params: "calculate" matches "calculate(int)"
    if sym_long_name.startswith(query + "("):
        return True
    # Match short name without params
    if sym_name.startswith(query + "("):
        return True
    return False


def chunk_timing_csv(csv_text: str, max_chars: int = 90000) -> list[str]:
    """Split timing CSV into chunks that fit within MCP token limits.

    Each chunk keeps the header row. max_chars defaults to 90 000
    (~30 000 tokens at ~3 chars/token).

    The assembly_code field typically contains embedded newlines (one per
    instruction). Naive line-based splitting would cut between a field's
    opening and closing quote, producing malformed CSV that the server's
    pandas.read_csv cannot parse. This splits on CSV record boundaries
    and re-serializes each chunk with the header intact.

    Returns an empty list when the input has no data rows — skills iterate
    the result and MUST NOT send a header-only chunk to the MCP (the server
    would reply with a misleading "All functions exceeded token limit"
    message because the empty-input branch shares that return path).
    """
    reader = csv.reader(io.StringIO(csv_text))
    try:
        rows = list(reader)
    except csv.Error:
        # Unparseable input — return as-is; caller deals with it.
        return [csv_text]
    if not rows:
        return []

    header = rows[0]
    data_rows = rows[1:]
    if not data_rows:
        return []

    def serialize(records: list[list[str]]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        for r in records:
            writer.writerow(r)
        return buf.getvalue()

    def row_size(row: list[str]) -> int:
        buf = io.StringIO()
        csv.writer(buf).writerow(row)
        return len(buf.getvalue())

    header_size = row_size(header)

    chunks: list[str] = []
    current_rows: list[list[str]] = []
    current_size = header_size

    for row in data_rows:
        rsize = row_size(row)
        # Start a new chunk when adding this row would exceed the cap.
        # A single oversize row still goes in its own chunk — the server's
        # per-row token-limit check will skip it with a warning rather than
        # fail the whole request.
        if current_rows and current_size + rsize > max_chars:
            chunks.append(serialize(current_rows))
            current_rows = []
            current_size = header_size
        current_rows.append(row)
        current_size += rsize

    if current_rows:
        chunks.append(serialize(current_rows))
    return chunks


def parse_blocks_to_timing_csv(blocks_text: str,
                                functions: list[str] | None = None) -> str:
    """Parse blocks CSV and produce timing-format CSV.

    Blocks CSV columns: s1.name, s1.long_name, r.from_addr, r.to_addr,
                        r.asm, db.block_ids, r.src_location

    Output CSV: function_name, assembly_code
        function_name = {s1.long_name}_{r.from_addr}
        assembly_code = r.asm (as-is)
    """
    reader = csv.DictReader(io.StringIO(blocks_text))

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["function_name", "assembly_code"])

    for row in reader:
        long_name = row.get("s1.long_name", "")
        from_addr = row.get("r.from_addr", "")
        asm = row.get("r.asm", "")

        if not long_name or not asm:
            continue

        # Filter by function names if specified
        if functions:
            short_name = row.get("s1.name", "")
            if not any(match_function(f, short_name, long_name)
                       for f in functions):
                continue

        function_name = f"{long_name}_{from_addr}"
        writer.writerow([function_name, asm])

    return csv_buf.getvalue()


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------
def slice_elf(elf_path: str, architecture: str | None = None,
              output_types: list[str] | None = None,
              filter_functions: bool = False) -> dict:
    output_types = output_types or ["asm", "symbols"]

    # Validate output_types
    invalid = set(output_types) - VALID_OUTPUT_TYPES
    if invalid:
        return {"error": f"Invalid output_types: {sorted(invalid)}. Valid: {sorted(VALID_OUTPUT_TYPES)}"}

    arch = resolve_arch(architecture)
    user_timing = resolve_timing_arch(architecture)
    result = run_analysis(elf_path, arch)
    detected_arch = result["arch"]
    files = result["files"]

    output = {}
    for otype in output_types:
        stem = OUTPUT_TYPE_TO_STEM.get(otype, otype)
        content = files.get(stem)
        if content is None:
            if otype == "asm":
                output["asm_diagnostic"] = _diagnose_elf(elf_path)
            output[otype] = None
            continue

        if otype == "asm":
            funcs = parse_functions_from_asm(content)
            if filter_functions:
                funcs = {
                    k: v for k, v in funcs.items()
                    if not k.startswith("_") or k.startswith("_Z")
                }
            output[otype] = {
                fname: {
                    "assembly": fdata["assembly"],
                    "start_address": fdata["start_address"],
                    "instruction_count": len(fdata["instructions"]),
                }
                for fname, fdata in funcs.items()
            }
        elif otype == "symbols":
            output[otype] = parse_symbols(content)
        else:
            # Return raw text for blocks, segments, callgraph, elfinfo
            output[otype] = content

    output["architecture"] = detected_arch
    output["timing_architecture"] = user_timing or (
        timing_arch(detected_arch) if detected_arch else None
    )

    return output


def extract_assembly(elf_path: str, functions: list[str] | None = None,
                     architecture: str | None = None,
                     blocks_file: str | None = None) -> dict:
    arch = resolve_arch(architecture)
    user_timing = resolve_timing_arch(architecture)
    result = run_analysis(elf_path, arch)
    detected_arch = result["arch"]
    files = result["files"]

    asm_text = files.get("asm")
    if not asm_text:
        diag = _diagnose_elf(elf_path)
        return {"error": f"No assembly output produced by asm-analyze. {diag}"}

    all_funcs = parse_functions_from_asm(asm_text)

    # Build symbol lookup for name matching
    symmap_text = files.get("symmap", "")
    symbols = parse_symbols(symmap_text) if symmap_text else []

    # Build a mapping from asm function name to symbol info
    sym_lookup = {}
    for sym in symbols:
        sym_lookup[sym["name"]] = sym
        if sym["long_name"]:
            sym_lookup[sym["long_name"]] = sym

    # Match requested functions (or all functions if no filter specified)
    if functions is None:
        # No filter: extract all functions
        matched = all_funcs.copy()
    else:
        # Filter by requested function names
        matched = {}
        for query in functions:
            # Try direct match in asm functions first
            if query in all_funcs:
                matched[query] = all_funcs[query]
                continue

            # Try matching via symbol names
            found = False
            for asm_name, asm_data in all_funcs.items():
                # Check against symbol lookup
                sym = sym_lookup.get(asm_name, {})
                sym_name = sym.get("name", asm_name) if sym else asm_name
                sym_long = sym.get("long_name", "") if sym else ""
                if match_function(query, sym_name, sym_long):
                    matched[query] = asm_data
                    found = True
                    break
                # Also try direct asm_name match
                if match_function(query, asm_name, asm_name):
                    matched[query] = asm_data
                    found = True
                    break

            if not found:
                matched[query] = {"error": f"Function '{query}' not found in ELF"}

    # Write blocks CSV to file if requested
    blocks_text = files.get("blocks", "")
    if blocks_file and blocks_text:
        Path(blocks_file).write_text(blocks_text, encoding="utf-8")

    # Build output
    functions_out = {}
    csv_rows = []
    for fname, fdata in matched.items():
        if "error" in fdata:
            functions_out[fname] = fdata
            continue

        asm = fdata["assembly"]
        instruction_count = len(fdata["instructions"])
        # Calculate size from instruction count (approximate: varies by arch)
        size = instruction_count * 4  # ARM/AArch64 = 4 bytes, Tricore = 4 bytes

        functions_out[fname] = {
            "assembly": asm,
            "start_address": fdata["start_address"],
            "size": size,
            "instruction_count": instruction_count,
        }
        # CSV row: quote the assembly for proper CSV formatting
        csv_rows.append((fname, asm))

    # Build timing CSV — prefer per-block granularity when blocks available
    if blocks_text:
        timing_csv = parse_blocks_to_timing_csv(blocks_text, functions)
    else:
        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow(["function_name", "assembly_code"])
        for fname, asm in csv_rows:
            writer.writerow([fname, asm])
        timing_csv = csv_buf.getvalue()

    cfg_text = get_cfg_text(detected_arch, files, functions)

    output = {
        "architecture": detected_arch,
        "timing_architecture": user_timing or (
            timing_arch(detected_arch) if detected_arch else None
        ),
        "functions": functions_out,
        "timing_csv": timing_csv,
        "timing_csv_chunks": chunk_timing_csv(timing_csv),
        "control_flow_graph": cfg_text,
    }
    if blocks_file and blocks_text:
        output["blocks_file"] = blocks_file

    return output


def extract_symbols(elf_path: str, architecture: str | None = None) -> dict:
    arch = resolve_arch(architecture)
    user_timing = resolve_timing_arch(architecture)
    result = run_analysis(elf_path, arch)
    files = result["files"]

    symmap_text = files.get("symmap")
    if not symmap_text:
        diag = _diagnose_elf(elf_path)
        return {"error": f"No symbol map output produced by asm-analyze. {diag}"}

    symbols = parse_symbols(symmap_text)

    return {
        "architecture": result["arch"],
        "timing_architecture": user_timing or (
            timing_arch(result["arch"]) if result["arch"] else None
        ),
        "symbols": symbols,
    }


def diff_elfs(elf_path: str, comparing_elf_path: str,
              architecture: str | None = None) -> dict:
    from loci.service.asmslicer import asmslicer

    arch = resolve_arch(architecture)

    # Validate both files exist
    if not Path(elf_path).is_file():
        return {"error": f"Base ELF not found: {elf_path}"}
    if not Path(comparing_elf_path).is_file():
        return {"error": f"Comparing ELF not found: {comparing_elf_path}"}

    with tempfile.TemporaryDirectory(prefix="loci-asm-analyze-diff-") as tmpdir:
        diff_kwargs = {
            "elf_file_path": elf_path,
            "comparing_elf_file_path": comparing_elf_path,
            "compare_out": tmpdir,
            "log": logging.getLogger("loci.asm-analyze"),
        }
        if arch:
            diff_kwargs["architecture"] = asmslicer_arch(arch)

        asmslicer.process(**diff_kwargs)

        # Read diff output
        files = {}
        for f in Path(tmpdir).iterdir():
            if f.is_file():
                files[_file_key(f)] = f.read_text(encoding="utf-8")

    # Parse diff CSV if available
    diff_text = files.get("diff", "")
    diff_entries = []
    summary = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}

    if diff_text:
        reader = csv.DictReader(io.StringIO(diff_text))
        for row in reader:
            status = row.get("status", "").lower()
            entry = {
                "status": status,
                "symbol": row.get("symbol", ""),
                "stt_type": row.get("stt_type", ""),
                "similarity_ratio": float(row.get("similarity_ratio", 0))
                if row.get("similarity_ratio", "").replace(".", "").isdigit()
                else 0.0,
                "reason": row.get("reason", ""),
            }
            diff_entries.append(entry)
            if status in summary:
                summary[status] += 1

    return {
        "diff": diff_entries,
        "summary": summary,
    }


def blocks_to_timing(blocks_file: str,
                     functions: list[str] | None = None) -> None:
    """Read blocks CSV and print timing-format CSV to stdout."""
    blocks_path = Path(blocks_file)
    if not blocks_path.is_file():
        print(json.dumps({"error": f"Blocks file not found: {blocks_file}"}))
        sys.exit(1)

    blocks_text = blocks_path.read_text(encoding="utf-8")
    timing_csv = parse_blocks_to_timing_csv(blocks_text, functions)
    print(timing_csv, end="")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def extract_cfg(elf_path, architecture, functions):
    """Return the annotated CFG text for the requested functions.

    Previously printed directly to stdout and returned "success"; that
    wrote the CFG before main()'s capture/JSON hygiene could run, making
    it impossible to guard the stream against third-party library
    leakage. main() now emits the CFG text explicitly after capture.
    """
    arch = resolve_arch(architecture)
    result = run_analysis(elf_path, arch)
    detected_arch = result["arch"]
    files = result["files"]
    cfg_text = get_cfg_text(detected_arch, files, functions)
    return {"control_flow_graph": cfg_text}


def get_cfg_text(detected_arch, files, functions):
    blocks_text = files.get("blocks")
    string_io_object = io.StringIO(blocks_text.strip())  # strip() removes leading/trailing whitespace
    functions_list = []
    if functions is not None and type(functions) is list:
        functions_list = functions
    elif functions is not None and functions != "":
        functions_list = functions.split(",")
    # Load the data into a DataFrame
    df = pd.read_csv(string_io_object, sep=',')
    from loci.service.asmslicer.cfg_formatter import df_to_cfg_text
    cfg_text = df_to_cfg_text(
        work=df,
        functions=functions_list,
        arch=detected_arch,
    )
    return cfg_text


def memmap(elf_path: str,
           comparing_elf_path: str | None = None,
           map_file: str | None = None,
           top_n: int = 10,
           with_heap: bool = False,
           allocators_file: str | None = None) -> dict:
    """Delegate to loci-service-asmslicer's memmap module."""
    from loci.service.asmslicer.memmap import memmap as _memmap

    allocators = None
    if allocators_file:
        try:
            names: set[str] = set()
            with open(allocators_file, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    names.add(line)
            # An explicitly-provided list overrides defaults even when empty —
            # users can suppress all detection by passing an empty allocators
            # file. Previously this silently fell back to DEFAULT_ALLOCATORS.
            allocators = frozenset(names)
        except OSError as e:
            return {"error": f"failed to read allocators file: {e}"}

    return _memmap(
        elf_path=elf_path,
        comparing_elf_path=comparing_elf_path,
        map_file=map_file,
        top_n=top_n,
        with_heap=with_heap,
        allocators=allocators,
    )


def stack_depth(elf_path: str | None = None,
                asm_path: str | None = None,
                callgraph_dot_path: str | None = None,
                architecture: str | None = None,
                entry_functions: list[str] | None = None,
                stack_budget: int | None = None,
                threshold: int = 50,
                max_recursion_depth: int = 1,
                unknown_callee_size: int = 64) -> dict:
    """Run stack depth analysis via the wheel's stack_depth module.

    Two paths:
      - Full ELF (elf_path): runs full disassembly + call-graph extraction
      - Fast/incremental (asm_path): reuses existing .asm and optional .callgraph.dot files
    """
    from loci.service.asmslicer.stack_depth import (
        analyze_stack_depth as _analyze_elf,
        analyze_from_files as _analyze_files,
    )

    # The wheel's analyze_stack_depth has an inconsistent arch validator:
    # it accepts canonical "cortexm" at entry, then forwards that unchanged
    # to asmslicer.process which only accepts "armcortexm". Passing None lets
    # asmslicer auto-detect from the ELF, which produces correct stack-depth
    # output (stack frames are ISA-subset-invariant across Cortex-M variants).
    if asm_path:
        # Fast path: reuse existing asmslicer output files
        canonical = resolve_arch(architecture)
        if not canonical:
            return {"error": "Architecture is required when using --asm-path. "
                    f"Supported: {', '.join(sorted(ARCH_ALIASES.keys()))}"}
        arch = None if canonical == "cortexm" else canonical
        return _analyze_files(
            asm_path=asm_path,
            architecture=arch,
            callgraph_dot_path=callgraph_dot_path,
            entry_functions=entry_functions,
            stack_budget=stack_budget,
            threshold_pct=threshold,
            max_recursion_depth=max_recursion_depth,
            unknown_callee_size=unknown_callee_size,
        )
    elif elf_path:
        # Full ELF path
        canonical = resolve_arch(architecture)
        arch = None if canonical == "cortexm" else canonical
        return _analyze_elf(
            elf_path=elf_path,
            architecture=arch,
            entry_functions=entry_functions,
            stack_budget=stack_budget,
            threshold_pct=threshold,
            max_recursion_depth=max_recursion_depth,
            unknown_callee_size=unknown_callee_size,
        )
    else:
        return {"error": "Either --elf-path or --asm-path is required"}


def main():
    parser = argparse.ArgumentParser(
        prog="asm-analyze",
        description="LOCI asm-analyze — local ELF binary analysis tool",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # slice-elf
    p_slice = subparsers.add_parser(
        "slice-elf",
        help="Full ELF analysis (asm, symbols, blocks, segments, callgraph, elfinfo)",
    )
    p_slice.add_argument("--elf-path", required=True, help="Path to the ELF binary")
    p_slice.add_argument("--arch", default=None, help="Target architecture (auto-detected if omitted)")
    p_slice.add_argument("--output-types", default="asm,symbols",
                         help="Comma-separated output types (default: asm,symbols)")
    p_slice.add_argument("--filter-functions", action="store_true",
                         help="Filter compiler-generated functions")

    # extract-assembly
    p_extract = subparsers.add_parser(
        "extract-assembly",
        help="Per-function assembly in timing-backend-ready format",
    )
    p_extract.add_argument("--elf-path", required=True, help="Path to the ELF binary")
    p_extract.add_argument("--functions", required=False,
                           help="Comma-separated function names to extract (omit to extract all functions)")
    p_extract.add_argument("--arch", default=None, help="Target architecture (auto-detected if omitted)")
    p_extract.add_argument("--blocks", default=None, metavar="FILE",
                           help="Write basic blocks CSV to this file")

    # extract-symbols
    p_symbols = subparsers.add_parser(
        "extract-symbols",
        help="Extract symbol map from an ELF binary",
    )
    p_symbols.add_argument("--elf-path", required=True, help="Path to the ELF binary")
    p_symbols.add_argument("--arch", default=None, help="Target architecture (auto-detected if omitted)")

    # diff-elfs
    p_diff = subparsers.add_parser(
        "diff-elfs",
        help="Compare two ELF binaries",
    )
    p_diff.add_argument("--elf-path", required=True, help="Path to the base ELF binary")
    p_diff.add_argument("--comparing-elf-path", required=True, help="Path to the changed ELF binary")
    p_diff.add_argument("--arch", default=None, help="Target architecture (auto-detected if omitted)")

    # blocks-to-timing
    p_blocks = subparsers.add_parser(
        "blocks-to-timing",
        help="Transform blocks CSV to timing-backend CSV format",
    )
    p_blocks.add_argument("--blocks", required=True, metavar="FILE",
                          help="Path to blocks CSV file")
    p_blocks.add_argument("--functions", default=None,
                          help="Comma-separated function names to filter")

    # extract-cfg
    p_cfg = subparsers.add_parser(
        "extract-cfg",
        help="Extract CFG (function Control Flow Graph) map from an ELF binary",
    )
    p_cfg.add_argument("--elf-path", required=True, help="Path to the ELF binary")
    p_cfg.add_argument("--arch", default=None, help="Target architecture (auto-detected if omitted)")
    p_cfg.add_argument("--functions", required=False,
                           help="Comma-separated function names to extract (omit to extract all functions)")

    # stack-depth
    p_stack = subparsers.add_parser(
        "stack-depth",
        help="Worst-case stack depth analysis via call-graph traversal",
    )
    p_stack_input = p_stack.add_mutually_exclusive_group(required=True)
    p_stack_input.add_argument("--elf-path", default=None,
                               help="Path to a linked ELF binary (full call-graph analysis)")
    p_stack_input.add_argument("--asm-path", default=None,
                               help="Path to .asm file from asmslicer (fast incremental path)")
    p_stack.add_argument("--callgraph-dot-path", default=None,
                         help="Path to .callgraph.dot file (used with --asm-path)")
    p_stack.add_argument("--arch", default=None,
                         help="Target architecture (required with --asm-path, auto-detected with --elf-path)")
    p_stack.add_argument("--entry-functions", default=None,
                         help="Comma-separated entry-point function names (auto-detect roots if omitted)")
    p_stack.add_argument("--stack-budget", type=int, default=None,
                         help="Configured stack size in bytes (enables usage %% and verdict)")
    p_stack.add_argument("--threshold", type=int, default=50,
                         help="Max allowed usage as percentage of budget (default: 50)")
    p_stack.add_argument("--max-recursion-depth", type=int, default=1,
                         help="Bounded recursion estimate depth (default: 1)")
    p_stack.add_argument("--unknown-callee-size", type=int, default=64,
                         help="Assumed frame size in bytes for unknown/external callees (default: 64)")

    # memmap
    p_memmap = subparsers.add_parser(
        "memmap",
        help="ROM/RAM memory usage report from ELF section and symbol analysis",
    )
    p_memmap.add_argument("--elf-path", required=True, help="Path to the ELF binary or .o file")
    p_memmap.add_argument("--comparing-elf-path", default=None,
                           help="Path to a second ELF to compare against (enables delta report)")
    p_memmap.add_argument("--map-file", default=None,
                           help="Path to GCC linker map file (enables region budgets)")
    p_memmap.add_argument("--top-n", type=int, default=10,
                           help="Number of top consumers to report per category (default: 10)")
    p_memmap.add_argument("--with-heap", action="store_true",
                           help="Include heap allocation analysis (direct calls to known allocators)")
    p_memmap.add_argument("--allocators-file", default=None, metavar="FILE",
                           help="Newline-separated allocator symbol names "
                                "(overrides the built-in default catalog; '#' lines treated as comments)")

    args = parser.parse_args()
    _configure_logging()

    debug = bool(os.environ.get("LOCI_DEBUG"))
    stray = io.StringIO()

    loci_log.info("asm-analyze", f"start: cmd={args.command}")
    try:
        # blocks-to-timing streams CSV to stdout directly — don't capture it,
        # or we'd swallow the payload this subcommand is supposed to emit.
        if args.command == "blocks-to-timing":
            funcs = ([f.strip() for f in args.functions.split(",")]
                     if args.functions else None)
            blocks_to_timing(blocks_file=args.blocks, functions=funcs)
            loci_log.info("asm-analyze", f"end: cmd={args.command} rc=0")
            sys.exit(0)

        # Everything else returns a JSON-serializable dict. Capture any
        # third-party prints during the analysis so they can't corrupt the
        # JSON document we're about to emit.
        with _capture_stdout() as stray:
            if args.command == "slice-elf":
                output_types = [t.strip() for t in args.output_types.split(",")]
                result = slice_elf(
                    elf_path=args.elf_path,
                    architecture=args.arch,
                    output_types=output_types,
                    filter_functions=args.filter_functions,
                )
            elif args.command == "extract-assembly":
                funcs = ([f.strip() for f in args.functions.split(",")]
                         if args.functions else None)
                result = extract_assembly(
                    elf_path=args.elf_path,
                    functions=funcs,
                    architecture=args.arch,
                    blocks_file=args.blocks,
                )
            elif args.command == "extract-symbols":
                result = extract_symbols(
                    elf_path=args.elf_path,
                    architecture=args.arch,
                )
            elif args.command == "diff-elfs":
                result = diff_elfs(
                    elf_path=args.elf_path,
                    comparing_elf_path=args.comparing_elf_path,
                    architecture=args.arch,
                )
            elif args.command == "extract-cfg":
                result = extract_cfg(
                    elf_path=args.elf_path,
                    architecture=args.arch,
                    functions=args.functions,
                )
            elif args.command == "stack-depth":
                entry_funcs = ([f.strip() for f in args.entry_functions.split(",")]
                               if args.entry_functions else None)
                result = stack_depth(
                    elf_path=args.elf_path,
                    asm_path=args.asm_path,
                    callgraph_dot_path=args.callgraph_dot_path,
                    architecture=args.arch,
                    entry_functions=entry_funcs,
                    stack_budget=args.stack_budget,
                    threshold=args.threshold,
                    max_recursion_depth=args.max_recursion_depth,
                    unknown_callee_size=args.unknown_callee_size,
                )
            elif args.command == "memmap":
                result = memmap(
                    elf_path=args.elf_path,
                    comparing_elf_path=args.comparing_elf_path,
                    map_file=args.map_file,
                    top_n=args.top_n,
                    with_heap=args.with_heap,
                    allocators_file=args.allocators_file,
                )
            else:
                result = {"error": f"Unknown command: {args.command}"}

        # Stdout is back to the real stream. If LOCI_DEBUG was set, echo
        # anything our analysis accidentally printed so the user can see it
        # without it corrupting the JSON payload.
        if debug and stray.getvalue():
            sys.stderr.write("[LOCI_DEBUG] captured stdout from analysis:\n")
            sys.stderr.write(stray.getvalue())
            if not stray.getvalue().endswith("\n"):
                sys.stderr.write("\n")

        # Pull any captured WARNINGs out of the buffer and attach them to
        # the JSON output as a "warnings" array. This keeps stderr empty in
        # the default flow, so a caller that does `2>&1 | jq` gets clean
        # JSON instead of cxxfilt / dwarf noise prepended.
        captured_warnings = _drain_warnings()

        # extract-cfg emits raw CFG text (the skill's contract is plain
        # text, not JSON). Every other subcommand emits a JSON document.
        if args.command == "extract-cfg" and "error" not in result:
            # Captured warnings can't go in the text output, but they still
            # belong somewhere — the consumer is reading text, not JSON, so
            # stderr is safe (no JSON corruption risk on this code path).
            for msg in captured_warnings:
                sys.stderr.write(msg + "\n")
            print(result["control_flow_graph"])
            loci_log.info("asm-analyze", f"end: cmd={args.command} rc=0")
            sys.exit(0)

        if captured_warnings and isinstance(result, dict):
            # Wrap captured log-message strings as structured dicts so the
            # `warnings` field is uniformly list[dict] across all subcommands.
            # Subcommand-emitted warnings (e.g. memmap's MAP_FORMAT_UNRECOGNIZED)
            # are already structured; we just append more entries here.
            wrapped = [{"code": "RUNTIME", "detail": m} for m in captured_warnings]
            existing = result.get("warnings")
            if isinstance(existing, list):
                existing.extend(wrapped)
            else:
                result["warnings"] = wrapped

        rc = 1 if "error" in result else 0
        loci_log.info("asm-analyze", f"end: cmd={args.command} rc={rc}")
        print(json.dumps(result, indent=2))
        sys.exit(rc)

    except Exception as e:
        loci_log.error("asm-analyze",
                       f"end: cmd={args.command} ({type(e).__name__}: {e})")
        # On exception, surface any captured warnings to stderr so they
        # don't disappear silently — if something went wrong, the user
        # may need that context to diagnose.
        for msg in _drain_warnings():
            sys.stderr.write(msg + "\n")
        print(json.dumps({
            "error": str(e),
            "traceback": traceback.format_exc(),
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
