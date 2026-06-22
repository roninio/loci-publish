---
name: bug-report
description: >
  Forensic diagnostic report for LOCI — collects environment state, runs health
  checks, and writes a timestamped report when analysis fails or doesn't trigger.
when_to_use: >
  When user says "bug report", "LOCI isn't working", "exec-trace didn't run",
  "skill didn't trigger", "MCP not connecting", "results are wrong",
  "results missing", "generate diagnostic", "something is broken",
  "debug LOCI", or any LOCI failure the user wants investigated.
argument-hint: "[description of what failed]"
---

# LOCI Bug Report

Generate a forensic diagnostic report when LOCI analysis fails, a skill does
not invoke, or results are missing or invalid. The report is written to a
timestamped `.md` file that can be shared or loaded into a future Claude Code
session to diagnose and fix the issue.

This skill must work even when LOCI is completely broken. Do NOT invoke the
LOCI HTTP API or MCP auth tools for collection (they may be the thing that's
broken). Use only: Read, Bash, Glob, Grep. Check 1 *records* whether a
`LOCI_API_KEY` is reachable — it does not call the API.

Timing/energy now runs through the LOCI HTTP API (`<plugin-dir>/lib/api_client.py`,
endpoint `https://mcp.auroralabs.com/mcp/v1/get_assembly_block_exec_behavior`,
bearer `LOCI_API_KEY`), not the legacy MCP exec-behavior tool. The MCP server
in `.mcp.json` remains only for the authentication flow.

Read these values from the LOCI session context (system-reminder block at
session start) and substitute them wherever the placeholders appear below:
- `asm-analyze command: <path>` → use as `<asm-analyze-cmd>`
- `venv python: <path>` → use as `<venv-python>`
- `plugin dir: <path>` → use as `<plugin-dir>`
- `project context: <path>` → use as `<project-context>`
- `loci version: <semver>` → use as `<plugin-version>`

If `plugin dir:` is not in the session context, fall back to the
`CLAUDE_PLUGIN_ROOT` environment variable. If neither is available, stop and
tell the user: "Cannot locate LOCI plugin directory. Ensure the plugin is
installed and restart Claude Code."

## Persistent layout (since 0.1.66)

The venv, setup marker, and state files live outside the versioned plugin
cache so they survive plugin upgrades:

| Path | Purpose | Fallback |
|------|---------|----------|
| `$LOCI_VENV_DIR` (typically `~/.loci/venv`) | Python 3.12 venv with asmslicer | `<plugin-dir>/.venv` |
| `$LOCI_VENV_DIR/.setup-complete` | Setup marker (sha256 fingerprint of requirements.txt) | `<plugin-dir>/.venv/.setup-complete` |
| `$LOCI_STATE_DIR` (typically `<cwd>/.loci/state`) | project-context, measurements, stats, loci-paths | `~/.loci/state`, then `<plugin-dir>/state` |
| `~/.loci/impact-token.json` | per-user telemetry token | — |

The plugin exports `LOCI_VENV_DIR` and `LOCI_STATE_DIR` at session start, but
that env does NOT propagate to skill Bash calls — so resolve the venv with
`${LOCI_VENV_DIR:-$HOME/.loci/venv}` (shared, user-scoped) and the state dir
with `${LOCI_STATE_DIR:-$(pwd)/.loci/state}` (project-local), which matches the
language-level defaults every consumer uses.

## Step 0: Capture user description

The skill accepts an optional argument string describing the problem.
Store it as `<user-description>`.

If no argument was provided, ask the user in one sentence:
"What did you expect LOCI to do, and what happened instead?"

## Step 1: Collect environment snapshot

Run these in parallel where possible via Bash and Read:

1. **Claude Code version** — `claude --version 2>/dev/null || echo "unknown"`
2. **Claude model** — read from your own system prompt (e.g. `claude-opus-4-7`,
   `claude-sonnet-4-6`). Record the exact model ID.
3. **Plugin version** — prefer `<plugin-version>` from session context. If
   missing, read `<plugin-dir>/.claude-plugin/plugin.json` and extract
   `.version` with `jq -r '.version'`. Fall back to "unknown".
4. **OS info** — `uname -a`
5. **OS short name** — `uname -s | tr '[:upper:]' '[:lower:]'` (for filename)
6. **Project context** — Read `<project-context>` (the per-session keyed file
   listed as `project context:` in this session). Record the full JSON. If
   missing, record "MISSING".
7. **LOCI paths** — Read `${LOCI_STATE_DIR:-$(pwd)/.loci/state}/loci-paths.json`.
   Fall back to `<plugin-dir>/state/loci-paths.json` for very old installs.
   If missing, record "MISSING".
