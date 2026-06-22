---
name: stack-depth
description: >
  Worst-case stack depth analysis for embedded C/C++/Rust: call-graph traversal,
  per-function frame sizes, recursion detection, and stack budget pass/fail from
   compiled .o or linked ELF binaries. Use when the user asks about stack sizing,
   stack overflow, task stack budgets, frame size impact, RAM optimization, hard
   faults, or sizing new RTOS tasks.
when_to_use: >
  When user asks about stack sizing, stack overflow, task stack budgets, frame
  size impact of a change, or RAM optimization in embedded/RTOS projects. Also
  when investigating hard faults or sizing new RTOS tasks.
---

# LOCI Stack Depth Analysis

Read these values from the LOCI session context (system-reminder block at session start) and substitute them wherever the placeholders appear below:
- `asm-analyze command: <path>` → use as `<asm-analyze-cmd>`
- `venv python: <path>` → use as `<venv-python>`
- `plugin dir: <path>` → use as `<plugin-dir>`
- `LOCI target: <arch>` → use as `<loci_target>` (one of `aarch64`, `armv7e-m`, `armv6-m`, `tc399`)

## Tool boundary: asm-analyze only — never objdump

All assembly and ELF inspection in this skill goes through `<asm-analyze-cmd>`.
Do **not** use `objdump`, `readelf`, `addr2line`, or `nm` as substitutes —
asm-analyze produces the call-graph and frame-size output binutils cannot.
If asm-analyze returns an error, surface it and stop; do not fall back to
objdump.

Always pass `--arch <loci_target>` on every asm-analyze call, reading the
value verbatim from the SessionStart `LOCI target:` line.

The practical workflow is: use `.o` for fast incremental checks on individual files
(did my change increase the frame?), use the linked ELF for full worst-case depth.

## Step 0: Check Session Context

Read architecture and compiler from the LOCI session context (the
`system-reminder` block emitted at session start). Look for:

```
Target: <target>, Compiler: <compiler>, Build: <build>
LOCI target: <loci_target>
```

Map the LOCI target to supported architectures:

| LOCI target | CPU |
|---|---|
| aarch64 | A53 |
| armv7e-m | CortexM4 |
| armv6-m | CortexM0P |
| tc399 | TC399 |

If the architecture is **not** in this table, emit and stop:

```
Supported: aarch64, armv7e-m, armv6-m, tc399
```

If no compiler was detected, inform the user and stop.

Do **not** re-run detection scripts — use the values already in the session context.

If the user provides their own binary (.elf, .out, .o, .axf), asm_analyze.py
auto-detects architecture from the ELF.

## Step 1: Identify Entry Functions and Stack Budgets

Determine which functions to analyze:

1. **User provides them** — e.g., "analyze stack depth for `TaskMain` with 2048-byte stack"
2. **Search RTOS config** — look for task creation calls:
   - FreeRTOS: `xTaskCreate(..., stackSize)`, `Task_construct(...)`
   - AUTOSAR: OS task configuration
   - `FreeRTOSConfig.h`, `ti_drivers_config.h`, linker scripts
3. **Auto-detect roots** — if no entry functions specified, the tool finds root functions
   (those not called by any other function in the binary)

Stack budget is optional. If provided, the tool reports usage as a percentage and
gives a pass/fail verdict against the threshold.

## Incremental Path — `.o` files (preferred for per-file checks)

Use this when checking if a change to a single file increased the stack frame.
Works on individual `.o` object files without needing a fully linked binary.

1. If a previous `.o` exists, save it as `.o.prev`
2. Compile only the changed source with `-c`.
   Always include `-g` to emit DWARF debug info (required by asm-analyze):
   ```
   <compiler> -g <flags> -c <source> -o .loci-build/<loci_target>/<basename>.o
   ```
3. Run stack depth on the new object file:
   ```
   <asm-analyze-cmd> stack-depth --elf-path .loci-build/<loci_target>/<basename>.o --entry-functions <func> --arch <loci_target>
   ```
4. Compare frame sizes before and after. If `.o.prev` exists, also run:
   ```
   <asm-analyze-cmd> stack-depth --elf-path .loci-build/<loci_target>/<basename>.o.prev --entry-functions <func> --arch <loci_target>
   ```
   Report the per-function frame size delta.

This gives fast feedback on whether a change grew the stack without needing a full link.

## Full ELF Path — linked binary (for worst-case depth)

Use this for full call-graph traversal to find the worst-case stack depth across
all call chains from a task entry point.

