"""Utilities for deduplicating, merging, and sanitizing flag lists."""
from __future__ import annotations

from . import is_arch_flag, is_define, is_include, is_lang_flag


def dedup_preserve_order(flags: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for f in flags:
        if f not in seen:
            out.append(f)
            seen.add(f)
    return out


def strip_source_and_output(flags: list[str]) -> list[str]:
    """Remove `-o <file>` pairs and any positional source-file tokens.

    Used when lifting flags from a real compile command.
    """
    out: list[str] = []
    skip_next = False
    for flag in flags:
        if skip_next:
            skip_next = False
            continue
        if flag == "-o":
            skip_next = True
            continue
        if flag.startswith("-o") and len(flag) > 2 and not flag.startswith("-oz"):
            # -oFILE form, but avoid stripping -Oz (optimization)
            continue
        out.append(flag)
    return out


def strip_arch_flags(flags: list[str]) -> list[str]:
    """Remove -mcpu/-march/-mthumb/-mfloat-abi/-mfpu.

    Used when lifting flags from an untrustworthy source like .projectspec —
    includes and defines are salvageable, arch flags are not.
    """
    return [f for f in flags if not is_arch_flag(f)]


def keep_includes_and_defines(flags: list[str]) -> list[str]:
    """Filter to only `-I*`, `-isystem …`, `-D*`, `-U*`."""
    out: list[str] = []
    skip_next = False
    for flag in flags:
        if skip_next:
            skip_next = False
            out.append(flag)
            continue
        if is_include(flag) or is_define(flag):
            out.append(flag)
            # -isystem is always a pair: keep the next token too
            if flag == "-isystem":
                skip_next = True
    return out


def merge(*lists: list[str]) -> list[str]:
    """Merge flag lists preserving order, deduplicated."""
    combined: list[str] = []
    for lst in lists:
        combined.extend(lst)
    return dedup_preserve_order(combined)


def ensure_required(flags: list[str]) -> list[str]:
    """Guarantee `-c` and some form of `-g` are present."""
    out = list(flags)
    if "-c" not in out:
        out.append("-c")
    has_g = any(
        f == "-g"
        or (f.startswith("-g") and (len(f) == 2 or f[2:3] in ("", "d", "s")))
        for f in out
    )
    if not has_g:
        out.insert(0, "-g")
    return out


__all__ = [
    "dedup_preserve_order", "strip_source_and_output",
    "strip_arch_flags", "keep_includes_and_defines",
    "merge", "ensure_required",
]