8. **Setup marker** — `cat "${LOCI_VENV_DIR:-$HOME/.loci/venv}/.setup-complete" 2>/dev/null \
   || cat "<plugin-dir>/.venv/.setup-complete" 2>/dev/null || echo "MISSING"`.
   The marker contains a sha256 fingerprint of `requirements.txt`, not a
   plugin version — a stale plugin version is not a setup failure.
9. **Git info** — `git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown"`
   and `git log --oneline -3 2>/dev/null || echo "no git history"`
10. **Hooks config** — Read `<plugin-dir>/hooks/hooks.json`. If missing,
    record "MISSING".
11. **MCP config** — Read `<plugin-dir>/.mcp.json`. Record the configured
    auth server URL. If missing, record "MISSING".
12. **API key reachable** — record whether a `LOCI_API_KEY` is resolvable:
    `$LOCI_API_KEY` non-empty, OR `.loci/config.json` in the working
    directory has a non-empty `LOCI_API_KEY`. Do NOT print the token value —
    record only "present (env)", "present (.loci/config.json)", or "MISSING".

## Step 2: Run 13-point diagnostics checklist

For each check, record status (PASS / FAIL) and a detail string.

| # | Check | How to test | PASS when |
|---|-------|-------------|-----------|
| 1 | LOCI_API_KEY reachable | `$LOCI_API_KEY` is non-empty, OR `.loci/config.json` in the working dir has a non-empty `LOCI_API_KEY`. Record the source, never the value. | A token is resolvable from either source |
| 2 | Session context exists | `<project-context>` (keyed file) exists and contains `project_root` | File exists with key |
| 3 | Compiler detected | `compiler` field in `<project-context>` is not `unknown` or empty | Has a value |
| 4 | Architecture detected | `architecture` field in `<project-context>` is not `unknown` or empty | Has a value |
| 5 | LOCI target supported | `loci_target` in `<project-context>` is one of: `aarch64`, `armv7e-m`, `armv6-m`, `tc399` | Value in set |
| 6 | Python venv working | `<venv-python> --version` exits 0 AND reports Python 3.12.x | Exit code 0 and major.minor = 3.12 |
| 7 | asm-analyze installed | `<venv-python> -c "from loci.service.asmslicer import asmslicer"` exits 0 | Exit code 0 |
| 8 | Setup complete | `${LOCI_VENV_DIR:-$HOME/.loci/venv}/.setup-complete` exists (or fallback `<plugin-dir>/.venv/.setup-complete`) | File exists and is non-empty |
| 9 | Build artifacts exist | Glob for `.loci-build/**/*.o` or any `.elf`/`.o`/`.axf` in project root | At least one found |
| 10 | c++filt available | Read `cxxfilt_dir` from `loci-paths.json`, run `<cxxfilt_dir>/c++filt --version` (or `.cmd`/`.exe` on Windows) | Exit code 0 |
| 11 | session-init executable | `test -x <plugin-dir>/hooks/session-init.sh` | Exit code 0 |
| 12 | hooks.json valid | `<plugin-dir>/hooks/hooks.json` parses with `jq .` | Valid JSON |
| 13 | HTTP API endpoint reachable | `curl -s -o /dev/null -w '%{http_code}' --max-time 8 -X POST https://mcp.auroralabs.com/mcp/v1/get_assembly_block_exec_behavior` — any HTTP status (even 401/400) proves the host/TLS path is reachable. This sends no key and no payload. | A non-empty HTTP status code comes back (network/TLS reached the server) |

If `<venv-python>` is unavailable, checks 6 and 7 automatically FAIL.
If `loci-paths.json` is missing, check 10 automatically FAILs.

Check 13 tests transport reachability only — it does NOT verify the key or
quota. Quota is enforced server-side and surfaced at analysis time as
`api_client.py` exit code 5 (HTTP 429); a missing/rejected key surfaces as
exit 3/4. If `curl` is unavailable, record check 13 as
"skipped: curl not available".

## Step 3: Collect stats

Run via Bash (skip if venv is broken):
```
<venv-python> <plugin-dir>/lib/loci_stats.py summary --context-file "<project-context>"
<venv-python> <plugin-dir>/lib/loci_stats.py global-summary
```

Record output or "stats unavailable — venv not working".

## Step 4: Reasoning — common failure forensics

This is the most important section. Analyze the session context and
diagnostics to determine what went wrong. Write this as free-form reasoning
(not templated) so it captures the actual session state.

### A. Skill Not Invoked

If the user's issue is that a LOCI skill should have triggered but didn't,
investigate:

1. **Prompt match** — compare the user's original prompt against the
   `when_to_use` triggers for each relevant skill. List the trigger keywords
   from the SKILL.md and note which matched or didn't.