1. Cross-compile or use the existing linked binary
2. Run full stack depth analysis:
   ```
   <asm-analyze-cmd> stack-depth --elf-path <binary> --entry-functions <funcs> --arch <loci_target> [--stack-budget <bytes>] [--threshold <percent>]
   ```
   Optional flags:
   - `--stack-budget <bytes>` — configured stack size; enables usage % and verdict
   - `--threshold <percent>` — max allowed usage percentage (default 50)
   - `--max-recursion-depth <N>` — bound for recursive call estimation (default 1)
   - `--unknown-callee-size <bytes>` — assumed frame size for external/library functions (default 64)

The JSON output contains per-entry-function results with:
- `worst_case_depth` — total bytes along the deepest call path
- `worst_case_path` — list of function names along that path
- `average_depth` — mean depth across all leaf-terminating paths
- `per_function_frames` — frame size in bytes for each function
- `budget`, `threshold_pct`, `usage_pct`, `verdict` — only when `--stack-budget` is provided
- `warnings` — recursion, indirect calls, unknown callees
- `has_recursion`, `has_indirect_calls`, `has_unknown_callees` — boolean flags

## Step 2: Report Results

**Important:** Always include `Worst-case path` for every reported function — do not omit it even when reporting many functions. If the output would be long, limit the number of functions reported (e.g., top 10 by depth) but always show the complete report for each function you do include.

### Per-function report

For each entry function, report:

```
## Stack Depth: <FunctionName>

Worst-case depth:   <N> bytes
Worst-case path:    func_a → func_b → func_c → func_d
Average depth:      <M> bytes
Frame size:         <F> bytes (this function only)

Per-function frames along worst path:
  func_a:   32 bytes
  func_b:   64 bytes
  func_c:  128 bytes
  func_d:   88 bytes
```

### With stack budget (when --stack-budget provided)

```
Stack budget:       2048 bytes
Threshold (50%):    1024 bytes
Worst-case usage:   312 bytes (15.2%)
Verdict:            PASS
```

### Warnings

Flag any issues that affect accuracy:
- **Recursion detected**: `func_x calls itself — depth bounded to N iterations`
- **Indirect calls**: `func_y has indirect call (blr x8) — callee unknown`
- **Unknown callees**: `func_z not found in binary — assumed 64 bytes`

These warnings mean the reported depth is an estimate. Indirect calls and unknown
callees may undercount the real depth.

### Incremental comparison (when .o.prev available)

```
## Stack Frame Delta: <FunctionName>

Before:  48 bytes
After:   96 bytes
Delta:  +48 bytes (+100%)
```

## Conclusion table

After all per-function reports, emit a single aggregate conclusion table.
Include only rows that apply this run. Every ⚠️ / ❌ row MUST cite a concrete
reason in the Note column — no icon without a cause.

Icon vocabulary: ✅ PASS · ⚠️ WARNING · ❌ FAIL. 

### Row catalogue (order when present)

1. **Worst-case depth** — always, when at least one entry function was
   analyzed. Report the maximum `worst_case_depth` across all entry
   functions + absolute bytes. Status by budget:
   - ✅ `usage_pct < 50%` (or no budget and the number looks safe)
   - ⚠️ `50% ≤ usage_pct ≤ 80%`
   - ❌ `usage_pct > 80%`
   Note cites the entry function whose path produced the maximum.
2. **Worst-case path** — always, when worst-case depth row is present.
   Render the `func_a → func_b → func_c` chain in the Note column;
   status mirrors the Worst-case depth row.
3. **Largest frame** — only when a single frame is ≥ 25% of the total
   worst-case depth. Note cites function name + frame size in bytes.
   Status: ✅ unless the frame size itself is unusually large in absolute
   terms (e.g., >512 B on a small-MCU stack), in which case ⚠️.
4. **Recursion** — only if `has_recursion = true` for any entry function.
   Status: ⚠️ (bounded recursion, `--max-recursion-depth` applied) or ❌
   (unbounded — no exit condition visible). Note cites which function.
5. **Indirect calls** — only if `has_indirect_calls = true`. Status: ⚠️
   if the depth estimate is confident despite them, ❌ if an unknown
   callee could dominate the depth. Note cites the call site.
6. **Unknown callees** — only if `has_unknown_callees = true`. Status
   mirrors (5); Note cites the missing symbol and the fallback size used.

Table footer: bolded single-line verdict.
- With budget: `Verdict: **PASS** <usage_pct>%` · `**CAUTION** <usage_pct>%` · `**FAIL** <usage_pct>%`
- Without budget: `Verdict: **PASS** — worst-case <N> bytes` · or the CAUTION/FAIL equivalent if a concerning absolute size surfaced.

### Example

```
### Conclusion
| Gate              | Status | Note                                               |
|-------------------|:------:|----------------------------------------------------|
| Worst-case depth  |   ✅   | 312 B (15.2% of 2048 B budget)                      |
| Worst-case path   |   ✅   | TaskMain → process_data → decode → crypto_verify    |
| Largest frame     |   ⚠️   | decode: 128 B (41% of total)                        |

Verdict: **PASS** 15.2%
```

