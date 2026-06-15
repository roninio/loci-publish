"""LOCI flag-discovery cascade — one module per source.

Every source exposes a `discover(...)` callable returning a
`DiscoveryResult | None`. The orchestrator in `build_metadata.py`
walks the CASCADE list below and accepts the first fully-resolved
result, merging partial results as it goes.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


# ---------------------------------------------------------------------------
# Compiler family tables (shared)
# ---------------------------------------------------------------------------

COMPILER_FAMILY: dict[str, str] = {
    "aarch64-linux-gnu": "aarch64",
    "aarch64-none-elf":  "aarch64",
    "arm-none-eabi":     "cortexm",
    "tricore-elf":       "tricore",
    "tiarmclang":        "cortexm",
    "armcl":             "cortexm",
    "iccarm":            "cortexm",
    "armcc":             "cortexm",
    "armclang":          "cortexm",
}

LOCI_TARGET_FAMILY: dict[str, str] = {
    "aarch64":  "aarch64",
    "armv7e-m": "cortexm",
    "armv6-m":  "cortexm",
    "tc399":    "tricore",
}

# CPU → LOCI target (for reverse mapping in compiler_match)
CPU_TO_TARGET: dict[str, str] = {
    "cortex-m0":      "armv6-m",
    "cortex-m0plus":  "armv6-m",
    "cortex-m1":      "armv6-m",
    "cortex-m3":      "armv7-m",
    "cortex-m4":      "armv7e-m",
    "cortex-m7":      "armv7e-m",
    "cortex-m23":     "armv8-m.base",
    "cortex-m33":     "armv8-m.main",
    "cortex-m55":     "armv8-m.main",
    "tc3xx":          "tc399",
}


Confidence = Literal["exact", "high", "medium", "low"]
AttemptResult = Literal[
    "accepted", "partial", "rejected-wrong-arch",
    "rejected-insufficient", "missing", "error",
    "skipped", "timeout",
]


@dataclass
class DiscoveryResult:
    """Outcome of a single flag source's `discover()` call."""
    compiler: str
    flags: list[str]
    kind: str
    confidence: Confidence = "medium"
    details: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    partial: bool = False

    def as_dict(self) -> dict:
        return {
            "compiler": self.compiler,
            "flags": list(self.flags),
            "kind": self.kind,
            "confidence": self.confidence,
            "details": dict(self.details),
            "warnings": list(self.warnings),
            "partial": self.partial,
        }


@dataclass(frozen=True)
class DiscoveryMiss:
    """Signals that a discoverer doesn't apply, with a specific reason.

    Returned in place of bare ``None`` so the cascade orchestrator can
    surface a precise sub-failure string in the attempt trace instead of
    falling back to a hardcoded category-level message. Bare ``None`` is
    still accepted (legacy contract) and routed through
    ``_reason_for_missing``.
    """
    reason: str


@dataclass
class AttemptRecord:
    """One entry in the cascade's attempt trace (lives in .meta.json)."""
    kind: str
    result: AttemptResult
    detail: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "result": self.result,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass
class FlagDecision:
    """Final cascade outcome — handed to the compiler invoker."""
    compiler: str
    flags: list[str]
    kind: str
    confidence: Confidence
    details: dict
    warnings: list[str]
    attempts: list[AttemptRecord]
    augmented_by: list[dict] = field(default_factory=list)
    user_override_applied: bool = False
    cpu_override: dict | None = None
    effective_loci_target: str | None = None
    degraded: bool = False

    def as_v2_block(self) -> dict:
        return {
            "kind": self.kind,
            "details": dict(self.details),
            "confidence": self.confidence,
            "warnings": list(self.warnings),
            "cpu_override": self.cpu_override,
            "user_override_applied": self.user_override_applied,
            "augmented_by": list(self.augmented_by),
            "attempts": [a.as_dict() for a in self.attempts],
        }


# ---------------------------------------------------------------------------
# Shared helpers — producer-string parsing + flag classification
# ---------------------------------------------------------------------------

@dataclass
class ParsedProducer:
    compiler: str | None
    flags: list[str]
    version: str | None


