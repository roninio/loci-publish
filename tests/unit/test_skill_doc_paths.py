"""Skill SKILL.md and session-init: `/tmp` path-policy must be unconditional.

Background: pre-0.1.70 the `/tmp` policy was framed as a Windows-only
constraint ("never `/tmp` on Windows", "Git Bash convenience path that
Windows-native Python (including the venv) cannot resolve"). On macOS
and Linux the LLM reasonably concluded `/tmp` was acceptable and
emitted commands like:

    asm_analyze.py extract-assembly ... > /tmp/loci_plan.json &&
    python -c "import json; json.load(open('/tmp/loci_plan.json'))..."

which trips Claude Code's out-of-project permission prompt and halts
plan/post-edit/eval automation regardless of OS.

This lint pins the OS-agnostic shape: every doc surface that mentions
`/tmp` (or `/var/tmp`) must carry an unconditional prohibition —
either uppercase `NEVER` or `never write|use` — within a small context
window around the mention. The OS qualifier alone (`on Windows`) is
not sufficient and was the root cause.
"""

from __future__ import annotations

import re
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = PLUGIN_ROOT / "skills"
SESSION_INIT = PLUGIN_ROOT / "hooks" / "session-init.sh"

DOC_FILES = sorted(SKILLS_DIR.rglob("SKILL.md")) + [SESSION_INIT]

# Matches `/tmp` or `/var/tmp` as a leading path segment. Excludes
# substrings like `*.tmp`, `tmpdir`, `build_tmp`, `${TMP}` etc.
TMP_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:tmp|var/tmp)\b")

# Unconditional prohibition. Lowercase "never" alone is not enough
# ("never on Windows" was the bug); we require either uppercase NEVER
# or the explicit verb-form "never write|use".
UNCONDITIONAL_RE = re.compile(r"\bNEVER\b|\bnever\s+(?:write|use)\b")

# Character window around each /tmp mention to scan for the rule.
# ~200 chars is roughly 2-3 wrapped lines of skill prose; tight enough
# that an unrelated "NEVER" elsewhere in the file can't accidentally
# satisfy the lint.
WINDOW_CHARS = 200


def test_tmp_path_policy_is_unconditional():
    offenders: list[tuple[str, int, str]] = []
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for m in TMP_PATH_RE.finditer(text):
            lo = max(0, m.start() - WINDOW_CHARS)
            hi = min(len(text), m.end() + WINDOW_CHARS)
            if not UNCONDITIONAL_RE.search(text[lo:hi]):
                lineno = text.count("\n", 0, m.start()) + 1
                offenders.append(
                    (str(path.relative_to(PLUGIN_ROOT)), lineno, m.group(0))
                )

    assert not offenders, (
        "/tmp (or /var/tmp) mentions must be bracketed by an unconditional "
        "prohibition — uppercase NEVER or 'never write|use' — within "
        f"{WINDOW_CHARS} chars. OS-conditional framing ('never on Windows') "
        "was the pre-0.1.70 root cause that let the LLM emit `> /tmp/...` "
        "on macOS/Linux, tripping Claude Code's permission prompt and "
        "halting automation:\n"
        + "\n".join(f"  {f}:{ln} -> {t}" for f, ln, t in offenders)
    )
