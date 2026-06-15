"""Skill SKILL.md lint: forbid stray flag tokens that no LOCI tool accepts.

Background: a stray `--verbose` reference in exec-trace/SKILL.md (cited
as an example of what the engineer might say to request expanded output)
sat in the same file as a documented `loci_stats.py record …` invocation.
An LLM reading the file conflated the two and emitted
`loci_stats.py record … --verbose`, which argparse rightly rejected with
exit code 2. The record never landed, the Stop-hook flush had nothing to
ship, and the dashboard stayed empty.

Skill SKILL.md files are instructions the LLM follows verbatim, so any
`--flag` token written there is a candidate for hallucination onto an
adjacent command. This test pins the set of forbidden flag tokens —
flags that aren't on any LOCI tool the skills invoke but are plausible
enough to look like one.
"""

from __future__ import annotations

import re
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = PLUGIN_ROOT / "skills"
LOCI_STATS = PLUGIN_ROOT / "lib" / "loci_stats.py"


# Flag tokens that have leaked into skill docs in the past and triggered
# hallucination onto loci_stats.py. Keep this list short and provable:
# each entry must (a) have caused or risk a real incident, and (b) not
# appear as a real flag on any tool a skill invokes (loci_stats.py,
# asm-analyze, git, etc.).
FORBIDDEN_TOKENS = {
    "--verbose",
}


def _extract_loci_stats_flags() -> set[str]:
    """Parse loci_stats.py source for every `add_argument("--xxx", ...)`
    call. Used to sanity-check FORBIDDEN_TOKENS does not collide with a
    real flag on the script the bug actually hit."""
    text = LOCI_STATS.read_text(encoding="utf-8")
    pat = re.compile(r"""add_argument\(\s*['"](--[A-Za-z][A-Za-z0-9_-]*)['"]""")
    return set(pat.findall(text))


def test_forbidden_tokens_are_not_real_loci_stats_flags():
    """The forbidden list must not contain a flag loci_stats.py actually
    accepts — otherwise the lint would block a legitimate documented use."""
    real = _extract_loci_stats_flags()
    collision = FORBIDDEN_TOKENS & real
    assert not collision, (
        f"FORBIDDEN_TOKENS contains real loci_stats.py flags: {collision}. "
        "Remove from FORBIDDEN_TOKENS or rename the argparse argument."
    )


def test_skill_docs_do_not_reference_forbidden_flags():
    """No skill SKILL.md may contain a forbidden flag token. Pre-fix,
    exec-trace/SKILL.md cited `--verbose` as a hypothetical engineer
    request — the LLM picked it up and emitted it onto an adjacent
    loci_stats.py invocation, breaking the dashboard pipeline."""
    offenders: list[tuple[str, int, str]] = []
    for skill_md in sorted(SKILLS_DIR.rglob("SKILL.md")):
        for lineno, line in enumerate(
            skill_md.read_text(encoding="utf-8").splitlines(), 1
        ):
            for tok in FORBIDDEN_TOKENS:
                # Whole-token match: prevent `--verbose-mode` style false hits.
                if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(tok)}(?![A-Za-z0-9_-])", line):
                    offenders.append(
                        (str(skill_md.relative_to(PLUGIN_ROOT)), lineno, tok)
                    )

    assert not offenders, (
        "Skill SKILL.md files contain forbidden flag tokens that no LOCI "
        "tool accepts. An LLM reading the file may hallucinate them onto "
        "an adjacent command (this is how `loci_stats.py --verbose` "
        "shipped and broke the telemetry pipeline). Rephrase the doc to "
        "avoid the token:\n"
        + "\n".join(f"  {f}:{ln} -> {t}" for f, ln, t in offenders)
    )
