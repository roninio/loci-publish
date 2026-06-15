"""Expand `@file.opt` response-file arguments used by TI and others.

Response files contain one flag per line (or whitespace-separated).
Comments start with `#`. Blank lines are ignored.
"""
from __future__ import annotations

import shlex
from pathlib import Path


def expand_response_files(
    flags: list[str],
    cwd: Path,
    _depth: int = 0,
) -> tuple[list[str], list[dict]]:
    """Expand every `@<path>` token in `flags` to the file's contents.

    `cwd` is the directory to resolve relative paths against.

    Returns `(expanded_flags, augmentations)` where `augmentations` is a
    list of per-file records suitable for `flag_source_v2.augmented_by`.
    Missing files are skipped and surface as warnings, not errors — the
    cascade tolerates best-effort augmentation.
    """
    if _depth > 4:
        # Guard against circular includes
        return list(flags), []

    out: list[str] = []
    augmentations: list[dict] = []

    for flag in flags:
        if not flag.startswith("@"):
            out.append(flag)
            continue

        rel = flag[1:].strip()
        if not rel:
            out.append(flag)
            continue

        p = Path(rel)
        if not p.is_absolute():
            p = cwd / p

        if not p.is_file():
            # Keep the @token so the caller can see it failed to resolve;
            # record a warning in augmentations so metadata captures it.
            out.append(flag)
            augmentations.append({
                "kind": "response_file_missing",
                "file": str(rel),
                "resolved_from": str(cwd),
            })
            continue

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            out.append(flag)
            continue

        nested = _tokenize_response_file(content)
        # Recurse — response files can reference other response files
        nested_expanded, nested_aug = expand_response_files(
            nested, p.parent, _depth + 1,
        )
        out.extend(nested_expanded)
        augmentations.append({
            "kind": "response_file_expand",
            "file": str(p),
            "added_flags": len(nested_expanded),
        })
        augmentations.extend(nested_aug)

    return out, augmentations


def _tokenize_response_file(content: str) -> list[str]:
    """Tokenize response-file content to a flat flag list."""
    tokens: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Allow inline # comments, but not inside quoted strings
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            parts = line.split()
        tokens.extend(parts)
    return tokens


__all__ = ["expand_response_files"]
