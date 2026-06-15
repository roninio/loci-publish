"""Reconcile a discovered compiler / `-mcpu` with the session's loci_target.

Policy (from plan): the discovered flags win within the same arch family.
Cross-family mismatches reject. A within-family CPU disagreement flips
loci_target for this compile only and records a `cpu_override` warning.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    COMPILER_FAMILY, CPU_TO_TARGET, LOCI_TARGET_FAMILY,
    compiler_family, is_arch_flag,
)


@dataclass
class ReconcileResult:
    accept: bool
    reason: str = ""
    effective_target: str | None = None
    cpu_override: dict | None = None
    warnings: list[str] = field(default_factory=list)


_DEFAULT_CPU: dict[str, str] = {
    "armv6-m":  "cortex-m0plus",
    "armv7e-m": "cortex-m4",
    "aarch64":  "",  # uses -march instead
    "tc399":    "tc3xx",
}


def extract_cpu(flags: list[str]) -> str | None:
    for f in flags:
        if f.startswith("-mcpu="):
            return f[len("-mcpu="):]
    return None


def extract_march(flags: list[str]) -> str | None:
    for f in flags:
        if f.startswith("-march="):
            return f[len("-march="):]
    return None


def reconcile_arch(
    compiler: str,
    flags: list[str],
    loci_target: str,
) -> ReconcileResult:
    """Decide whether a discovery result's compiler+flags are usable."""
    target_family = LOCI_TARGET_FAMILY.get(loci_target)
    if target_family is None:
        # Unknown session target — can't disprove. Accept.
        return ReconcileResult(accept=True, effective_target=loci_target)

    family = compiler_family(compiler)

    # Host-native compilers (g++, clang++ with no cross prefix) — only
    # acceptable for aarch64 if the host is aarch64; otherwise reject.
    if family is None:
        return ReconcileResult(
            accept=False,
            reason=(
                f"compiler {Path(compiler).name!r} has no recognized cross-"
                f"toolchain prefix; rejecting for loci_target={loci_target}"
            ),
        )

    if family != target_family:
        return ReconcileResult(
            accept=False,
            reason=(
                f"compiler family {family!r} does not match "
                f"loci_target={loci_target!r} (family {target_family!r})"
            ),
        )

    # Within-family: check for CPU compatibility
    cpu = extract_cpu(flags)
    if cpu is None:
        # No CPU in discovered flags — cascade will use the target default.
        return ReconcileResult(
            accept=True,
            effective_target=loci_target,
        )

    # Map CPU → LOCI target. Treat anything that maps to the same family as
    # same-family; pick whichever is the more specific (the discovered CPU).
    discovered_target = CPU_TO_TARGET.get(cpu)
    if discovered_target is None:
        # Unknown CPU string (e.g. `-mcpu=cortex-m4+nofp`) — strip any `+…`
        # suffix and retry.
        base_cpu = cpu.split("+", 1)[0]
        discovered_target = CPU_TO_TARGET.get(base_cpu)

    if discovered_target is None:
        # Accept but warn.
        return ReconcileResult(
            accept=True,
            effective_target=loci_target,
            warnings=[f"cpu_unknown: -mcpu={cpu!r} not in CPU_TO_TARGET"],
        )

    if discovered_target == loci_target:
        return ReconcileResult(accept=True, effective_target=loci_target)

    # Same family, different target (e.g. session says armv7e-m but
    # discovered is cortex-m0plus). Discovered wins.
    discovered_fam = LOCI_TARGET_FAMILY.get(discovered_target)
    if discovered_fam == target_family:
        return ReconcileResult(
            accept=True,
            effective_target=discovered_target,
            cpu_override={
                "session_target": loci_target,
                "discovered_target": discovered_target,
                "discovered_cpu": cpu,
            },
            warnings=[
                f"cpu_downgrade: session loci_target={loci_target} but "
                f"discovered -mcpu={cpu} ({discovered_target}). Using "
                f"discovered target for this compile."
            ],
        )

    return ReconcileResult(
        accept=False,
        reason=(
            f"discovered target {discovered_target!r} is in family "
            f"{discovered_fam!r}, not {target_family!r}"
        ),
    )


def choose_compiler_for_source(default_compiler: str, source: Path) -> str:
    """Pick C compiler for .c files when default is a C++ driver."""
    ext = source.suffix.lower()
    if ext != ".c":
        return default_compiler
    if default_compiler.endswith("clang++"):
        return default_compiler[:-2]  # "clang++" → "clang"
    if default_compiler.endswith("g++"):
        return default_compiler[:-3] + "gcc"
    return default_compiler


__all__ = [
    "ReconcileResult", "reconcile_arch",
    "extract_cpu", "extract_march",
    "choose_compiler_for_source",
]
