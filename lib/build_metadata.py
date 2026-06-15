#!/usr/bin/env python3
"""LOCI build metadata — always-known compiler/flags for preflight + post-edit.

When the project has an existing .o/.elf, the plugin has no way to know which
compiler version or flags produced it. Reusing it as .o.prev and comparing
against a freshly compiled post-edit .o gives contaminated diffs. This tool
always compiles with flags the plugin controls, records them in a .meta.json
sidecar, and verifies the post-edit rebuild used the same compiler + flags
before the diff is trusted.

Subcommands:
  compile — detect flags (or inherit via --meta-prev), invoke the compiler,
            write the .meta.json sidecar, print a human block.
  diff    — compare two .meta.json files, exit 1 on divergence, print block.
  print   — emit a formatted block for a meta file.
"""

# ---------------------------------------------------------------------------
# Venv auto-bootstrap (mirrors asm_analyze.py) — re-launch under the plugin
# venv so stdlib-only callers don't need to activate anything.
# ---------------------------------------------------------------------------
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Force UTF-8 for all Python I/O (and any child Python processes we spawn).
# Windows consoles default to cp1252, which can't encode the Unicode
# characters the plugin prints (→, ·, µ, ↳, ⚠, ✗, ✅). This env var is the
# one reliable cross-platform knob: it survives subprocess.run without an
# explicit env=, and it applies before any reconfigure() calls run.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_REQUIRED_PY = (3, 12)


def _venv_dir_candidates():
    """Mirror lib/asm_analyze.py — see that file for the rationale.

    Shared ~/.loci/venv first (default since the upgrade-survival fix), then
    the per-version plugin-dir fallback for venvs from older installs.
    """
    cands = []
    env_dir = os.environ.get("LOCI_VENV_DIR")
    if env_dir:
        cands.append(Path(env_dir))
    cands.append(Path.home() / ".loci" / "venv")
    cands.append(_PLUGIN_DIR / ".venv")
    return cands


