"""Skill SKILL.md lint: advertised slash commands must be reachable.

Background: pre-0.1.74, `skills/exec-trace/SKILL.md` and
`skills/control-flow/SKILL.md` both carried `disable-model-invocation:
true`. The flag suppresses the skill from BOTH the model's
auto-invocation set AND the user-facing slash-command set, so neither
`/exec-trace` nor `/control-flow` was actually reachable — even though
`hooks/session-init.sh` and `skills/help/SKILL.md` advertised them as
on-demand commands.

The user-visible failure was empty impact dashboards. The impact
pipeline (function_measurements / analysis_results / loci_skill_runs)
is fed by skills writing measurements to local JSONL via
`loci_stats.py record-measurement`; when the skill never runs, the
JSONL stays empty and the Stop-hook flush has nothing to ship. For
"user asks for timing of an existing function" requests the matching
skill is exec-trace, so its unreachability mapped 1:1 to missing
dashboard data.

The fix removes the flag (mirroring the stack-depth fix in PR #50:
"Enable stack-depth as user-invocable skill"). This lint pins the
contract: any skill whose `/<name>` slash command is advertised in
session-init.sh or help/SKILL.md must not carry the flag.
"""

from __future__ import annotations

import re
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = PLUGIN_ROOT / "skills"
SESSION_INIT = PLUGIN_ROOT / "hooks" / "session-init.sh"
HELP_SKILL = SKILLS_DIR / "help" / "SKILL.md"


# Matches a /<skill-name> slash command in plugin docs. The skill name
# must match a real directory under skills/ — that's how we filter out
# unrelated forward-slash strings (e.g. "/tmp", "/plan").
_SLASH_CMD_RE = re.compile(r"/([a-z][a-z0-9-]*)")


def _existing_skill_dirs() -> set[str]:
    return {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}


def _advertised_slash_commands() -> set[str]:
    """Skills referenced as `/<name>` in user-facing docs.

    Source surfaces:
      - hooks/session-init.sh — the SessionStart context block
      - skills/help/SKILL.md  — the on-demand skills list

    Anything advertised here must be invocable. Anything else (e.g.
    skills only invoked transitively by another skill) can carry the
    `disable-model-invocation` flag without breaking a user promise.
    """
    skill_dirs = _existing_skill_dirs()
    found: set[str] = set()
    for path in (SESSION_INIT, HELP_SKILL):
        text = path.read_text(encoding="utf-8")
        for m in _SLASH_CMD_RE.finditer(text):
            name = m.group(1)
            if name in skill_dirs:
                found.add(name)
    return found


def _has_disable_model_invocation(skill_md: Path) -> bool:
    """True iff the skill's YAML frontmatter sets the flag truthy.

    Frontmatter is the leading `---`-delimited block. We only inspect
    that block so a stray mention in the prose body (e.g. quoting the
    flag name in a comment) does not count as enabling it.
    """
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    frontmatter = text[3:end]
    for line in frontmatter.splitlines():
        m = re.match(r"\s*disable-model-invocation\s*:\s*(\S+)", line)
        if m and m.group(1).lower() in ("true", "yes", "on"):
            return True
    return False


def test_advertised_slash_commands_are_invocable():
    advertised = _advertised_slash_commands()
    assert advertised, (
        "Expected at least one advertised /<skill> command in "
        "session-init.sh or help/SKILL.md; got none. The regex or "
        "the doc surfaces probably drifted."
    )

    offenders: list[str] = []
    for name in sorted(advertised):
        skill_md = SKILLS_DIR / name / "SKILL.md"
        if not skill_md.exists():
            continue  # `_advertised_slash_commands` already filters,
                       # but be defensive against rename races.
        if _has_disable_model_invocation(skill_md):
            offenders.append(name)

    assert not offenders, (
        "Skills advertised as `/<name>` slash commands in "
        "hooks/session-init.sh and/or skills/help/SKILL.md but suppressed "
        "by `disable-model-invocation: true` in their frontmatter. The "
        "flag hides the skill from both model auto-invocation and the "
        "user-facing slash-command set, so the advertised command does "
        "not actually work. Either remove the flag (matching PR #50, the "
        "stack-depth enablement) or stop advertising the command. "
        "Offenders:\n"
        + "\n".join(f"  skills/{n}/SKILL.md" for n in offenders)
    )
