"""Step 7 (augmenter role): regex-scan makefiles for `-I`, `-isystem`, `-D`.

Unable to expand `$(VAR)` or `@file.opt` response files — that's what
gmake_dryrun and python_makefile are for. Kept for the case where
those failed but some defines are still recoverable.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import DiscoveryResult


_MAX_LINES = 50_000
_INC_PATTERNS = (
    re.compile(r'-I"([^"]*)"'),
    re.compile(r"-I'([^']*)'"),
    re.compile(r"-I(\S+)"),
)
_ISYSTEM_PATTERN = re.compile(r"-isystem\s+(\S+)")
_DEFINE_PATTERN = re.compile(r"(-D\S+)")
_UNDEF_PATTERN = re.compile(r"(-U\S+)")

# Skip flags that contain unresolved $(VAR) references — they're not valid
# include paths as literal strings.
_VAR_REF = re.compile(r"\$\(.+?\)|\$\{.+?\}")


def _extract(content: str) -> list[str]:
    flags: list[str] = []
    seen: set[str] = set()
    lines_read = 0
    for line in content.splitlines():
        lines_read += 1
        if lines_read > _MAX_LINES:
            break
        for pat in _INC_PATTERNS:
            for m in pat.finditer(line):
                path = m.group(1)
                if _VAR_REF.search(path):
                    continue
                flag = f"-I{path}" if not m.group(0).startswith("-I\"") else f'-I"{path}"'
                # Normalize quoted form to unquoted
                if flag.startswith('-I"') and flag.endswith('"'):
                    flag = "-I" + flag[3:-1]
                if flag not in seen:
                    seen.add(flag)
                    flags.append(flag)
        for m in _ISYSTEM_PATTERN.finditer(line):
            path = m.group(1)
            if _VAR_REF.search(path):
                continue
            flag = f"-isystem {path}"
            if flag not in seen:
                seen.add(flag)
                flags.append("-isystem")
                flags.append(path)
        for m in _DEFINE_PATTERN.finditer(line):
            flag = m.group(1)
            if _VAR_REF.search(flag):
                continue
            if flag not in seen:
                seen.add(flag)
                flags.append(flag)
        for m in _UNDEF_PATTERN.finditer(line):
            flag = m.group(1)
            if flag not in seen:
                seen.add(flag)
                flags.append(flag)
    return flags


def _candidate_makefiles(project_root: Path, source: Path,
                          build_dir: Path | None) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path):
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp in seen or not rp.is_file():
            return
        seen.add(rp)
        candidates.append(rp)

    # 1) build_dir first
    if build_dir is not None:
        for name in ("makefile", "Makefile", "GNUmakefile"):
            _add(build_dir / name)
        for p in build_dir.glob("*.mk"):
            _add(p)

    # 2) source's parents up to 4 levels
    parent = source.parent
    for _ in range(4):
        for name in ("makefile", "Makefile", "GNUmakefile"):
            _add(parent / name)
        for p in parent.glob("*.mk"):
            _add(p)
        if parent == parent.parent:
            break
        parent = parent.parent

    # 3) project_root
    for name in ("makefile", "Makefile", "GNUmakefile"):
        _add(project_root / name)
    try:
        for p in project_root.glob("*.mk"):
            _add(p)
    except OSError:
        pass

    return candidates


def discover(
    source: Path,
    loci_target: str,
    project_root: Path,
    context: dict,
    build_dir: Path | None,
) -> DiscoveryResult | None:
    candidates = _candidate_makefiles(project_root, source, build_dir)
    if not candidates:
        return None

    collected: list[str] = []
    seen: set[str] = set()
    scanned: list[str] = []

    for mk in candidates[:12]:  # cap
        try:
            content = mk.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned.append(str(mk))
        for f in _extract(content):
            if f not in seen:
                seen.add(f)
                collected.append(f)

    if not collected:
        return None

    # Emit a compiler hint from context if available, but mark partial so the
    # orchestrator merges with another source's compiler + arch flags.
    compiler = context.get("compiler") if isinstance(context, dict) else ""
    compiler = compiler or "unknown"

    return DiscoveryResult(
        compiler=compiler,
        flags=collected,
        kind="makefile-regex",
        confidence="low",
        details={"scanned": scanned, "collected_count": len(collected)},
        partial=True,  # Never enough flags on its own — always augmenter
    )


__all__ = ["discover"]