def _venv_python_version(vp: str):
    try:
        out = subprocess.check_output(
            [vp, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            stderr=subprocess.DEVNULL, timeout=10,
        ).decode().strip()
        maj, minor = out.split(".", 1)
        return int(maj), int(minor)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _find_venv_python(require_version=_REQUIRED_PY):
    for vdir in _venv_dir_candidates():
        for p in [
            vdir / "Scripts" / "python.exe",
            vdir / "bin" / "python3",
            vdir / "bin" / "python",
        ]:
            if not p.is_file():
                continue
            if require_version is None:
                return str(p)
            if _venv_python_version(str(p)) == require_version:
                return str(p)
    return None


def _in_venv():
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


_current_py = (sys.version_info.major, sys.version_info.minor)
_wrong_version_in_venv = _in_venv() and _current_py != _REQUIRED_PY

if _wrong_version_in_venv and not os.environ.get("_LOCI_BOOTSTRAP"):
    required_str = f"{_REQUIRED_PY[0]}.{_REQUIRED_PY[1]}"
    actual_str = f"{_current_py[0]}.{_current_py[1]}"
    sys.stderr.write(
        f"LOCI build_metadata: venv runs Python {actual_str} (need {required_str}). "
        f"Restart Claude Code or run: bash {_PLUGIN_DIR}/setup/setup.sh\n"
    )
    sys.exit(1)

if not _in_venv() and not os.environ.get("_LOCI_BOOTSTRAP"):
    os.environ["_LOCI_BOOTSTRAP"] = "1"
    vp = _find_venv_python()
    if vp is None:
        setup = _PLUGIN_DIR / "setup" / "setup.sh"
        if setup.is_file():
            subprocess.run(["bash", str(setup)], capture_output=True, timeout=300)
            vp = _find_venv_python()
    if vp:
        sys.exit(subprocess.run([vp] + sys.argv).returncode)
    else:
        required_str = f"{_REQUIRED_PY[0]}.{_REQUIRED_PY[1]}"
        sys.stderr.write(
            f"LOCI build_metadata: Python {required_str} venv unavailable. "
            f"Run: bash {_PLUGIN_DIR}/setup/setup.sh\n"
        )
        sys.exit(1)

# ---------------------------------------------------------------------------
# Normal imports
# ---------------------------------------------------------------------------
import argparse
import hashlib
import json
import re
import shlex
import time
from datetime import datetime, timezone

# Make the flag_sources package importable when running this file directly.
_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import loci_log  # noqa: E402

from flag_sources import (  # noqa: E402
    AttemptRecord, DiscoveryMiss, DiscoveryResult, FlagDecision,
    LOCI_TARGET_FAMILY,
    has_include, is_include,
)
from flag_sources import (  # noqa: E402
    build_root as fs_build_root,
    compile_commands as fs_compile_commands,
    compiler_match as fs_compiler_match,
    gmake_dryrun as fs_gmake_dryrun,
    linked_elf_dwarf as fs_linked_elf_dwarf,
    makefile_regex as fs_makefile_regex,
    projectspec_xml as fs_projectspec_xml,
    same_stem_dwarf as fs_same_stem_dwarf,
    sibling_obj_dwarf as fs_sibling_obj_dwarf,
    stdlib_headers as fs_stdlib,
    user_override as fs_user_override,
)
from flag_sources.flags_normalize import (  # noqa: E402
    dedup_preserve_order, ensure_required, merge,
)
from flag_sources import (  # noqa: E402
    compiler_family, parse_producer,
)
from flag_sources.compiler_match import (  # noqa: E402
    choose_compiler_for_source,
)

# -- Backward-compat re-exports (old module surface) ------------------------
# Tests and external callers may still import these names directly. Keep
# them as thin wrappers around the new flag_sources implementations so the
# CLI and existing test suite remain stable.

ensure_required_flags = ensure_required  # legacy alias


def find_compile_commands(project_root: Path) -> Path | None:
    return fs_compile_commands.find_compile_commands(project_root)


def parse_compile_command(entry: dict) -> tuple[str, list[str]]:
    return fs_compile_commands.parse_entry(entry)


def detect_from_compile_commands(source: Path, cc_path: Path) -> tuple[str, list[str]] | None:
    try:
        data = json.loads(cc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = fs_compile_commands._find_entry_for_source(data, source)
    if entry is None:
        return None
    try:
        return fs_compile_commands.parse_entry(entry)
    except ValueError:
        return None


def detect_from_dwarf(elf_path: Path, source_stem: str) -> tuple[str, list[str]] | None:
    """Legacy helper — search for a CU whose DW_AT_name matches source_stem.
    Returns None when no producer has at least one flag (preserves old API)."""
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError:
        return None
    try:
        with open(elf_path, "rb") as f:
            elf = ELFFile(f)
            if not elf.has_dwarf_info(strict=False):
                return None
            dwarf = elf.get_dwarf_info()
            best = None
            for cu in dwarf.iter_CUs():
                top_die = cu.get_top_DIE()
                if top_die.tag != "DW_TAG_compile_unit":
                    continue
                pa = top_die.attributes.get("DW_AT_producer")
                if pa is None:
                    continue
                pv = pa.value
                producer = pv.decode("utf-8", "replace") if isinstance(pv, bytes) else str(pv)
                parsed = parse_producer(producer)
                if parsed.compiler is None or not parsed.flags:
                    continue
                na = top_die.attributes.get("DW_AT_name")
                name_str = ""
                if na is not None:
                    nv = na.value
                    name_str = nv.decode("utf-8", "replace") if isinstance(nv, bytes) else str(nv)
                if Path(name_str).stem == source_stem:
                    return (parsed.compiler, list(parsed.flags))
                if best is None:
                    best = (parsed.compiler, list(parsed.flags))
            return best
    except Exception:
        return None


def scan_makefiles_for_flags(project_root: Path, source: Path) -> list[str]:
    """Legacy helper — regex scan for -I/-isystem/-D in nearby makefiles."""
    result = fs_makefile_regex.discover(
        source, "armv7e-m", project_root, {}, None,
    )
    return list(result.flags) if result else []


def _parse_producer_flags(producer: str) -> tuple[str, list[str]] | None:
    """Legacy helper — returns (compiler, flags) or None if no flags found."""
    p = parse_producer(producer)
    if p.compiler is None or not p.flags:
        return None
    return (p.compiler, list(p.flags))


def compiler_matches_target(compiler: str, loci_target: str) -> bool:
    """Legacy helper — True if compiler family matches loci_target family."""
    target_family = LOCI_TARGET_FAMILY.get(loci_target)
    if target_family is None:
        return True
    family = compiler_family(compiler)
    if family is None:
        return False
    return family == target_family


def detect_flags(
    source: Path,
    loci_target: str,
    project_root: Path,
    context: dict | None = None,
) -> tuple[str, list[str], str]:
    """Legacy entry point — returns (compiler, flags, flag_source_label).

    The modern caller should use `detect_flags_verbose` for full provenance.
    The label is formatted for backward compat with the previous string form.
    """
    decision = detect_flags_verbose(source, loci_target, project_root, context)
    kind = decision.kind
    label: str
    if kind == "compile_commands":
        cc_path = decision.details.get("compile_commands_path", "")
        label = f"compile_commands ({cc_path})" if cc_path else "compile_commands"
    elif kind.startswith("linked-elf-dwarf") or kind.startswith("same-stem-dwarf") \
            or kind.startswith("sibling-obj-dwarf"):
        elf = decision.details.get("elf_path") or decision.details.get("obj_path") \
              or decision.details.get("donor_obj", "")
        elf_name = Path(str(elf)).name if elf else ""
        label = f"dwarf ({elf_name})" if elf_name else "dwarf"
    elif kind == "defaults" and any(
        "rejected-wrong-arch" in a.result and a.kind == "compile_commands"
        for a in decision.attempts
    ):
        rejected = next(
            a for a in decision.attempts
            if a.kind == "compile_commands" and a.result == "rejected-wrong-arch"
        )
        label = (
            f"defaults (compile_commands rejected: wrong arch; "
            f"{rejected.detail})"
        )
    elif kind == "defaults":
        label = "defaults"
    else:
        label = kind
    return decision.compiler, decision.flags, label


# Ensure Unicode output on Windows consoles
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower().replace("-", "") != "utf8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Defaults per LOCI target — terminal fallback when every cascade step
# returns nothing usable.
# ---------------------------------------------------------------------------
DEFAULT_COMPILER: dict[str, str] = {
    "aarch64":  "aarch64-linux-gnu-g++",
    "armv7e-m": "arm-none-eabi-g++",
    "armv6-m":  "arm-none-eabi-g++",
    "tc399":    "tricore-elf-g++",
}
DEFAULT_FLAGS: dict[str, list[str]] = {
    "aarch64":  ["-g", "-O2", "-march=armv8-a", "-c"],
    "armv7e-m": ["-g", "-O2", "-mcpu=cortex-m4", "-mthumb", "-c"],
    "armv6-m":  ["-g", "-O2", "-mcpu=cortex-m0plus", "-mthumb", "-c"],
    "tc399":    ["-g", "-O2", "-mcpu=tc3xx", "-c"],
}

RUST_TARGETS: dict[str, str] = {
    "aarch64":  "aarch64-unknown-linux-gnu",
    "armv7e-m": "thumbv7em-none-eabihf",
    "armv6-m":  "thumbv6m-none-eabi",
}
RUST_FLAGS_BASE: list[str] = ["--emit=obj", "-C", "debuginfo=2", "-C", "opt-level=2"]

C_EXTS = {".c"}
CXX_EXTS = {".cc", ".cpp", ".cxx", ".c++"}
RUST_EXTS = {".rs"}
COMPILABLE_EXTS = C_EXTS | CXX_EXTS | RUST_EXTS
HEADER_EXTS = {".h", ".hpp", ".hxx", ".h++"}


# ---------------------------------------------------------------------------
# Cascade definition — order matters (see plan §1).
# ---------------------------------------------------------------------------

_CASCADE: list[tuple[str, object]] = [
    ("user-override",     fs_user_override),   # step 0 — replace mode only
    ("compile_commands",  fs_compile_commands),
    ("gmake-dry-run",     fs_gmake_dryrun),
    ("sibling-obj-dwarf", fs_sibling_obj_dwarf),
    ("same-stem-dwarf",   fs_same_stem_dwarf),
    ("linked-elf-dwarf",  fs_linked_elf_dwarf),
    ("projectspec-xml",   fs_projectspec_xml),
    ("makefile-regex",    fs_makefile_regex),
]


def _make_defaults_decision(
    source: Path, loci_target: str,
    attempts: list[AttemptRecord],
    degraded_reason: str,
) -> FlagDecision:
    compiler = DEFAULT_COMPILER.get(loci_target)
    flags = DEFAULT_FLAGS.get(loci_target)
    if compiler is None or flags is None:
        raise RuntimeError(
            f"No default compiler/flags for loci_target={loci_target!r}; "
            f"supported: {sorted(DEFAULT_COMPILER.keys())}"
        )
    compiler = fs_compiler_match.choose_compiler_for_source(compiler, source)
    return FlagDecision(
        compiler=compiler,
        flags=list(flags),
        kind="defaults",
        confidence="low",
        details={"reason": degraded_reason},
        warnings=[f"fallback to defaults: {degraded_reason}"],
        attempts=attempts,
        degraded=True,
        effective_loci_target=loci_target,
    )


def _merge_partial(
    accumulator: DiscoveryResult | None,
    new: DiscoveryResult,
) -> DiscoveryResult:
    if accumulator is None:
        return new
    combined_flags = merge(accumulator.flags, new.flags)
    combined_details = dict(accumulator.details)
    combined_details[f"augmented_by_{new.kind}"] = new.details
    combined_warnings = list(accumulator.warnings) + list(new.warnings)
    compiler = accumulator.compiler
    if (not compiler or compiler == "unknown") and new.compiler and new.compiler != "unknown":
        compiler = new.compiler
    kind = accumulator.kind if accumulator.kind != "makefile-regex" else new.kind
    return DiscoveryResult(
        compiler=compiler,
        flags=combined_flags,
        kind=kind,
        confidence=accumulator.confidence,
        details=combined_details,
        warnings=combined_warnings,
        partial=False,  # caller decides
    )


def detect_flags_verbose(
    source: Path,
    loci_target: str,
    project_root: Path,
    context: dict | None,
) -> FlagDecision:
    """Walk the cascade. Return the winning decision + attempt trace."""
    if context is None:
        context = {}

    attempts: list[AttemptRecord] = []

    # Locate the build directory first — every step may need it.
    t0 = time.monotonic()
    build_dir = fs_build_root.find_build_root(source, project_root, context)
    attempts.append(AttemptRecord(
        kind="build-root-discover",
        result="accepted" if build_dir is not None else "missing",
        detail=str(build_dir) if build_dir else "no scored candidate >0",
        duration_ms=int((time.monotonic() - t0) * 1000),
    ))

    # If user override specifies build_root, honor it first.
    ov = fs_user_override.load_override(project_root)
    if ov.build_root:
        override_dir = Path(ov.build_root)
        if not override_dir.is_absolute():
            override_dir = project_root / ov.build_root
        if override_dir.is_dir():
            build_dir = override_dir
    # Expose user_override variables to gmake_dryrun
    if ov.variables:
        context = dict(context)
        context["user_override_variables"] = ov.variables

    partial_accumulator: DiscoveryResult | None = None

    for kind, mod in _CASCADE:
        t0 = time.monotonic()
        try:
            result: DiscoveryResult | DiscoveryMiss | None = mod.discover(
                source, loci_target, project_root, context, build_dir,
            )
        except Exception as exc:  # noqa: BLE001
            attempts.append(AttemptRecord(
                kind=kind, result="error", detail=str(exc)[:200],
                duration_ms=int((time.monotonic() - t0) * 1000),
            ))
            continue

        if result is None or isinstance(result, DiscoveryMiss):
            # DiscoveryMiss carries a precise sub-failure reason from the
            # discoverer; bare None falls back to the category-level string
            # in `_reason_for_missing`. Both are recorded as `missing`.
            reason = (
                result.reason if isinstance(result, DiscoveryMiss)
                else _reason_for_missing(kind, build_dir)
            )
            attempts.append(AttemptRecord(
                kind=kind, result="missing", detail=reason,
                duration_ms=int((time.monotonic() - t0) * 1000),
            ))
            continue

        # Partial sources (projectspec-xml, makefile-regex, partial DWARF)
        # contribute -I/-D without claiming arch authority. Skip arch
        # reconciliation for them — they'll merge with a later source that
        # supplies compiler + arch, or with defaults.
        is_partial_shape = result.partial or (
            not has_include(result.flags) and result.confidence != "exact"
        )

        if not is_partial_shape:
            reconciled = fs_compiler_match.reconcile_arch(
                result.compiler, result.flags, loci_target,
            )
            if not reconciled.accept:
                attempts.append(AttemptRecord(
                    kind=kind, result="rejected-wrong-arch",
                    detail=reconciled.reason,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                ))
                continue

            # Apply reconcile warnings into the result
            if reconciled.warnings:
                result.warnings.extend(reconciled.warnings)
            if reconciled.cpu_override is not None:
                result.details["cpu_override"] = reconciled.cpu_override
            effective_target = reconciled.effective_target or loci_target
        else:
            reconciled = None
            effective_target = loci_target

        if is_partial_shape:
            partial_accumulator = _merge_partial(partial_accumulator, result)
            attempts.append(AttemptRecord(
                kind=kind, result="partial",
                detail=f"kept as partial; flags={len(result.flags)}",
                duration_ms=int((time.monotonic() - t0) * 1000),
            ))
            continue

        # Accepted — merge any accumulated partials into final answer
        final = _merge_partial(partial_accumulator, result)
        attempts.append(AttemptRecord(
            kind=kind, result="accepted",
            detail=f"flags={len(final.flags)}",
            duration_ms=int((time.monotonic() - t0) * 1000),
        ))
        compiler = fs_compiler_match.choose_compiler_for_source(final.compiler, source)
        decision = FlagDecision(
            compiler=compiler,
            flags=ensure_required(dedup_preserve_order(final.flags)),
            kind=final.kind,
            confidence=final.confidence,
            details=final.details,
            warnings=final.warnings,
            attempts=attempts,
            cpu_override=reconciled.cpu_override,
            effective_loci_target=effective_target,
        )
        # Apply augment-mode user overrides on top
        augmented, applied = fs_user_override.apply_augment(
            DiscoveryResult(
                compiler=decision.compiler, flags=decision.flags,
                kind=decision.kind, confidence=decision.confidence,
                details=decision.details, warnings=decision.warnings,
                partial=False,
            ),
            source, project_root,
        )
        if applied:
            decision.compiler = augmented.compiler
            decision.flags = ensure_required(dedup_preserve_order(augmented.flags))
            decision.user_override_applied = True
            decision.details = augmented.details
        return decision

    # Cascade exhausted. Merge partials if any; else defaults.
    if partial_accumulator is not None and has_include(partial_accumulator.flags):
        # Pick a usable compiler: if the partial accumulator didn't identify
        # one (common for projectspec-xml / makefile-regex), fall back to the
        # target's default compiler.
        partial_compiler = partial_accumulator.compiler
        if not partial_compiler or partial_compiler == "unknown":
            partial_compiler = DEFAULT_COMPILER.get(loci_target, "")
        compiler = fs_compiler_match.choose_compiler_for_source(
            partial_compiler, source,
        )
        # Merge in the default arch flags so the merged result is compilable
        # (partial sources strip arch; we add it back from defaults).
        merged_flags = list(partial_accumulator.flags)
        for f in DEFAULT_FLAGS.get(loci_target, []):
            if f not in merged_flags:
                merged_flags.append(f)
        warnings = list(partial_accumulator.warnings)
        warnings.append(
            f"merged-partial: using default arch flags for {loci_target} "
            f"({partial_accumulator.kind} contributed -I/-D only)"
        )
        return FlagDecision(
            compiler=compiler,
            flags=ensure_required(dedup_preserve_order(merged_flags)),
            kind=f"merged-partial:{partial_accumulator.kind}",
            confidence=partial_accumulator.confidence,
            details=partial_accumulator.details,
            warnings=warnings,
            attempts=attempts,
            degraded=True,
            effective_loci_target=loci_target,
        )

    return _make_defaults_decision(
        source, loci_target, attempts,
        degraded_reason="no cascade source produced usable flags",
    )


def _reason_for_missing(kind: str, build_dir: Path | None) -> str:
    if kind == "compile_commands":
        return "no compile_commands.json under project_root or build_dir"
    if kind == "gmake-dry-run":
        return ("no makefile in build_dir" if build_dir else "no build_dir discovered")
    if kind == "sibling-obj-dwarf":
        return ("no .obj/.o files in build_dir" if build_dir else "no build_dir discovered")
    if kind == "same-stem-dwarf":
        return "no <stem>.obj/.o found near source or build_dir"
    if kind == "linked-elf-dwarf":
        return "no ELFs with matching DWARF found"
    if kind == "projectspec-xml":
        return "no .projectspec found with compilerBuildOptions"
    if kind == "makefile-regex":
        return "no makefile with extractable literal -I/-D"
    if kind == "user-override":
        return ".loci-build/flags.json absent or augment-only"
    return ""


# ---------------------------------------------------------------------------
# Doomed-compile static guard
# ---------------------------------------------------------------------------

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')


def _read_includes(source: Path, limit: int = 200) -> list[str]:
    try:
        with open(source, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= limit:
                    break
                m = _INCLUDE_RE.match(line)
                if m:
                    lines.append(m.group(1))
            return lines
    except OSError:
        return []


def _include_paths_from_flags(flags: list[str]) -> list[Path]:
    """Extract concrete directories from -I / -isystem flags."""
    out: list[Path] = []
    it = iter(flags)
    for f in it:
        if f.startswith("-I"):
            path = f[2:]
            if path:
                out.append(Path(path))
        elif f == "-isystem":
            try:
                nxt = next(it)
            except StopIteration:
                break
            out.append(Path(nxt))
        elif f.startswith("-isystem"):
            path = f[len("-isystem"):].lstrip("=")
            if path:
                out.append(Path(path))
    return out


def _header_resolves(
    header: str,
    include_paths: list[Path],
    build_dir: Path | None,
    source: Path,
) -> bool:
    # Source-relative lookup for quoted includes
    candidates = [source.parent] + include_paths
    if build_dir is not None:
        candidates.append(build_dir)
    for base in candidates:
        try:
            if (base / header).is_file():
                return True
        except OSError:
            continue
    return False


def _check_doomed_compile(
    source: Path,
    decision: FlagDecision,
    build_dir: Path | None,
) -> str | None:
    """Return an error payload string if compile is guaranteed to fail,
    else None."""
    includes = _read_includes(source)
    non_stdlib = []
    for h in includes:
        if fs_stdlib.is_stdlib(h):
            continue
        if fs_stdlib.is_generated(h) and build_dir is not None:
            continue
        non_stdlib.append(h)

    if not non_stdlib:
        return None

    inc_paths = _include_paths_from_flags(decision.flags)
    unresolved: list[str] = []
    checked = 0
    for h in non_stdlib:
        checked += 1
        if not _header_resolves(h, inc_paths, build_dir, source):
            unresolved.append(h)
        if checked >= 5:
            break

    # Fail fast only when EVERY checked non-stdlib include is unresolved AND
    # we had at least 2 to check (avoid flapping on single-header files).
    if not unresolved:
        return None
    if checked >= 2 and len(unresolved) == checked:
        return _format_insufficient_error(
            source, unresolved, decision, build_dir,
        )
    # Attach a warning but let the compile proceed
    decision.warnings.append(
        f"unresolved_includes: {len(unresolved)}/{checked} "
        f"first-checked non-stdlib headers not found under -I paths"
    )
    return None


# Matches "header not found" stderr messages from clang/tiarmclang, gcc,
# and MSVC. Used to gate _diagnose_missing_include_dirs — we only blame
# missing -I dirs when the compiler actually complained about a header.
_HEADER_NOT_FOUND_RE = re.compile(
    r"""(?xi)
    (?:
        fatal\ error:\s*['"<]([^'">]+)['">]\s+file\s+not\s+found
        | ['"<]([^'">]+\.(?:h|hh|hpp|hxx|inc|rs))['">].*?no\s+such\s+file
        | ([^\s:'"<>]+\.(?:h|hh|hpp|hxx|inc)):\s*no\s+such\s+file
        | cannot\s+open\s+(?:source\s+)?(?:file|include\s+file)\s*:?\s*['"<]([^'">]+)['">]
    )
    """,
)


def _diagnose_missing_include_dirs(
    stderr_text: str,
    flags: list[str],
) -> str | None:
    """When `stderr_text` reports a missing-header error, stat every
    include directory in `flags` and return a formatted diagnostic block
    listing those that don't exist on this host. Returns None when the
    stderr doesn't look like a header-not-found failure, or when every
    include dir resolves.

    This exists to self-diagnose the common project-side misconfig where
    a Makefile resolves a variable (e.g. FREERTOS_INSTALL_DIR) to a
    placeholder path that doesn't exist on the user's machine — the
    gmake-dry-run source faithfully emits those -I flags, the compile
    fails with an inscrutable "FreeRTOS.h not found", and without this
    diagnostic the user has to puzzle out the root cause by hand.
    """
    if not _HEADER_NOT_FOUND_RE.search(stderr_text):
        return None
    inc_paths = _include_paths_from_flags(flags)
    if not inc_paths:
        return None

    missing: list[Path] = []
    seen: set[str] = set()
    for p in inc_paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not p.is_dir():
                missing.append(p)
        except OSError:
            missing.append(p)

    if not missing:
        return None

    total = len(seen)
    noun = "directory" if total == 1 else "directories"
    lines = [
        "",
        f"LOCI diagnostic: {len(missing)} of {total} include "
        f"{noun} referenced by -I / -isystem do not exist on this host:",
    ]
    for p in missing[:12]:
        lines.append(f"  - {p}")
    if len(missing) > 12:
        lines.append(f"  ... and {len(missing) - 12} more")
    lines.extend([
        "",
        "Likely cause: a build-system variable resolved to a path that "
        + "isn't installed on this host (SDK/toolchain variables often "
        + "default to a template path or another developer's layout). The "
        + "flag source faithfully emitted these -I flags — the paths "
        + "themselves are the problem, not LOCI's discovery.",
        "",
        "Fix: point the variable at a real path on this host and re-run. "
        + "Or drop `.loci-build/flags.json` in the project root with a "
        + "working -I, e.g.:",
        "",
        "  {",
        '    "flags": [',
        '      "-I/path/to/real/sdk/include",',
        '      "-I/path/to/real/sdk/portable"',
        "    ]",
        "  }",
        "",
        "In augment mode (the default) these flags merge on top of the "
        + "cascade's winner; set `\"mode\": \"replace\"` to bypass the "
        + "cascade entirely.",
    ])
    return "\n".join(lines)


def _format_insufficient_error(
    source: Path,
    unresolved: list[str],
    decision: FlagDecision,
    build_dir: Path | None,
) -> str:
    inc_paths = _include_paths_from_flags(decision.flags)
    lines = [
        "error: LOCI cannot reliably compile "
        f"{_rel(source)} — flag discovery was insufficient.",
        "",
        f"  Edited source:            {_rel(source)}",
    ]
    for h in unresolved:
        lines.append(f"  Unresolved #include:      {h!r} (not found under any -I path)")
    lines.append(f"  Include paths discovered: {len(inc_paths)}")
    lines.append(f"  Build dir:                {build_dir if build_dir else 'not discovered'}")
    lines.append("  Flag source chain tried:")
    for a in decision.attempts:
        tag = f"[{a.kind}]"
        lines.append(
            f"    {tag:<22} {a.result:<8}  "
            f"({a.detail})"
        )
    lines.extend([
        "",
        "  How to fix (pick one):",
        "    1. Run `bear -- make` (or CMake with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON)",
        "       once to emit compile_commands.json. LOCI will reuse it forever.",
        "    2. Install GNU Make (or put CCS gmake.exe on PATH) so LOCI can",
        "       invoke `make --dry-run` against the project's existing makefile.",
        "    3. Tell LOCI where the build dir is — create .loci-build/flags.json:",
        "         {",
        "           \"build_root\": \"<path relative to project root>\"",
        "         }",
        "    4. Inject flags directly via the LOCI_EXTRA_CFLAGS env var:",
        "         export LOCI_EXTRA_CFLAGS='-I/path/to/sdk/include -DFOO ...'",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def source_hash(source: Path) -> str:
    try:
        h = hashlib.sha256()
        h.update(source.read_bytes())
        return "sha256:" + h.hexdigest()[:16]
    except OSError:
        return "unknown"


def compiler_version(compiler: str) -> str:
    try:
        out = subprocess.check_output(
            [compiler, "--version"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        return out.splitlines()[0].strip() if out.strip() else "unknown"
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"


def build_output_path(source: Path, loci_target: str, project_root: Path) -> Path:
    return project_root / ".loci-build" / loci_target / f"{source.stem}.o"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _shell_quote_flags(flags: list[str]) -> str:
    return " ".join(shlex.quote(f) for f in flags)


def format_metadata_block(meta: dict) -> str:
    """Human-readable LOCI · build block printed to stdout on --verbose."""
    src = Path(meta.get("source_file", "?"))
    out = Path(meta.get("output", "?"))
    compiler_line = meta.get("compiler", "?")
    ver = meta.get("compiler_version", "")
    if ver and ver != "unknown":
        compiler_line = f"{compiler_line}  ({ver})"
    lines = [
        "─── LOCI · build ─────────────────────────",
        f"  phase:     {meta.get('phase', '?')}",
        f"  source:    {_rel(src)}",
        f"  compiler:  {compiler_line}",
        f"  flags:     {_shell_quote_flags(meta.get('flags', []))}",
        f"  target:    {meta.get('loci_target', '?')}",
        f"  output:    {_rel(out)}",
        f"  flag src:  {meta.get('flag_source', '?')}",
    ]
    v2 = meta.get("flag_source_v2") or {}
    if v2:
        conf = v2.get("confidence", "")
        lines[-1] = f"  flag src:  {meta.get('flag_source', '?')} (confidence: {conf})"
        details = v2.get("details") or {}
        if "build_dir" in details:
            lines.append(f"    build dir: {details['build_dir']}")
        if "target" in details:
            lines.append(f"    donor obj: {details['target']}")
        cpu_o = v2.get("cpu_override")
        if cpu_o:
            lines.append(
                f"    cpu override: session={cpu_o.get('session_target')} "
                f"→ discovered={cpu_o.get('discovered_target')} "
                f"(-mcpu={cpu_o.get('discovered_cpu')})"
            )
        warnings = v2.get("warnings") or []
        if warnings:
            lines.append(f"  warnings:  {warnings[0]}")
            for w in warnings[1:]:
                lines.append(f"             {w}")
    lines.append("──────────────────────────────────────────")
    return "\n".join(lines)


def _flags_diff(prev: list[str], curr: list[str]) -> tuple[list[str], list[str]]:
    prev_set = list(prev)
    curr_set = list(curr)
    removed = [f for f in prev_set if f not in curr_set]
    added = [f for f in curr_set if f not in prev_set]
    return removed, added


def diff_metas(prev: dict, curr: dict) -> list[str]:
    out: list[str] = []
    for key in ("compiler", "compiler_version", "loci_target", "architecture"):
        if prev.get(key) != curr.get(key):
            out.append(f"  {key:14} {prev.get(key)!r} → {curr.get(key)!r}")
    removed, added = _flags_diff(prev.get("flags", []), curr.get("flags", []))
    if removed or added:
        parts: list[str] = []
        if removed:
            parts.append("removed " + " ".join(shlex.quote(f) for f in removed))
        if added:
            parts.append("added " + " ".join(shlex.quote(f) for f in added))
        out.append(f"  {'flags':14} {'; '.join(parts)}")
    # Compare flag_source_v2.kind when both sides have it. A kind change
    # is a regression EXCEPT when either side used the "inherited" path —
    # the legitimate post-edit flow inherits from prev so the kind tag
    # may read "inherited" on curr while prev has the real kind.
    def _norm_kind(m: dict) -> str | None:
        v2 = m.get("flag_source_v2") or {}
        kind = v2.get("kind")
        # Older/stale metas occasionally store the full v1 flag_source string
        # ("inherited from X.meta.json.prev") in v2.kind. Treat any kind that
        # *starts with* "inherited" as the canonical short form so the inherit
        # carve-out below applies and we don't false-positive a "regression".
        if isinstance(kind, str) and kind.startswith("inherited"):
            details = v2.get("details") or {}
            if isinstance(details, dict) and "upstream_kind" in details:
                return details["upstream_kind"]
            return "inherited"
        if kind:
            return kind
        fs = m.get("flag_source")
        if isinstance(fs, str) and fs.startswith("inherited"):
            return "inherited"
        return fs

    prev_kind = _norm_kind(prev)
    curr_kind = _norm_kind(curr)
    if (
        prev_kind and curr_kind
        and prev_kind != curr_kind
        and "inherited" not in (prev_kind, curr_kind)
    ):
        out.append(
            f"  {'flag_source':14} kind {prev_kind!r} → {curr_kind!r} — "
            "discovery regressed between preflight and post-edit; baseline unreliable"
        )
    return out


def format_mismatch_block(divergences: list[str]) -> str:
    lines = ["─── LOCI · build mismatch ────────────────"]
    lines.extend(divergences)
    lines.append("──────────────────────────────────────────")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _load_context(path_str: str | None) -> dict | None:
    if not path_str:
        return None
    try:
        return json.loads(Path(path_str).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _reject_header(source: Path) -> int:
    print(
        f"error: {source.name} is a header file; LOCI cannot compile headers "
        f"directly to a .o for analysis.\n"
        f"  Header edits affect the translation units that #include them. "
        f"Identify the .c/.cpp that includes this header and run "
        f"build-metadata compile on that file instead.",
        file=sys.stderr,
    )
    return 2


def _rust_target_triple(loci_target: str) -> str | None:
    return RUST_TARGETS.get(loci_target)


def _compile_rust(source: Path, loci_target: str, output: Path,
                  phase: str, meta_prev_path: Path | None,
                  inherit_from: dict | None) -> tuple[int, dict | None]:
    import shutil as _shutil

    rustc = _shutil.which("rustc")
    if rustc is None:
        print(
            "error: rustc not found on PATH. LOCI requires rustc for Rust sources.\n"
            "  Install the Rust toolchain (https://rustup.rs) and the target for "
            f"{loci_target}:\n"
            f"    rustup target add {_rust_target_triple(loci_target) or '<target triple>'}",
            file=sys.stderr,
        )
        return 127, None

    if inherit_from is not None:
        compiler = inherit_from["compiler"]
        flags = list(inherit_from["flags"])
        flag_source = f"inherited from {meta_prev_path.name}"
        v2_block = {
            "kind": "inherited",
            "details": {"meta_prev": str(meta_prev_path)},
            "confidence": "exact", "warnings": [], "cpu_override": None,
            "user_override_applied": False, "augmented_by": [],
            "attempts": [],
        }
    else:
        triple = _rust_target_triple(loci_target)
        if triple is None:
            print(
                f"error: no rustc target triple known for loci_target={loci_target!r}.\n"
                f"  Supported Rust targets: {sorted(RUST_TARGETS.keys())}",
                file=sys.stderr,
            )
            return 2, None
        compiler = rustc
        flags = list(RUST_FLAGS_BASE) + ["--target", triple]
        flag_source = "defaults (rustc)"
        v2_block = {
            "kind": "defaults-rustc",
            "details": {"target_triple": triple},
            "confidence": "low", "warnings": [], "cpu_override": None,
            "user_override_applied": False, "augmented_by": [],
            "attempts": [],
        }

    cmd = [compiler] + flags + [str(source), "-o", str(output)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: rustc invocation failed: {exc}", file=sys.stderr)
        return 1, None
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        print(f"error: rustc failed (exit {proc.returncode})", file=sys.stderr)
        return proc.returncode, None
    if not output.is_file():
        sys.stderr.write(proc.stderr)
        print(
            f"error: rustc reported success but {output} was not produced.",
            file=sys.stderr,
        )
        return 1, None

    meta = {
        "schema_version": 2,
        "source_file": str(source),
        "source_hash": source_hash(source),
        "compiler": compiler,
        "compiler_version": compiler_version(compiler),
        "flags": flags,
        "architecture": loci_target,
        "loci_target": loci_target,
        "output": str(output),
        "phase": phase,
        "flag_source": flag_source,
        "flag_source_v2": v2_block,
        "language": "rust",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return 0, meta


def compile_subcommand(args) -> int:
    source = Path(args.source).resolve()
    if not source.is_file():
        print(f"error: source not found: {source}", file=sys.stderr)
        return 2

    ext = source.suffix.lower()
    if ext in HEADER_EXTS:
        return _reject_header(source)
    if ext not in COMPILABLE_EXTS:
        print(
            f"error: unsupported source extension {ext!r}. "
            f"LOCI can compile: {sorted(COMPILABLE_EXTS)}",
            file=sys.stderr,
        )
        return 2

    context = _load_context(args.context)

    if args.project_root:
        project_root = Path(args.project_root).resolve()
    elif context and context.get("project_root") not in (None, "", "unknown"):
        project_root = Path(context["project_root"]).resolve()
    else:
        project_root = Path.cwd()

    inherit_from: dict | None = None
    meta_prev_path: Path | None = None
    if args.meta_prev:
        meta_prev_path = Path(args.meta_prev)
        try:
            inherit_from = json.loads(meta_prev_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot read --meta-prev: {exc}", file=sys.stderr)
            return 2

    output = Path(args.output).resolve() if args.output else build_output_path(
        source, args.loci_target, project_root)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Rust path — isolated, unchanged cascade
    if ext in RUST_EXTS:
        rc, meta = _compile_rust(
            source=source,
            loci_target=args.loci_target,
            output=output,
            phase=args.phase,
            meta_prev_path=meta_prev_path,
            inherit_from=inherit_from,
        )
        if rc != 0 or meta is None:
            return rc or 1
        meta_path = output.parent / f"{output.name}.meta.json"
        meta_path.write_text(
            json.dumps(meta, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        if args.verbose:
            print(format_metadata_block(meta))
        return 0

    # C/C++ path
    if inherit_from is not None:
        compiler = inherit_from["compiler"]
        flags = list(inherit_from["flags"])
        flag_source = f"inherited from {meta_prev_path.name}"
        decision = FlagDecision(
            compiler=compiler,
            flags=flags,
            kind="inherited",
            confidence="exact",
            details={"meta_prev": str(meta_prev_path)},
            warnings=[],
            attempts=[AttemptRecord(
                kind="inherited", result="accepted",
                detail=str(meta_prev_path),
            )],
            effective_loci_target=args.loci_target,
        )
    else:
        try:
            decision = detect_flags_verbose(
                source, args.loci_target, project_root, context,
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        flag_source = decision.kind if decision.kind else "unknown"
        compiler = decision.compiler
        flags = decision.flags

    # Resolve build_dir for diagnostic purposes (and the guard)
    build_dir = fs_build_root.find_build_root(source, project_root, context or {})

    # If the discovered compiler doesn't exist, try PATH lookup, then
    # consult the project-context compiler_path hint as a last resort.
    # This handles cross-platform traps where `imports.mak` hardcodes
    # a Linux path but the user runs on Windows (or vice versa).
    import shutil as _shutil
    if not Path(compiler).is_file() and not _shutil.which(compiler):
        basename = Path(compiler).name
        if basename.lower().endswith(".exe"):
            basename = basename[:-4]
        alt = _shutil.which(basename) or _shutil.which(basename + ".exe")
        if alt:
            compiler = alt
        elif context and isinstance(context, dict):
            ctx_path = context.get("compiler_path")
            if ctx_path and Path(ctx_path).is_file():
                # Use context path only if its basename matches what was discovered
                if Path(ctx_path).stem.lower().startswith(basename.lower()[:6]):
                    compiler = ctx_path
                    decision.warnings.append(
                        f"compiler_path_resolved: discovered path did not exist; "
                        f"substituted from context.compiler_path={ctx_path}"
                    )
        # Final fallback: well-known Windows TI path
        if not Path(compiler).is_file() and not _shutil.which(compiler):
            import glob as _glob
            for pattern in (
                r"C:\ti\ticlang\bin\{0}.exe",
                r"C:\ti\ccs*\tools\compiler\ti-cgt-armllvm*\bin\{0}.exe",
            ):
                for match in _glob.glob(pattern.format(basename)):
                    compiler = match
                    decision.warnings.append(
                        f"compiler_path_resolved: found {basename} at {match}"
                    )
                    break
                if Path(compiler).is_file():
                    break

    # Doomed-compile static guard — skip when inheriting (trust prior decision)
    if inherit_from is None:
        err = _check_doomed_compile(source, decision, build_dir)
        if err is not None:
            print(err, file=sys.stderr)
            return 1

    effective_target = decision.effective_loci_target or args.loci_target

    cmd = [compiler] + flags + [str(source), "-o", str(output)]

    def _emit_attempt_trace():
        if inherit_from is None and decision.attempts:
            sys.stderr.write("\nLOCI flag-source trace:\n")
            for a in decision.attempts:
                sys.stderr.write(
                    f"  [{a.kind}] {a.result}  ({a.detail})\n"
                )

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        print(f"error: compiler not found on PATH: {compiler}", file=sys.stderr)
        _emit_attempt_trace()
        return 127
    except OSError as exc:
        print(f"error: failed to invoke compiler: {exc}", file=sys.stderr)
        _emit_attempt_trace()
        return 1
    except subprocess.TimeoutExpired:
        print(f"error: compile timed out after 180s: {' '.join(cmd)}", file=sys.stderr)
        _emit_attempt_trace()
        return 1

    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        # Surface -I paths that don't exist — the most common project-side
        # cause of header-not-found failures on projects that hand LOCI
        # flags via Makefile placeholder variables.
        missing_dirs_block = _diagnose_missing_include_dirs(proc.stderr, flags)
        if missing_dirs_block:
            sys.stderr.write(missing_dirs_block + "\n")
        # Tail the attempt trace so even real-compile errors carry provenance
        _emit_attempt_trace()
        print(f"error: compile failed (exit {proc.returncode}): "
              f"{' '.join(shlex.quote(c) for c in cmd)}", file=sys.stderr)
        return proc.returncode

    if not output.is_file():
        sys.stderr.write(proc.stderr)
        print(
            f"error: compiler exited 0 but {output} was not produced. "
            f"Review the compiler stderr above — this usually means the "
            f"source file type was not recognized.",
            file=sys.stderr,
        )
        return 1

    # Write meta.json (schema v2 with flag_source_v2).
    # On inherited path, preserve the upstream kind so diff_metas can tell
    # that a kind difference is a legitimate inherit rather than a regression.
    if inherit_from is not None:
        upstream_v2 = inherit_from.get("flag_source_v2") or {}
        upstream_kind = (
            upstream_v2.get("kind")
            if upstream_v2.get("kind") and upstream_v2.get("kind") != "inherited"
            else (inherit_from.get("flag_source") or "unknown").split(" (", 1)[0]
        )
        # Merge any warnings added during this compile (e.g. compiler_path_resolved)
        merged_warnings = list(upstream_v2.get("warnings") or [])
        for w in decision.warnings:
            if w not in merged_warnings:
                merged_warnings.append(w)
        v2_block = {
            "kind": "inherited",
            "details": {
                "meta_prev": str(meta_prev_path),
                "upstream_kind": upstream_kind,
            },
            "confidence": upstream_v2.get("confidence", "exact"),
            "warnings": merged_warnings,
            "cpu_override": upstream_v2.get("cpu_override"),
            "user_override_applied": bool(upstream_v2.get("user_override_applied", False)),
            "augmented_by": list(upstream_v2.get("augmented_by") or []),
            "attempts": [{
                "kind": "inherited", "result": "accepted",
                "detail": f"from {meta_prev_path.name} (upstream_kind={upstream_kind})",
                "duration_ms": 0,
            }],
        }
    else:
        v2_block = decision.as_v2_block()

    meta = {
        "schema_version": 2,
        "source_file": str(source),
        "source_hash": source_hash(source),
        "compiler": compiler,
        "compiler_version": compiler_version(compiler),
        "flags": flags,
        "architecture": effective_target,
        "loci_target": effective_target,
        "output": str(output),
        "phase": args.phase,
        "flag_source": flag_source,
        "flag_source_v2": v2_block,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    meta_path = output.parent / f"{output.name}.meta.json"
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    if args.verbose:
        print(format_metadata_block(meta))
    return 0


def diff_subcommand(args) -> int:
    try:
        prev = json.loads(Path(args.prev).read_text(encoding="utf-8"))
        curr = json.loads(Path(args.curr).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read meta: {exc}", file=sys.stderr)
        return 2

    divergences = diff_metas(prev, curr)
    if divergences:
        print(format_mismatch_block(divergences))
        return 1
    print("build metadata matches — preflight and post-edit used the same "
          "compiler and flags")
    return 0


def print_subcommand(args) -> int:
    try:
        meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read meta: {exc}", file=sys.stderr)
        return 2
    print(format_metadata_block(meta))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="build-metadata",
        description="LOCI build metadata — record compiler/flags for preflight "
                    "and verify the post-edit rebuild matches.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("compile",
                         help="Compile a source, write .meta.json, print block")
    pc.add_argument("--source", required=True, help="Path to the source file")
    pc.add_argument("--loci-target", required=True,
                    help="One of aarch64, armv7e-m, armv6-m, tc399")
    pc.add_argument("--context", default=None,
                    help="Path to project-context JSON (optional fallback)")
    pc.add_argument("--project-root", default=None,
                    help="Search root for compile_commands.json (default: cwd)")
    pc.add_argument("--output", default=None,
                    help="Path to emit .o (default: .loci-build/<target>/<stem>.o)")
    pc.add_argument("--meta-prev", default=None,
                    help="Inherit compiler+flags from this meta file instead "
                         "of detecting them (post-edit path).")
    pc.add_argument("--phase", default="preflight",
                    choices=("preflight", "post-edit"))
    pc.add_argument("--verbose", action="store_true",
                    help="Print the build-metadata block on stdout")

    pd = sub.add_parser("diff", help="Compare two .meta.json files")
    pd.add_argument("--prev", required=True)
    pd.add_argument("--curr", required=True)
    pd.add_argument("--verbose", action="store_true",
                    help="Print build metadata output")

    pp = sub.add_parser("print", help="Pretty-print a .meta.json")
    pp.add_argument("--meta", required=True)
    pp.add_argument("--verbose", action="store_true",
                    help="Print build metadata output")

    args = parser.parse_args()
    loci_log.info("build-metadata", f"start: cmd={args.cmd}")
    try:
        if args.cmd == "compile":
            rc = compile_subcommand(args)
        elif args.cmd == "diff":
            rc = diff_subcommand(args)
        elif args.cmd == "print":
            rc = print_subcommand(args)
        else:
            rc = 2
    except Exception as e:
        loci_log.error("build-metadata",
                       f"end: cmd={args.cmd} ({type(e).__name__}: {e})")
        raise
    loci_log.info("build-metadata", f"end: cmd={args.cmd} rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
