"""Step 0: user escape hatch — `.loci-build/flags.json` + `LOCI_EXTRA_CFLAGS`.

The override file supports two modes:
- `augment` (default): merge `flags` on top of whatever the cascade wins.
  Per-source globs can also augment or outright replace.
- `replace`: skip the cascade; use `compiler` + `flags` verbatim.

`LOCI_EXTRA_CFLAGS` env var is always append-only.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import DiscoveryResult


@dataclass
class UserOverride:
    """Loaded override config — merged with env var."""
    mode: str = "augment"
    compiler: str | None = None
    compiler_path: str | None = None
    flags: list[str] = field(default_factory=list)
    variables: dict = field(default_factory=dict)
    build_root: str | None = None
    per_source: dict = field(default_factory=dict)
    extra_cflags: list[str] = field(default_factory=list)
    source_path: str | None = None  # where it came from

    @property
    def empty(self) -> bool:
        return not (
            self.compiler or self.flags or self.variables
            or self.build_root or self.per_source or self.extra_cflags
        )

    def per_source_for(self, source: Path, project_root: Path) -> dict | None:
        """Return the matching per_source entry, or None."""
        if not self.per_source:
            return None
        try:
            rel = source.relative_to(project_root).as_posix()
        except ValueError:
            rel = source.name
        for pattern, entry in self.per_source.items():
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(source.name, pattern):
                return entry
        return None


def load_override(project_root: Path) -> UserOverride:
    """Load override from `.loci-build/flags.json` (if any) + env var."""
    ov = UserOverride()

    # Env var is always read (append-only augmentation)
    extra = os.environ.get("LOCI_EXTRA_CFLAGS", "").strip()
    if extra:
        try:
            ov.extra_cflags = shlex.split(extra, posix=(os.name != "nt"))
        except ValueError:
            ov.extra_cflags = extra.split()

    ov_path = project_root / ".loci-build" / "flags.json"
    if not ov_path.is_file():
        return ov

    try:
        data = json.loads(ov_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ov
    if not isinstance(data, dict):
        return ov

    ov.source_path = str(ov_path)
    ov.mode = data.get("mode", "augment") if data.get("mode") in ("augment", "replace") else "augment"
    if isinstance(data.get("compiler"), str):
        ov.compiler = data["compiler"]
    if isinstance(data.get("compiler_path"), str):
        ov.compiler_path = data["compiler_path"]
    if isinstance(data.get("flags"), list):
        ov.flags = [str(f) for f in data["flags"]]
    if isinstance(data.get("variables"), dict):
        ov.variables = {str(k): str(v) for k, v in data["variables"].items()}
    if isinstance(data.get("build_root"), str):
        ov.build_root = data["build_root"]
    if isinstance(data.get("per_source"), dict):
        ov.per_source = data["per_source"]

    return ov


def _expand_vars(flag: str, project_root: Path, variables: dict) -> str:
    """Iterative `${VAR}` / `$(VAR)` substitution in flag strings.

    Resolves variable values that themselves reference other variables
    (e.g. `SDK_SRC = "${PROJECT_ROOT}/sdk/src"`) by re-running substitution
    until the string stops changing. Capped at 8 passes to avoid infinite
    loops on cyclic definitions — a value that still contains `${...}` after
    the cap is left as-is and surfaces as a compile error downstream.

    Recognized well-known variables: PROJECT_ROOT, HOME. Well-known names
    win on collision with user-supplied `variables`.
    """
    well_known = {
        "PROJECT_ROOT": str(project_root),
        "HOME": os.path.expanduser("~"),
    }
    merged = {**variables, **well_known}
    out = flag
    for _ in range(8):
        new_out = out
        for name, value in merged.items():
            new_out = new_out.replace(f"${{{name}}}", value).replace(f"$({name})", value)
        if new_out == out:
            break
        out = new_out
    return out


def discover(
    source: Path,
    loci_target: str,
    project_root: Path,
    context: dict,
    build_dir: Path | None,
) -> DiscoveryResult | None:
    """Only returns a result in `mode: replace`. In `augment` mode the
    orchestrator applies the override after the cascade picks a winner."""
    ov = load_override(project_root)
    if ov.empty:
        return None

    per_source = ov.per_source_for(source, project_root)

    # Replace mode? (either top-level or per-source)
    mode = ov.mode
    compiler = ov.compiler
    flags = list(ov.flags)
    if per_source:
        if per_source.get("mode") in ("augment", "replace"):
            mode = per_source["mode"]
        if isinstance(per_source.get("compiler"), str):
            compiler = per_source["compiler"]
        if isinstance(per_source.get("flags"), list):
            flags = [str(f) for f in per_source["flags"]]

    if mode != "replace":
        return None  # augment-only: applied after cascade, not here

    if not compiler:
        # Replace without a compiler — can't proceed, fall through to cascade
        return None

    expanded = [_expand_vars(f, project_root, ov.variables) for f in flags]
    # Env extras also apply in replace mode
    expanded.extend(ov.extra_cflags)

    return DiscoveryResult(
        compiler=compiler,
        flags=expanded,
        kind="user-override-replace",
        confidence="exact",
        details={
            "source_path": ov.source_path,
            "per_source_pattern": next(
                (p for p in ov.per_source if fnmatch.fnmatch(source.name, p)
                 or fnmatch.fnmatch(str(source), p)), None,
            ),
            "extra_cflags_count": len(ov.extra_cflags),
        },
    )


def apply_augment(
    result: DiscoveryResult,
    source: Path,
    project_root: Path,
) -> tuple[DiscoveryResult, bool]:
    """After the cascade wins, layer augment-mode overrides on top.

    Returns (new_result, applied: bool).
    """
    ov = load_override(project_root)
    if ov.empty:
        return result, False

    per_source = ov.per_source_for(source, project_root)
    mode = ov.mode
    add_flags = list(ov.flags)
    if per_source:
        if per_source.get("mode") in ("augment", "replace"):
            mode = per_source["mode"]
        if isinstance(per_source.get("flags"), list):
            add_flags = [str(f) for f in per_source["flags"]]

    if mode == "replace":
        # Handled by discover(); nothing to do here
        return result, False

    extras = [_expand_vars(f, project_root, ov.variables) for f in add_flags]
    extras.extend(ov.extra_cflags)
    if not extras:
        return result, False

    merged = list(result.flags)
    for f in extras:
        if f not in merged:
            merged.append(f)

    new_details = dict(result.details)
    new_details["user_override_augmented"] = {
        "source_path": ov.source_path,
        "added_flag_count": len(extras),
    }
    return DiscoveryResult(
        compiler=result.compiler,
        flags=merged,
        kind=result.kind,
        confidence=result.confidence,
        details=new_details,
        warnings=list(result.warnings),
        partial=result.partial,
    ), True


__all__ = ["UserOverride", "load_override", "discover", "apply_augment"]