### Escalation fold-back

When stack-depth is invoked as an ESCALATION from loci-plan or
loci-post-edit, still emit the full Conclusion table above, AND hand back
to the parent skill a one-line summary in the form:
`stack: <worst_case_depth> B (<usage_pct>%) — <PASS|CAUTION|FAIL>`.
The parent skill folds that line into its own "Stack escalation" row.

## LOCI voice remark

Before the footer, add one short LOCI voice remark (max 15 words) that
acknowledges the user's work grounded in a specific number from the
analysis. Attribute improvements to the user ("clean work", "smart move",
"tight code"). For concerns, be honest and constructive with specifics.
Skip if the analysis produced no results or the user needs raw data only.

## LOCI footer

After emitting all per-function stack-depth reports and the voice
remark, append the footer as the last thing printed — **only if
N > 0**. If no functions were processed, do NOT emit the footer.

**Record cumulative stats + verdict** (run via Bash before rendering the footer).
Pass `--verdict "<verbatim-verdict-line>"` so the gate outcome ships alongside
the per-function trends payload on the next Stop-hook flush — the line is the
same string already rendered to chat in the conclusion-table footer
(`Verdict: PASS <usage>%`, `Verdict: CAUTION <usage>%`, `Verdict: FAIL <usage>%`,
or — when no `--stack-budget` was supplied — `Verdict: PASS — worst-case <N> bytes`).
Pass it unbolded, no surrounding asterisks.
```
<venv-python> <plugin-dir>/lib/loci_stats.py record --context-file "<project-context>" --skill stack-depth --functions <N> --api-calls 0 --co-reasoning 0 --verdict "<verbatim-verdict-line>"
```

**Record per-function measurements** (single Bash call for all entry functions).
Pipe all measurements as JSONL via stdin:
```
echo '<jsonl_records>' | <venv-python> <plugin-dir>/lib/loci_stats.py record-measurement --context-file "<project-context>" --stdin --skill stack-depth
```
Where `<jsonl_records>` is one JSON object per line for each entry function:
```
{"fn":"<func>","stack_b":<worst_case_depth>,"src":"<source_file>"}
```
Use the `worst_case_depth` value (in bytes) from the stack-depth JSON output.

Do NOT call `loci_stats.py summary` here. The cumulative branch-stats
line is deliberately removed from skill footers — it is available via
the `trends` skill when the user asks for it.

### Render the footer — compact by default

One line. Icon-led, no surrounding bars, middle-dot separators:

```
<icon> LOCI stack-depth · <entry-fn> · <worst> B (<usage>% budget)
```

- `<icon>` — mirrors the body's conclusion-table verdict: `✅` PASS,
  `⚠️` WARNING, `❌` FAIL.
- `<entry-fn>` — the single entry function when `N = 1`. When `N > 1`
  the compact form is `<N> fn, worst <max> B` (drops the usage % since
  budgets may differ per entry).
- `<worst>` — `worst_case_depth` in bytes.
- `<usage>` — usage as % of budget; omit the parenthetical entirely
  when no budget was supplied (`--stack-budget` not set).

Worked examples:
```
✅ LOCI stack-depth · BLEAppUtil_Task · 312 B (30% budget)
✅ LOCI stack-depth · main · 288 B
⚠️ LOCI stack-depth · sensor_task · 1620 B (79% budget)
```

### Fold-back to parent (escalation mode)

When stack-depth was invoked as an escalation from `plan` /
`post-edit`, emit the full footer as described above AND hand the
parent a one-line summary for fold-back:

```
stack: <worst_case_depth> B (<usage_pct>%) — <PASS|CAUTION|FAIL>
```

The parent skill renders its own compact or expanded footer based on
whether this fold-back was clean (see the clean-escalation suffix rule
in the `loci-plan` / `loci-post-edit` SKILL.md files).

### Expand when...

Replace the compact form with the expanded multi-line form if **any**
of the following is true:
- Verdict is `⚠️ WARNING` or `❌ FAIL`.
- Recursion or unknown-callee warnings make the reported depth a
  lower bound rather than a worst case (the engineer needs the
  inline warnings list).
- `N > 1` and at least one entry has usage ≥ 50% of its budget.

Expanded form:
```
─── LOCI · stack-depth ─────────────────
  <N> functions analyzed
  Verdict: <PASS | CAUTION | FAIL> — <one-line summary>
────────────────────────────────────────
```

The expanded form does **not** include the cumulative branch-stats line.

- **N** = unique entry functions analyzed via asm-analyze stack-depth