2. **Auto-run conditions** — for auto-triggered skills:
   - `loci-post-edit`: Was the edited file a C/C++/Rust source
     (.c, .cc, .cpp, .cxx, .h, .hpp, .hxx, .rs)? Was an Edit/Write/MultiEdit
     tool used?
   - `loci-plan`: Was Claude in `/plan` mode when the user described
     new logic?

3. **Skill visibility** — is the skill listed in the `Available:` line of the
   session-reminder? Currently expected:
   `/help, /exec-trace, /stack-depth, /memory-report, /control-flow, /bug-report`.
   If not, session-init may not have registered it.

4. **Deferred tools** — check if `loci:loci-post-edit`, `loci:loci-plan`,
   `loci:trends`, etc. appear in the system-reminder available skills list.
   If absent, the plugin may not be loaded.

5. **Competing behavior** — did Claude answer directly instead of invoking the
   skill? Did another skill or tool pre-empt? Note what Claude did instead.

### B. Results Not Evaluated or Not Valid

If a skill ran but produced no results, wrong results, or results that weren't
used, investigate:

1. **Compilation** — did the compilation step succeed? Look for compiler errors,
   missing headers, wrong flags. Check if the compiler from `<project-context>`
   is actually installed: `which <compiler>`.

2. **asm-analyze output** — did `extract-assembly` or `extract-cfg` produce
   valid JSON? Common failures: function name not found in binary, architecture
   mismatch between ELF and LOCI target, empty output. If `json.load` on the
   output file fails at position 0 ("Expecting value: line 1 column 1"), a
   third-party library leaked non-JSON bytes to stdout during the analysis.
   Re-run with `LOCI_DEBUG=1` (the CLI captures stdout during analysis and
   forwards any captured content to stderr in debug mode) to see the leaked
   text. On Windows, also confirm the caller did not merge streams with
   `2>&1 > file` — stderr diagnostics before the JSON would produce the same
   symptom.

3. **HTTP API response** — did `api_client.py exec-behavior` return
   timing/energy data? Branch by exit code: 3 = no `LOCI_API_KEY` (env or
   `.loci/config.json`); 4 = HTTP non-2xx (401/403 rejected key, 5xx server
   error — body on stderr); 5 = quota/429 (daily token limit); 6 =
   network/transport (DNS, TLS, timeout). Confirm the endpoint host
   `https://mcp.auroralabs.com/mcp/v1` is reachable (the host was relocated
   from `loci.auroralabs.com` in 0.1.66; an outdated plugin install may still
   reference the old host).

4. **Result parsing** — were `timing_csv_chunks`, `timing_architecture`, or
   `execution_time_ns` fields present in the output? If asm-analyze returned
   data but Claude didn't use it, note the gap.

