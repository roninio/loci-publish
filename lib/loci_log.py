"""LOCI plugin — shared Python logger.

Appends checkpoint lines to $LOCI_STATE_DIR/loci.log. Same format and gating
as lib/loci_log.sh, so the two loggers can write to the same file from the
same process tree without conflict.

Designed for short-lived CLI invocations (asm_analyze.py, build_metadata.py,
loci_stats.py, hook scripts) — no logging-module configuration that would
leak into a long-running parent process.

Line format matches Claude Code's debug log so correlating by timestamp
against ~/.claude/debug/<session>.txt is trivial:

    2026-05-05T11:29:03.107Z [INFO] [loci.<source>] message
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "OFF": 99}
_ROTATE_AT = 5 * 1024 * 1024
_ROTATE_KEEP = 1 * 1024 * 1024


def _resolve_threshold() -> int | None:
    raw = os.environ.get("LOCI_LOG_LEVEL", "").strip().upper()
    if not raw or raw == "OFF":
        return None
    return _LEVELS.get(raw)


def _resolve_log_file() -> Path | None:
    raw = os.environ.get("LOCI_STATE_DIR")
    d = Path(raw) if raw else Path.cwd() / ".loci" / "state"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d / "loci.log"
    except OSError:
        return None


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size > _ROTATE_AT:
            with path.open("rb") as f:
                f.seek(-_ROTATE_KEEP, os.SEEK_END)
                tail = f.read()
            path.write_bytes(tail)
    except OSError:
        # Best-effort logging: rotation failures must not break caller flow.
        return


# Resolve once at import time. Per-call resolution costs an env read and
# stat/mkdir which adds up across many short-lived hook invocations.
_THRESHOLD: int | None = _resolve_threshold()
_LOG_FILE: Path | None = None
if _THRESHOLD is not None:
    _LOG_FILE = _resolve_log_file()
    if _LOG_FILE is not None and _LOG_FILE.exists():
        _rotate(_LOG_FILE)


def log(level: str, source: str, msg: str) -> None:
    """Emit a single log line. Never raises."""
    if _LOG_FILE is None or _THRESHOLD is None:
        return
    lvl_num = _LEVELS.get(level.upper(), 20)
    if lvl_num < _THRESHOLD:
        return

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    line = f"{ts} [{level.upper()}] [loci.{source}] {msg}\n"

    try:
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        # Best-effort logging: write failures are intentionally ignored.
        return


def info(source: str, msg: str) -> None:
    log("INFO", source, msg)


def warn(source: str, msg: str) -> None:
    log("WARN", source, msg)


def error(source: str, msg: str) -> None:
    log("ERROR", source, msg)


def debug(source: str, msg: str) -> None:
    log("DEBUG", source, msg)


class around:
    """Context manager: log start + end of a block.

    Usage:
        with around("asm-analyze", "extract-assembly"):
            do_work()
    """

    def __init__(self, source: str, label: str):
        self.source = source
        self.label = label

    def __enter__(self) -> "around":
        info(self.source, f"start: {self.label}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            info(self.source, f"end: {self.label}")
        else:
            error(self.source, f"end: {self.label} (raised {exc_type.__name__}: {exc})")
