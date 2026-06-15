"""Step 1: `compile_commands.json` lookup (exact)."""
from __future__ import annotations

import json
from pathlib import Path

from . import DiscoveryResult, shlex_split_line
from .flags_normalize import strip_source_and_output


_CC_SEARCH = (
    "compile_commands.json",
    "build/compile_commands.json",
    ".loci-build/compile_commands.json",
    "Debug/compile_commands.json",
    "Release/compile_commands.json",
    "out/compile_commands.json",
    # TI-style build locations also discovered via find_build_root
)


def find_compile_commands(project_root: Path) -> Path | None:
    """Return the path to compile_commands.json under project_root, or None."""
    for rel in _CC_SEARCH:
        p = project_root / rel
        if p.is_file():
            return p
    # Also search one-level-deep build dirs (e.g. build-debug/)
    try:
        for child in project_root.iterdir():
            if child.is_dir() and child.name.lower().startswith("build"):
                p = child / "compile_commands.json"
                if p.is_file():
                    return p
    except OSError:
        pass
    return None


def parse_entry(entry: dict) -> tuple[str, list[str]]:
    """Extract (compiler, flags) from one compile_commands.json entry."""
    if "arguments" in entry:
        args = list(entry["arguments"])
    elif "command" in entry:
        args = shlex_split_line(entry["command"])
    else:
        raise ValueError("compile_commands entry has neither 'arguments' nor 'command'")
    if not args:
        raise ValueError("compile_commands entry has empty command")

    compiler = args[0]
    source_file = entry.get("file", "")
    source_name = Path(source_file).name if source_file else ""

    flags = []
    for arg in strip_source_and_output(args[1:]):
        # Skip the source file itself (absolute or relative basename match)
        if source_file and (arg == source_file or Path(arg).name == source_name):
            continue
        flags.append(arg)
    return compiler, flags


def _find_entry_for_source(data: list, source: Path) -> dict | None:
    src_resolved = source.resolve()
    src_name = source.name
    # Prefer resolved-path match; fall back to basename match.
    basename_match: dict | None = None
    for entry in data:
        entry_file = Path(entry.get("file", ""))
        if not entry_file.is_absolute():
            entry_file = Path(entry.get("directory", "")) / entry_file
        try:
            if entry_file.resolve() == src_resolved:
                return entry
        except (OSError, ValueError):
            pass
        if entry_file.name == src_name and basename_match is None:
            basename_match = entry
    return basename_match


def discover(
    source: Path,
    loci_target: str,
    project_root: Path,
    context: dict,
    build_dir: Path | None,
) -> DiscoveryResult | None:
    cc_path = find_compile_commands(project_root)
    if cc_path is None and build_dir is not None:
        cand = build_dir / "compile_commands.json"
        if cand.is_file():
            cc_path = cand
    if cc_path is None:
        return None

    try:
        data = json.loads(cc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    entry = _find_entry_for_source(data, source)
    if entry is None:
        return None

    try:
        compiler, flags = parse_entry(entry)
    except ValueError:
        return None

    try:
        rel = cc_path.relative_to(project_root)
    except ValueError:
        rel = cc_path

    return DiscoveryResult(
        compiler=compiler,
        flags=flags,
        kind="compile_commands",
        confidence="exact",
        details={
            "compile_commands_path": str(rel),
            "entry_file": entry.get("file", ""),
            "entry_directory": entry.get("directory", ""),
        },
    )


__all__ = ["discover", "find_compile_commands", "parse_entry"]
