"""Step 6: parse TI CCS `.projectspec` XML for `-I` / `-D` hints.

The `compilerBuildOptions=` attribute contains a whitespace-separated
list of compiler flags. It is IDE-sourced boilerplate that LIES about
CPU/ABI (BLE projectspec says `-mcpu=cortex-m4` even though the real
build uses `cortex-m0plus`). We therefore keep only the `-I*`,
`-isystem`, `-D*`, and `-U*` entries and mark the result partial so
the cascade merges arch flags from another source.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from . import DiscoveryResult, shlex_split_line
from .flags_normalize import (
    dedup_preserve_order, keep_includes_and_defines,
)


def _find_projectspecs(project_root: Path, build_dir: Path | None) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path):
        if not p.is_file():
            return
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp in seen:
            return
        seen.add(rp)
        found.append(rp)

    if build_dir is not None:
        for p in build_dir.glob("*.projectspec"):
            _add(p)

    try:
        for p in project_root.rglob("*.projectspec"):
            _add(p)
            if len(found) > 8:
                break
    except OSError:
        pass

    return found


def _resolve_tokens(raw: str, ps_dir: Path) -> list[str]:
    """Tokenize the compilerBuildOptions attribute, resolve ${PROJECT_LOC}
    and ${PROJECT_ROOT} to the projectspec's directory."""
    tokens = shlex_split_line(raw)
    out: list[str] = []
    for t in tokens:
        t2 = (
            t.replace("${PROJECT_LOC}", str(ps_dir))
             .replace("${PROJECT_ROOT}", str(ps_dir))
             .replace("${ConfigName}", ".")
        )
        # Unresolved ${...} tokens are dropped — they're usually
        # IDE-private variables we can't honor.
        if "${" in t2:
            continue
        out.append(t2)
    return out


def _extract_from_xml(ps_path: Path) -> list[str]:
    try:
        tree = ET.parse(ps_path)
    except ET.ParseError:
        return []
    except OSError:
        return []
    root = tree.getroot()
    flags: list[str] = []

    # projectspec schemas vary; look in any element for compilerBuildOptions=
    for elem in root.iter():
        for attr_name, attr_val in elem.attrib.items():
            if attr_name == "compilerBuildOptions" and attr_val:
                flags.extend(_resolve_tokens(attr_val, ps_path.parent))

    return flags


def discover(
    source: Path,
    loci_target: str,
    project_root: Path,
    context: dict,
    build_dir: Path | None,
) -> DiscoveryResult | None:
    specs = _find_projectspecs(project_root, build_dir)
    if not specs:
        return None

    all_flags: list[str] = []
    used: list[str] = []
    for ps in specs[:4]:
        flags = _extract_from_xml(ps)
        if flags:
            all_flags.extend(flags)
            used.append(str(ps))

    if not all_flags:
        return None

    # Drop everything except includes/defines — projectspec CPU/ABI flags
    # are IDE boilerplate and cannot be trusted.
    filtered = keep_includes_and_defines(all_flags)
    filtered = dedup_preserve_order(filtered)

    if not filtered:
        return None

    compiler = context.get("compiler") if isinstance(context, dict) else "unknown"
    compiler = compiler or "unknown"

    return DiscoveryResult(
        compiler=compiler,
        flags=filtered,
        kind="projectspec-xml",
        confidence="medium",
        details={
            "projectspecs": used,
            "include_count": sum(1 for f in filtered if f.startswith("-I") or f == "-isystem"),
            "define_count": sum(1 for f in filtered if f.startswith("-D") or f.startswith("-U")),
        },
        warnings=[
            "projectspec CPU/ABI/standard flags stripped; supply those via "
            "defaults or another source",
        ],
        partial=True,
    )


__all__ = ["discover"]