5. **Delta comparison** — for post-edit: did `.o.prev` exist before the
   recompile? Did `diff-elfs` return 0 changed functions (meaning the binary
   didn't actually change)?

6. **Output suppression** — did Claude generate analysis but fail to present
   it? (Context window pressure, interrupted response, tool call error.)

### C. Venv survived plugin upgrade?

Since 0.1.66 the venv lives at `~/.loci/venv` rather than inside the
versioned plugin cache. If the user just upgraded the plugin and analysis
broke, check:

- Does `~/.loci/venv/bin/python` (or `Scripts\python.exe` on Windows) exist?
- Does it report Python 3.12.x?
- Does the setup-marker fingerprint match `sha256(requirements.txt)`?
  Compute it with `sha256sum <plugin-dir>/requirements.txt | cut -c1-16` and
  compare to the contents of `${LOCI_VENV_DIR}/.setup-complete`. A mismatch
  means session-init will rebuild the venv on the next start.

### D. Root cause

Based on the diagnostics and reasoning above, state the root cause. Use the
dependency chain to find the most upstream failure:

```
hooks → setup → venv → asm-analyze → project-context → LOCI_API_KEY → HTTP API → compilation → analysis
```

If all 13 checks pass, the issue is likely:
- Skill trigger wording mismatch (Claude didn't recognize the intent)
- Transient HTTP API timeout
- A bug in the skill logic itself

## Step 5: Write report file

Determine the output filename:
```
report-<YYYY-MM-DD>-<os-short>.md
```

Write the file to the current working directory using this structure:

```markdown
# LOCI Diagnostic Report

Generated: <YYYY-MM-DD HH:MM:SS UTC>

## Versions

| Component | Version |
|-----------|---------|
| Claude Code | <claude --version output> |
| Claude model | <model ID, e.g. claude-opus-4-7> |
| LOCI plugin | <plugin version from plugin.json> |
| OS | <uname -a output> |

## User Description

<user-description>

## Environment

| Field | Value |
|-------|-------|
| Project root | <project_root or cwd> |
| Git branch | <branch> |
| Compiler | <compiler or "unknown"> |
| Build system | <build_system or "unknown"> |
| Architecture | <architecture or "unknown"> |
| LOCI target | <loci_target or "unknown"> |
| LOCI_API_KEY | <present (env) / present (.loci/config.json) / MISSING> |
| HTTP API endpoint | https://mcp.auroralabs.com/mcp/v1/get_assembly_block_exec_behavior |
| MCP auth URL | <url from .mcp.json> |
| asm-analyze | <command path or "unavailable"> |
| venv python | <path or "unavailable"> |
| LOCI_VENV_DIR | <resolved path> |
| LOCI_STATE_DIR | <resolved path> |

## Diagnostics Checklist

| # | Check | Status | Detail |
|---|-------|--------|--------|
| 1 | LOCI_API_KEY reachable | <PASS/FAIL> | <detail> |
| 2 | Session context exists | <PASS/FAIL> | <detail> |
| 3 | Compiler detected | <PASS/FAIL> | <detail> |
| 4 | Architecture detected | <PASS/FAIL> | <detail> |
| 5 | LOCI target supported | <PASS/FAIL> | <detail> |
| 6 | Python venv working | <PASS/FAIL> | <detail> |
| 7 | asm-analyze installed | <PASS/FAIL> | <detail> |
| 8 | Setup complete | <PASS/FAIL> | <detail> |
| 9 | Build artifacts exist | <PASS/FAIL> | <detail> |
| 10 | c++filt available | <PASS/FAIL> | <detail> |
| 11 | session-init executable | <PASS/FAIL> | <detail> |
| 12 | hooks.json valid | <PASS/FAIL> | <detail> |
| 13 | HTTP API endpoint reachable | <PASS/FAIL> | <detail, e.g. "HTTP 401 (reachable, key not sent)" or "could not connect — DNS/TLS"> |

**Result: <N>/13 checks passed.**

## Reasoning

### What the user was trying to do
<describe the intent and expected behavior>

### What should have happened
<which skill should have triggered, with trigger conditions from when_to_use>

### What actually happened
<what Claude did instead — answered directly, wrong skill, error, silence>

### Why it failed
<root cause reasoning chain, referencing specific checklist failures>

### Skill trigger analysis
<for each relevant skill, did the trigger conditions match?>

## Diagnosis

**Root cause:** <one-sentence root cause>

**Contributing factors:** <any additional FAIL checks>

**Suggested fix:**
<numbered actionable steps to resolve>

## Stats

### Branch stats
<loci_stats.py summary output, or "no stats recorded">

### Global stats
<loci_stats.py global-summary output, or "no stats recorded">

## Raw Data

<details>
<summary>project-context.json</summary>

```json
<sanitized contents or "MISSING">
```
</details>

<details>
<summary>loci-paths.json</summary>

```json
<sanitized contents or "MISSING">
```
</details>

<details>
<summary>hooks.json</summary>

```json
<sanitized contents or "MISSING">
```
</details>

<details>
<summary>.mcp.json</summary>

```json
<sanitized contents or "MISSING">
```
</details>

<details>
<summary>.setup-complete</summary>

<sanitized contents or "MISSING">
</details>

<details>
<summary>Recent git log</summary>

<git log --oneline -3 output>
</details>
```

### Redaction

Before embedding any file contents in the Raw Data section above, sanitize
them in-memory:

1. **Secrets** — replace values matching common secret patterns (API keys,
   tokens, passwords, `Bearer ...`, `Authorization: ...`, private key blocks,
   the `token` field inside `impact-token.json`) with `[REDACTED]`.
2. **Home paths** — replace the user's home directory prefix
   (`/Users/<name>/`, `/home/<name>/`, `C:\Users\<name>\`) with `~/`.

Apply substitutions BEFORE writing the report. Do NOT write unsanitized
contents and edit afterward.

## Step 6: Present summary to user

After writing the report file, display a concise summary:

```
## LOCI Diagnostic Summary

<N>/13 checks passed.

**Root cause:** <one-sentence diagnosis>

**Suggested fix:**
<numbered steps>

Share this file when reporting issues, or open it in a new Claude Code
session for further investigation.

─── LOCI · bug-report ─────────────────
  Report: <absolute-path-to-report-file>
────────────────────────────────────────
```

The report file path MUST appear in the footer as the last visible output.
Use the absolute path so the user can copy-paste it directly.

Do NOT record stats for this skill (diagnostic/informational only).
Do NOT emit a LOCI voice remark (inappropriate for failure context).