_PRODUCER_FLAG_PATTERNS = (
    r"-mcpu=\S+",
    r"-march=\S+",
    r"-mthumb\b",
    r"-mthumb-interwork\b",
    r"-mfloat-abi=\S+",
    r"-mfpu=\S+",
    r"-O\S*",
    r"-std=\S+",
    r"-D\S+",
    r"-I\S+",
    r"-isystem\s+\S+",
)


def parse_producer(producer: str) -> ParsedProducer:
    """Parse DW_AT_producer into (compiler, flags, version).

    Unlike the original implementation this returns the compiler even when
    no flags were extracted, so the caller can merge flags from a later
    cascade step and still benefit from knowing which compiler the ELF
    was built with.
    """
    if not producer:
        return ParsedProducer(None, [], None)

    low = producer.lower()
    compiler: str | None = None
    version: str | None = None

    if "tiarmclang" in low:
        compiler = "tiarmclang"
    elif "armclang" in low:
        compiler = "armclang"
    elif "arm-none-eabi" in low:
        m = re.search(r"arm-none-eabi-\S+", producer, re.IGNORECASE)
        if m:
            compiler = m.group(0)
    elif "aarch64-linux-gnu" in low:
        m = re.search(r"aarch64-linux-gnu-\S+", producer, re.IGNORECASE)
        if m:
            compiler = m.group(0)
    elif "tricore" in low:
        m = re.search(r"tricore-elf-\S+", producer, re.IGNORECASE)
        if m:
            compiler = m.group(0)
    elif "clang" in low:
        compiler = "clang"
    elif "gnu" in low or low.startswith("gcc") or " gcc " in low:
        compiler = "gcc"

    # Version detection — look for something like "3.2.2.LTS" or "12.2.0"
    m = re.search(r"version\s+(\S+)", producer, re.IGNORECASE)
    if m:
        version = m.group(1)

    flags: list[str] = []
    for pattern in _PRODUCER_FLAG_PATTERNS:
        for m in re.finditer(pattern, producer):
            flag = m.group(0).strip()
            if flag and flag not in flags:
                flags.append(flag)

    return ParsedProducer(compiler, flags, version)


def is_include(flag: str) -> bool:
    return flag.startswith("-I") or flag.startswith("-isystem")


def has_include(flags: list[str]) -> bool:
    return any(is_include(f) for f in flags)


def is_arch_flag(flag: str) -> bool:
    return (
        flag.startswith("-mcpu=") or
        flag.startswith("-march=") or
        flag == "-mthumb" or
        flag == "-mthumb-interwork" or
        flag.startswith("-mfloat-abi=") or
        flag.startswith("-mfpu=") or
        flag.startswith("-mlittle-endian") or
        flag.startswith("-mbig-endian")
    )


def is_define(flag: str) -> bool:
    return flag.startswith("-D") or flag.startswith("-U")


def is_lang_flag(flag: str) -> bool:
    return flag.startswith("-std=")


def compiler_family(compiler: str) -> str | None:
    """Return LOCI arch family for a compiler executable basename, or None."""
    bn = Path(compiler).name.lower()
    for prefix, family in COMPILER_FAMILY.items():
        if prefix in bn:
            return family
    return None


def shlex_split_line(line: str) -> list[str]:
    """Tokenize a shell command line, tolerating Windows backslashes.

    shlex.split treats backslash as escape; on Windows it mangles paths.
    We don't try to be clever — we just normalize `\\` to `/` before the
    split, which works for the compile-command use case where paths are
    arguments (never escape characters).
    """
    # Only rewrite backslashes that look like path separators; leave
    # shell-escape backslashes alone. Heuristic: 2+ backslashes or
    # backslash followed by a word-char becomes forward slashes.
    normalized = re.sub(r"\\(?=[A-Za-z0-9_./])", "/", line)
    try:
        return shlex.split(normalized, posix=True)
    except ValueError:
        return shlex.split(normalized, posix=False)


__all__ = [
    "COMPILER_FAMILY", "LOCI_TARGET_FAMILY", "CPU_TO_TARGET",
    "DiscoveryResult", "DiscoveryMiss", "AttemptRecord", "FlagDecision",
    "ParsedProducer", "parse_producer",
    "is_include", "has_include", "is_arch_flag", "is_define", "is_lang_flag",
    "compiler_family", "shlex_split_line",
]
