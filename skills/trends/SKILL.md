---
name: trends
description: >
  Per-function measurement history on the current branch: timing, energy,
  stack, and memory trends over time from LOCI analysis.
when_to_use: >
  When user says "show trends", "optimization progress", "what changed on
  this branch", "how are my functions doing", "/trends". Also when user asks
  about performance trajectory or whether an optimization sprint is working.
---

# LOCI Trends

Read these values from the LOCI session context (system-reminder block at
session start) and substitute them wherever the placeholders appear below:
- `venv python: <path>` → use as `<venv-python>`
- `plugin dir: <path>` → use as `<plugin-dir>`

## Step 0: Check session context

Read the persisted detection results from the `<project-context>` path (the
per-session keyed file, listed as `project context:` in this session's
context). Extract `git_branch` for the report header.

If the file does not exist, stop and tell the user:

> LOCI session context not found. Please restart Claude Code so the plugin
> setup runs and detects the project environment.

## Step 1: Retrieve trend summary

Run via Bash:
```
<venv-python> <plugin-dir>/lib/loci_stats.py trend --context-file "<project-context>"
```

If the output is empty, respond with:

> No measurements on this branch.

Nothing more. Do not suggest running other skills or explain how to generate
measurements.

## Step 2: Render the report

If step 1 produced output, render it with a heading that includes the branch
name from step 0, followed by a one-line summary derived from the trend data:

```
## LOCI Trends: <branch_name>

**<N> functions tracked · <M> measurements · <K> improvements · <J> regressions · <B> baselines**

<trend output from step 1>
```

Computing the summary line:
- **N** = rows in the trend table (same as the "Branch summary: N functions tracked" line that `loci_stats.py trend` already prints — safe to copy verbatim).
- **M** = total measurements (sum of the Edits column, also present in the CLI's "Branch summary" line).
- **K** = count of rows with `Direction = improved`.
- **J** = count of rows with `Direction = regressed`.
- **B** = count of rows with `Direction = baseline`.

The table shows only columns that have data — timing from post-edit
auto-runs, stack from /stack-depth invocations, memory from /memory-report
invocations. No empty columns, no missing-data notices.

## Step 3: Single-function drill-down (optional)

If the user asks about a specific function, run:
```
<venv-python> <plugin-dir>/lib/loci_stats.py trend --context-file "<project-context>" --function <func_name>
```

Render the chronological output under a heading:
```
### <func_name>

<chronological output from above>
```

## LOCI voice remark

End the report with one short LOCI voice remark (max 15 words). The remark
should reinforce the value of measuring — help the user see why tracking
matters and nudge them to keep going.

When there are improvements or regressions, ground the remark in a specific
number:
- "3 functions faster since branch start. The data is paying off."
- "process_data down 25% from peak — you caught that early."
- "All stable. That's the baseline locked in for the next change."

When there are only baselines (single measurements), highlight what comes
next:
- "First measurements captured. Next edit shows the delta."
- "Baseline locked. Every future change gets measured against this."
- "1 function tracked. LOCI will show the impact of your next edit."

No footer separator lines after the remark.

Do NOT record stats for this skill — trends is a read-only view.
