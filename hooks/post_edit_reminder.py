#!/usr/bin/env python3
"""
PostToolUse hook — LOCI post-edit reminder.

Fires after Edit/Write/MultiEdit. If the target file is a C/C++/Rust source,
emits an additionalContext reminder telling Claude to invoke loci-post-edit.

This is the automated backstop — even if Claude misses the system-reminder
instruction, this hook puts the reminder directly in the tool-use response.

Always exits 0 (advisory, never blocking).
"""

import atexit
import json
import os
import re
import sys
from pathlib import Path

# Shared file-only logger (no-op unless LOCI_LOG_LEVEL is set).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import loci_log  # noqa: E402

# Force UTF-8 for all Python I/O and any child Python process we spawn.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower().replace("-", "") != "utf8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_SOURCE_EXTS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx", ".rs"}
_HEADER_EXTS = {".h", ".hpp", ".hxx"}


def _extract_edit_content(tool_name: str, tool_input: dict) -> str:
    """Pull the newly inserted content from whichever write-family tool fired."""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string", "") or ""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits", []) or []
        return "\n".join((e.get("new_string", "") or "") for e in edits)
    return ""


def _is_no_measurement_edit(code: str, ext: str) -> bool:
    """Return True when the edit cannot change compiled function bodies.

    Skipping these cases prevents the post-edit skill from being invoked on
    header-plumbing, typedef promotion, or #include additions that have no
    measurable effect — LOCI would spin up the full compile+MCP pipeline
    only to report 'nothing changed'.
    """
    # Headers never produce their own .o; measurement needs a compilation unit.
    if ext in _HEADER_EXTS:
        return True

    if not code:
        return False  # Let the skill surface whatever state it finds.

    # Strip C/C++ comments and blank lines.
    without_comments = re.sub(
        r"//.*?$|/\*.*?\*/", "", code, flags=re.MULTILINE | re.DOTALL
    )
    non_empty = [ln for ln in without_comments.splitlines() if ln.strip()]

    if not non_empty:
        return True  # Pure-whitespace / comment-only edit.

    # #include-only edit (any number of include lines, nothing else).
    if all(ln.strip().startswith("#include") for ln in non_empty):
        return True

    # No opening brace anywhere in the new content → can't introduce or
    # modify a function body. Declarations/macros/typedefs/globals only.
    # Note: an Edit that rewrites lines *inside* an existing function body
    # normally carries braces (assignment expressions, control flow),
    # so this rarely false-skips a real change.
    if "{" not in without_comments:
        return True

    return False


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    hook_event = data.get("hook_event_name", "PostToolUse")

    # Extract file path from the tool input
    file_path = tool_input.get("file_path", "")
    loci_log.info("post-edit",
                  f"start: {hook_event} hook (tool={tool_name} file={file_path or '?'})")
    atexit.register(lambda: loci_log.info("post-edit", f"end: {hook_event} hook"))
    if not file_path:
        sys.exit(0)

    # Skip non-source files
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()
    if ext not in _SOURCE_EXTS:
        sys.exit(0)

    # Skip plan files and configs that happen to have source extensions
    skip_patterns = (".claude/plans/", ".claude/settings")
    normalized = file_path.replace("\\", "/")
    if any(p in normalized for p in skip_patterns):
        sys.exit(0)

    # Skip edits that cannot produce a measurable compiled diff — header
    # files, #include-only changes, typedef/macro/global-only changes.
    code = _extract_edit_content(tool_name, tool_input)
    if _is_no_measurement_edit(code, ext):
        sys.exit(0)

    basename = os.path.basename(file_path)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"[loci] {basename} was modified. "
                "You MUST invoke the loci:loci-post-edit skill NOW — "
                "do not proceed to the next edit or respond to the user first. "
                "EXCEPTION: if this edit was made as part of a loci-plan pass "
                "(predictive measurement of a candidate function), do NOT invoke "
                "loci-post-edit — loci-plan will report the analysis itself."
            ),
        }
    }
    json.dump(output, sys.stdout)
    sys.exit(0)


if __name__ == "__main__":
    main()
