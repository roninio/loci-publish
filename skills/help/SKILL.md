---
name: help
description: >
  Quick-reference guide to LOCI — shows available skills, environment status,
  and troubleshooting for build environment and LOCI API key / connection issues.
  Use when the user asks what LOCI can do, how to use LOCI, available commands,
  setup help, or types /help.
when_to_use: >
  When user asks for help with LOCI, what LOCI can do, how to use LOCI,
  available commands, or types /help. Also when user seems confused about
  LOCI setup or capabilities.
---

# LOCI Help

Show the user their environment status, available skills, and a contextual
next step. Adapt the output based on what is actually working vs missing.

## Step 0: Diagnose Environment

Read the LOCI session context from the `system-reminder` block emitted at
session start:

```
Target: <target>, Compiler: <compiler>, Build: <build>
LOCI target: <loci_target>
asm-analyze command: <path>   ← absent when venv not ready
venv python: <path>
plugin dir: <path>
```

Capture `venv python: <path>` as `<venv-python>` and `plugin dir: <path>` as
`<plugin-dir>` — needed for the Stats Footer below.

Classify the environment into one of three states:

| State | How to detect | Priority |
|-------|---------------|----------|
| **No API key** | Neither `$LOCI_API_KEY` nor `.loci/config.json` (key `LOCI_API_KEY`) has a non-empty token | Check first |
| **No build env** | Target = `unknown` OR Compiler = `unknown` in session context | Check second |
| **Ready** | Target and Compiler are both known, and a `LOCI_API_KEY` is reachable | Default |

A session can be in multiple degraded states simultaneously (no build env AND
no API key). Report all that apply.

## Step 1: Show Environment Status

Based on Step 0, render the appropriate status block.

### When fully ready

```
## Environment
  Target:    <loci_target> (<mapped CPU name>)
  Compiler:  <compiler>
  Build:     <build_system>
  API key:   present (<env | .loci/config.json>)
```

Daily token quota is enforced server-side and reported when you actually run
an analysis (the helper exits with a "Daily token limit reached" message and
reset countdown on HTTP 429). Tier limits for reference: free 30,000 /
premium 300,000 / enterprise 1,500,000 daily tokens.

Map LOCI target to CPU name:

| LOCI target | CPU |
|---|---|
| aarch64 | A53 |
| cortexm / armv7e-m | Cortex-M4 |
| armv6-m | Cortex-M0+ |
| tricore / tc399 | TC399 |

### When build environment is missing

```
## Environment — setup needed

LOCI didn't detect a build environment in this directory.

To get started:
1. `cd` into a C/C++/Rust project with source files
2. Ensure a cross-compiler is installed:
   - ARM Cortex-M: `arm-none-eabi-gcc`
   - ARM Cortex-A: `aarch64-linux-gnu-gcc`
   - TriCore: `tricore-elf-gcc`
3. Restart Claude Code so LOCI can auto-detect the project

Or point LOCI at an existing binary directly:
  "What's the execution cost of main() in path/to/firmware.elf?"
```

### When the API key is missing

```
## Environment — LOCI API key needed

LOCI's timing and energy analysis requires a LOCI API key.

→ Either export it:        export LOCI_API_KEY=sk-loci-...
  Or create .loci/config.json in this directory:
                           { "LOCI_API_KEY": "sk-loci-..." }

The key is read at call time by the HTTP API helper — no restart needed.

Skills that work without an API key: /stack-depth, /memory-report, /control-flow
Skills that need an API key:         /exec-trace, loci-plan, loci-post-edit
```

## Step 2: Show Available Skills

Always show the full skill list regardless of environment state — users
should know what's possible even if their setup isn't complete yet.

```
## On-demand skills

  /exec-trace      Timing & energy from real silicon traces
                   "What's the execution cost of main()?"

  /stack-depth     Worst-case stack depth & budget check
                   "Is my stack safe for TaskMain with 2048 bytes?"

  /memory-report   ROM/RAM breakdown from ELF/map files
                   "How much ROM/RAM does my build use?"

  /control-flow    Annotated control-flow graphs
                   "Show me the call graph for process_data()"

## Auto-running (no command needed)

  loci-plan        Runs in /plan — checks call graph, timing, energy, execution fit
                   Escalates to /stack-depth or /memory-report when needed
                   Verdict: GOOD / ADJUST PLAN / STOP

  loci-post-edit   Runs after edits — diffs binary, reports timing/energy % delta
                   Verdict: OK / CAUTION / FLAG (proposes fix on FLAG)
```

## Step 3: Contextual Next Step

Based on the environment state from Step 0, suggest a single next action:

- **Ready + ELF files exist in project**: "You have compiled binaries — try asking about timing for a specific function, or run `/memory-report` for a full ROM/RAM breakdown."
- **Ready + no ELF files**: "Compile your project first, then ask about timing or stack depth for a specific function."
- **No build env**: "Navigate to your C/C++/Rust project directory and restart Claude Code, or point me at a `.elf`, `.o`, or `.axf` file directly."
- **No API key**: "Set `LOCI_API_KEY` (env var or `.loci/config.json`) to unlock timing and energy analysis."

If multiple issues exist, prioritize the API key first (it's the quicker fix),
then build environment setup.

## Stats Footer

After rendering all help output, run via Bash:
```
<venv-python> <plugin-dir>/lib/loci_stats.py global-summary
```

If output is non-empty, append it as the last line — no heading, just the
stats line. If empty (first-time user), show nothing.

Do NOT record stats for this skill — help is informational only.

