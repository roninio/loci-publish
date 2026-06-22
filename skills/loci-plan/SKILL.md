---
name: loci-plan
description: >
  Execution-aware plan analysis (control-flow, timing/energy) on the
  functions an edit touches and the callees of any new code, using compiled
  artifacts, to catch problems while the design is still cheap to change.
when_to_use: >
  MANDATORY in /plan mode when user describes new logic or a modification.
  Triggers: "implement", "add", "write a function", "new feature", "how
  should I", "modify", "refactor", "guard". Do NOT invoke for review/explain
  requests or direct edits outside plan mode.
---

# loci-plan

This skill is a **thinking tool, not a write-gate**. Run it during planning —
while you are still deciding what to write — so the execution fit is visible
before any code changes. The output shapes how you write, not just whether.

**Plan analysis requires compiled artifacts.** It does not fall back to source-level
reasoning. If the project cannot be compiled or the architecture is not
supported, the skill stops and tells the user why.

## Tool boundary: asm-analyze only — never objdump

All assembly, CFG, symbol, and ELF inspection in this skill goes through
`<asm-analyze-cmd>`. Do **not** use `objdump`, `readelf`, `addr2line`, or
`nm` as substitutes — asm-analyze produces the annotated CFG and per-block
CSV the LOCI HTTP API expects, and binutils output is not equivalent. If
asm-analyze returns an error, surface it and stop; do not fall back to
objdump.

Always pass `--arch <loci_target>` on every asm-analyze call, reading the
value verbatim from the SessionStart `LOCI target:` line.

## When to run

Run the plan analysis as part of forming your plan, immediately after you understand
what function(s) you need to write and before you issue any Edit/Write call:

1. User describes the task
2. You read the relevant files to understand the call site and surrounding code
3. **← run the plan analysis here, while thinking**
4. Adjust the plan based on findings
5. Write the code

**Plan mode:** Always emit the full plan report (Execution, CFG Analysis,
Execution fit, footer) in the **response text** — never inside the plan body.
The plan body should contain only the adjusted implementation steps that
incorporate plan findings. The user must see the complete structured
report in the response, not a summary buried in the plan context.

## LOCI HTTP API — replaces the legacy MCP tool

All exec-behavior (timing/energy) calls go through the HTTP API, not the MCP
tool. Use the helper at `<plugin-dir>/lib/api_client.py` (stdlib `urllib`, no
extra deps). Do NOT call
`mcp__plugin_loci_loci__get_assembly_block_exec_behavior` — that path is
retired.

**Endpoint** — `POST https://mcp.auroralabs.com/mcp/v1/get_assembly_block_exec_behavior`
**Auth** — `Authorization: Bearer <LOCI_API_KEY>`. The helper resolves the
token in this order (first hit wins):
  1. `$LOCI_API_KEY` environment variable.
  2. `.loci/config.json` in the **current working directory**, key
     `"LOCI_API_KEY"`.
Do not embed the token in any command line or commit it to the repo; the
helper keeps it out of process argv.

**Helper invocation** — one chunk per call, CSV chunk on stdin:

```
echo "<chunk>" | <venv-python> <plugin-dir>/lib/api_client.py exec-behavior \
    --architecture <api_arch>
```

`<api_arch>` is the `timing_architecture` field from extract-assembly
(`A53`, `CortexM4`, `CortexM0P`, `TC399`). When a chunk is large, write it
to a project-local file (NEVER `/tmp/`) and pass `--csv-file`.

**Exit codes** (the skill must branch on these — see the degradation rules):

| Exit | Meaning |
|---|---|
| 0 | Success — CSV on stdout |
| 3 | No `LOCI_API_KEY` found (neither in env nor `.loci/config.json`) |
| 4 | HTTP non-2xx (auth, server error); body on stderr |
| 5 | Quota / rate limit (HTTP 429); body on stderr verbatim |
| 6 | Network/transport error (DNS, TLS, timeout) |

## Step 0: Check session context

**Pre-flight token check** — before any analysis, verify a token is reachable
from at least one source:

```
test -n "$LOCI_API_KEY" \
  || jq -e 'has("LOCI_API_KEY") and (.LOCI_API_KEY | length > 0)' .loci/config.json >/dev/null 2>&1 \
  || echo "no LOCI_API_KEY in env or .loci/config.json"
```

If neither source has a token, stop and tell the user:

> No `LOCI_API_KEY` found. Either export it
> (`export LOCI_API_KEY=...`) or create `.loci/config.json` in this
> working directory with `{"LOCI_API_KEY": "..."}`. The token is read
> at call time by the HTTP API helper.

Read the persisted detection results from the `<project-context>` path (the
per-session keyed file, listed as `project context:` in this session's
context). It is written by session-init.sh at session start and is the single
source of truth for compiler, architecture, and build system.
**Do NOT re-run detection scripts.**

```json
{
  "compiler": "...",
  "build_system": "...",
  "architecture": "...",
  "loci_target": "...",
  ...
}
```

If the file does not exist, stop and tell the user:

> LOCI session context not found. Please restart Claude Code so the plugin
> setup runs and detects the project environment.

Also check the `system-reminder` block emitted at session start for:

```
Target: <target>, Compiler: <compiler>, Build: <build>
LOCI target: <loci_target>
```

Map the LOCI target to LOCI HTTP API supported architectures and binary targets:

| LOCI target | CPU |
|---|---|
| aarch64 | A53 |
| armv7e-m | CortexM4 |
| armv6-m | CortexM0P |
| tc399 | TC399 |

The CPU column identifies which real silicon hardware the LOCI timing and
energy predictions are traced from.

If the architecture is **not** in this table, emit and stop:

```
## Loci Plan: STOPPED
Architecture not supported.
Supported: aarch64, armv7e-m, armv6-m, tc399
```

If no compiler was detected, emit and stop:

```
## Loci Plan: STOPPED
No compiler detected in session context.
Action: resolve the build environment, then re-run the plan analysis.
```

## Step 1: Compile the affected source(s) via build-metadata

Always compile the source file(s) whose callees the new code will invoke
through `<build-metadata-cmd>`. Do **not** reuse an existing `.o` or `.elf`
from the project's own build — LOCI needs the compiler, flags, and version it
controls so that the post-edit rebuild can diff apples-to-apples.

Read `build-metadata command:`, `asm-analyze command:`, `venv python:`, and
`plugin dir:` from the SessionStart context. For each source:

```
<build-metadata-cmd> compile \
    --source <path/to/src.cpp> \
    --loci-target <loci_target> \
    --context "<project-context>" \
    --phase preflight
```

`build-metadata` resolves flags through a typed cascade — each step is
recorded in the `.meta.json` sidecar under `flag_source_v2.attempts`:

1. User override (`.loci-build/flags.json`, `LOCI_EXTRA_CFLAGS`)
2. `compile_commands.json` (exact)
3. `make --dry-run` against the project's own makefile (exact)
4. Sibling `.obj`/`.o` DWARF in the build directory (high)
5. Same-stem `.obj`/`.o` DWARF near the source (high)
6. Linked ELF DWARF (medium; prefers CU whose `DW_AT_name` matches source)
7. TI `.projectspec` XML — `-I`/`-D` only, CPU stripped (medium, partial)
8. Makefile regex scan — augmenter only (low, partial)
9. Hardcoded defaults — last resort with a warning

It guarantees `-g` and `-c`, and writes `.loci-build/<loci_target>/<basename>.o`
plus `.loci-build/<loci_target>/<basename>.o.meta.json`. The compiler /
flags / version / discovery tier are recorded in the sidecar; post-edit
calls `build-metadata diff` to verify parity. **Do not print the
build-metadata block to the user** — the sidecar is the source of truth,
and the block is intentionally suppressed to keep the skill output focused
on the analysis.

**Validate the .o** — a standalone `-c` compile can exit 0 yet produce an
empty object file when the source is wrapped in `#if` / `#ifdef` guards whose
defines (`-D`) were not on the command line. After `build-metadata compile`
succeeds, run:

```
<asm-analyze-cmd> extract-symbols --elf-path .loci-build/<loci_target>/<basename>.o --arch <loci_target>
```

If the result shows 0 symbols or returns an error mentioning "no code" or
"preprocessor", the target function was compiled out. In that case ask the
user for the `-D` flags the project build system uses, re-run
`<build-metadata-cmd> compile`, and re-validate.

**Secondary path: existing binary**

Use a full binary (.elf, .out) for *analysis* only if the callees span multiple
compilation units and linking is needed. You MUST still run
`<build-metadata-cmd> compile` for the relevant source file — the `.o` +
`.meta.json` pair is what the pre-edit hook snapshots, and what post-edit
compares against. Skipping it breaks the entire pre/post chain.

**Hard stop: build-metadata compile fails**

If `<build-metadata-cmd> compile` exits non-zero, emit stderr verbatim and
stop. Do NOT paraphrase, do NOT proceed to analysis. The stderr already
carries the source, flag-source trace, and remediation options.

```
## Loci Plan: STOPPED
build-metadata compile failed for <source>.
<stderr from the command, verbatim>
```

## Step 2: Call graph and timing/energy analysis

Read `asm-analyze command:`, `venv python:`, and `plugin dir:` from the LOCI session context (system-reminder at session start). Use these as `<asm-analyze-cmd>`, `<venv-python>`, and `<plugin-dir>` in the commands below.

The goal is to analyze the functions the edit will affect — for new code, the
callees it will invoke; for a modification, the function itself (plus any new
callees) — before writing anything.

### Extract assembly

Extract CFGs for the callees the new function will invoke:

```
<asm-analyze-cmd> extract-assembly --elf-path <.o or binary> --functions <callee_1,callee_2...> --arch <loci_target>
```

The JSON contains the `control_flow_graph` field with annotated CFGs in
text-format optimized for LLM analysis.

The JSON output contains `timing_csv_chunks`, `timing_csv`, and `timing_architecture` fields needed
for the HTTP API call.

**Extract fields with `jq`, not `python -c`.** Save the extract JSON inside
the project (e.g. `.loci-build/extract.json`) — NEVER `/tmp/`, `/var/tmp/`,
or any path outside the working directory: Claude Code prompts for permission
on every out-of-project access and halts automation. Then:

```
<asm-analyze-cmd> extract-assembly --elf-path <…> --functions <…> --arch <loci_target> > .loci-build/extract.json
jq -r '.control_flow_graph'    .loci-build/extract.json   # annotated CFG text
jq -r '.timing_architecture'   .loci-build/extract.json   # api_arch string (A53/CortexM4/...)
jq -c '.timing_csv_chunks[]'   .loci-build/extract.json   # one chunk per line, pass to the API
```


### Timing and energy via the LOCI HTTP API

Immediately after extraction, get hardware-accurate timing and energy for the
callees by sending each chunk to `api_client.py exec-behavior`:

```
echo "<chunk>" | <venv-python> <plugin-dir>/lib/api_client.py exec-behavior \
    --architecture <timing_architecture>
```

Issue one call per chunk. Run the chunk calls concurrently (background Bash
calls in a single response, then wait) rather than strictly sequentially —
the API handles them independently. Concatenate the result CSVs (skip
duplicate headers) before computing per-callee metrics.

Compute per-callee:
- **Worst path** = `execution_time_ns` + `std_dev_ns`
- **Energy** = `energy_ws` (report in uWs; convert from Ws by multiplying by 1e6)

The API response CSV columns are exactly: `function_name`, `std_dev_ns`,
`execution_time_ns`, `energy_ws`. Reference those column names literally
when reading rows — there is no bare `std_dev` column.

Sum worst-case timings and energy across the hot-path call chain — but
**not** by adding the bare CSV `execution_time_ns` of every hot-path
block. Hot-path blocks that end in `bl` / `blx` are *call sites*: the
API-returned cost for that single block reflects only the branch-only /
single-instruction call-site cost, NOT the cost of the callee's body.
You MUST expand every such block first (see next sub-step) before summing.

If the cumulative expanded chain exceeds a known deadline or energy
budget, flag it now — before any code is written.

### Expand `bl` / `blx` call-site rows

For every block on the hot path whose disassembly ends in `bl` / `blx`
(or whose CFG terminator is annotated `(external-call ...)`,
`→ <callee_symbol>`, or `(unresolved reloc)`):

1. **Identify the callee.** Read the symbol from the CFG annotation
   and/or the `bl` instruction's target. Strip any `_0x<hex>` block
   suffix — you want the function name (e.g. `ClockP_start`,
   `xTimerCreateStatic`).

2. **In-binary callee** — rows whose `function_name` starts with
   `<callee>_` are present in the same API response. Walk the
   callee's hot path through its CFG, then compute:

   ```
   callee_worst_ns  = Σ over callee hot-path blocks of (execution_time_ns + std_dev_ns)
   callee_energy_ws = Σ over callee hot-path blocks of  energy_ws
   ```

   Replace the call-site cost with `bl_cost + callee_worst_ns` (and
   energy with `bl_energy + callee_energy_ws`). If the callee itself
   contains a `bl` to another in-binary symbol, recurse one more
   level. Stop at recursion depth 2 to bound work; if a deeper chain
   is on the hot path, surface it as a CFG note rather than recursing
   indefinitely.

3. **External callee** — `function_name` prefix `<callee>_` is NOT in
   the response (the callee's `.o` was not in `--functions` /
   `--elf-path`, e.g. FreeRTOS / vendor library symbols). Keep
   `bl_cost` as a **lower bound** for this site. Do NOT silently
   accept it as the call-site cost. You MUST:

   - Add a CFG-Analysis line: `⚠️ external callee body unmeasured —
     <callee> figure is a lower bound`.
   - Append `(≥ <total> ns — external callees unmeasured)` to the
     Latency row's Note in the conclusion table.
   - Where reasonable, suggest re-extracting with the callee's
     `.o` added so the next pass measures the body.

The hot-path total is the sum over all hot-path blocks where every
`bl`-terminated block's cost has been replaced by its expanded form
per the rules above. Treating a bare `bl` row as the full call-site
cost (instead of expanding an in-binary callee's hot-path cost, or
marking an external callee as an explicit lower bound when its body
is unavailable) silently understates timing for any function whose
hot path traverses an in-binary callee, and silently understates
external-callee cost without flagging it as a lower bound.

If modifying an existing function and a `.o.prev` exists, also extract timing
and energy for the baseline (pre-edit) function. Compute delta:
```
diff_pct = ((post_value - pre_value) / pre_value) * 100
```

Branch on `api_client.py`'s exit code (the table in the LOCI HTTP API
section). The helper surfaces the underlying stderr verbatim:

- **Missing token (exit 3)** — stop the skill entirely; emit the
  no-`LOCI_API_KEY` message from Step 0.
- **Network error (exit 6)** — skip timing/energy, note "(timing/energy
  unavailable — LOCI HTTP API unreachable)", surface the stderr message
  (DNS/TLS/timeout) so the user can see the cause, and continue with
  CFG-only analysis. Do not retry in a sleep loop.
- **HTTP error (exit 4)** — surface the stderr body (begins with
  `api_client: HTTP <code>`). On 401/403 tell the user the
  `LOCI_API_KEY` was rejected and to verify it is current. On 5xx, note
  "(timing/energy unavailable — LOCI API server error)" and continue with
  CFG-only analysis.
- **Quota / rate-limit (exit 5)** — **stop the skill entirely**. Do not
  continue with CFG analysis or escalation triggers. Output the quota
  message verbatim:
  ```
  LOCI usage quota reached — plan analysis skipped.

  <stderr body from api_client.py verbatim — includes usage/limit, reset countdown, and upgrade link>
  ```
  The server message already contains reset time and upgrade CTA, e.g.:
  "Daily token limit reached (31,000 / 30,000 tokens). Resets in 4h 23m.
  Upgrade to Premium at auroralabs.com for 300,000 tokens/day."
  Show it verbatim. Then end the skill.

If a single chunk call fails with a network/HTTP error (exit 4 non-auth or
6) while others succeed, treat it as unavailable for that chunk's callees
only: skip their timing, flag each affected callee with `⚠️ RISK: timing
data unavailable for <callee>` in CFG Analysis, and continue with CFG-only
analysis for those callees.

### Analyze the CFG output

Check the CFG text from the extract-assembly output for structural hazards:
- **Missing declarations**: are callees present in the binary with the expected
  signatures? If a callee is absent, flag a missing forward declaration or
  linkage issue.
- **Indirect calls**: any `bl` to a register in a callee's CFG — flag as a
  potential CFI hazard.
- **Recursion/cycles**: back edges in the CFG with no visible exit condition —
  flag unbounded recursion.
- **Latency**: use the API timing results above; flag any callee whose worst
  path violates a timing budget, or where the cumulative hot-path chain
  exceeds a known deadline.
- **Energy**: use the API energy results above; flag any callee or hot-path
  chain whose energy cost is notably high relative to the use case (e.g.,
  battery-powered device, ISR context, tight power budget).

### Reason over results

After analyzing the CFG and receiving LOCI results, reason through the
following before proceeding to output. This is a mandatory thinking step —
do not skip it when results look clean. Increment **R** (reasoning cycle
counter) by 1 now.

**Interpretation questions:**
- What is this function's role in the system — is it on a hot path, ISR,
  periodic task, or called once? This determines whether any timing delta
  is critical, advisory, or irrelevant.
- If `.o.prev` exists: is `|delta| < std_dev_ns`? If yes — change is within measurement
  noise, treat as stable. If `|delta| > std_dev_ns` — change is real; flag it.
  If no `.o.prev`: this is the first measurement — record these numbers as the
  baseline and note no prior exists for comparison.
- Does `std_dev_ns` indicate a stable path or high hardware variance — and why
  (cache sensitivity, branch misprediction, pipeline stalls visible in CFG)?
- Is a timing budget known from the session context? If yes, compare hot-path
  worst against it and flag if exceeded. If no budget is known, report the
  number and skip the fit assessment.
- What does the CFG structure explain about the timing — which blocks
  dominate, are there expensive paths the new code will always hit?
- Has every hot-path `bl` / `blx` site been expanded per the
  "Expand `bl` / `blx` call-site rows" step? If a callee's body rows
  are present in the API response but its bare `bl` cost is still
  what's flowing into the Latency total, the number is the entry-block
  understatement — re-aggregate before continuing. If a callee is
  external (no `<callee>_*` rows), is the lower-bound annotation in
  the Latency Note?
- Is the hot-path energy distribution balanced across callees, or does one
  callee dominate? If dominated, that callee is the leverage point — plan
  to cache its result, call it less frequently, or substitute a lighter alternative.
- Do any CFG findings (indirect calls, recursion, missing declarations) change
  the design — does the plan need a guard, a different callee, or a linkage fix?
- **Synthesize per-row Status**: when multiple sub-findings roll up to the
  same Gate (e.g. several CFG hazards under Safety, both worst-case latency
  and dominance under Performance), the row's Status is the worst of the
  contributors and the Note lists them comma-separated, worst-first.
- **Verdict cause comes from sub-findings, not Gate names**: the
  ADJUST PLAN / STOP one-sentence cause lifts the lead item from the
  driving row's Note (e.g. "STOP — unbounded recursion blocks plan", not
  "STOP — Safety row is ❌"). Gate names are for the table; the verdict
  speaks in concrete findings.


**Escalation triggers (run skill inline, then reason over its results):**

*Escalate to `stack-depth`* when — increment R by 1 at trigger:
- Execution context is ISR, HWI, or interrupt callback, AND call chain
  depth > 3 levels visible in CFG, OR
- Recursion already flagged in CFG analysis above, OR
- Plan adds a new RTOS task (xTaskCreate, Task_construct, osThreadNew) that
  needs stack sizing, OR
- Plan introduces large local variables on stack (buffers, arrays, C++ objects
  with non-trivial constructors), OR
- Plan adds a known-deep callee (printf, snprintf, crypto, TLS functions).

After stack-depth returns, reason over its results — increment R by 1:
- Does worst-case stack depth fit the task's or ISR's configured stack budget?
- Are there large frames that could move to static or heap allocation?
- Does any frame in the chain add cost the plan can eliminate?
- Could the call chain be flattened to reduce depth?
→ adjust plan based on conclusion before proceeding.

*Escalate to `memory-report`* when — increment R by 1 at trigger:
- The plan introduces significant new static allocations (large buffers,
  global arrays, static structs) visible from reading the source, OR
- `.o.prev` exists and the plan grows or restructures existing data sections.

After memory-report returns, reason over its results — increment R by 1:
- Does the new allocation fit within available ROM/RAM headroom?
  (answerable only if map file was provided — memory_regions shows usage %;
  without map file, report section size delta only)
- Which region is under most pressure after the change?
- Does the plan need to reduce static footprint before proceeding?
→ adjust plan based on conclusion before proceeding.

### Re-query loop

After reasoning, check whether a better candidate exists before committing to
the plan. If any of the following is true, go back to **Extract assembly** with
the alternative callees and repeat through **Reason over results**:

- Reasoning identified a lighter or safer alternative callee worth evaluating
- A flagged callee (timing violation, CFI hazard, recursion) has a named alternative
  visible in the source files already read
- Hot-path energy is dominated by one callee that may have a lighter variant
- The plan for the new function changed (different call sequence, new callees
  introduced) and those callees have not yet been measured by LOCI — re-query
  with the new callee set before finalizing the plan

Increment **R** by 1 and **M** by the number of new API calls for each re-query cycle.

**Cycle limit: 3 re-query iterations maximum.** If the limit is reached without
a stable plan, emit the best candidate found and note the cycle limit was hit.

**Convergence condition — exit the loop when:**
- The plan is stable (no new callees to evaluate and no unresolved flags), OR
- All remaining flags are ✗ BLOCK (require user decision, not further querying), OR
- The cycle limit is reached.

## Output format

Emit the plan report in the **response text**, before describing what
you will write. In `/plan` mode, the report goes in the response — NOT
inside the plan body.

The output has three blocks in order: (1) conclusion table, (2) voice
remark, (3) LOCI footer. No free-form prose sections, no multi-paragraph
reasoning write-ups, no per-callee enumerations. The reasoning happens
in Step "Reason over results" above — it's mandatory and increments `R`
— but the OUTPUT of the reasoning lands as Status + Note in table rows.

The build-metadata block from `build-metadata compile` is intentionally
NOT shown to the user. Compiler/flag provenance lives in the `.meta.json`
sidecar; `build-metadata diff` surfaces its own `LOCI · build mismatch`
block on its own when parity actually breaks, and that is the only case
the user needs to see it.

### Conclusion table — structure

Header:

```
## Loci Plan: <FunctionName>
```

Followed by the conclusion table. Icon vocabulary: ✅ PASS · ⚠️ WARNING ·
❌ FAIL.

**Row-inclusion rules:**
- Include a row only if the gate actually executed this run.
- Include a row only if there is something to report (skip "Recursion ✅
  none" noise rows).
- Every ⚠️ / ❌ row MUST cite a reason in the Note column — no icon
  without a cause. The Note is the one-line synthesis of the "Reason
  over results" pass for that gate.
- Skipped gates are omitted (no fourth "N/A" icon).

**Row catalogue** (order when present):

1. **Safety** — fires when CFG analysis surfaces a structural hazard
   (missing declaration, indirect call, recursion / cycle). Status:
   ❌ for unbounded recursion or a BLOCK-level missing declaration;
   ⚠️ for benign-but-noteworthy hazards (function-pointer dispatch,
   bounded recursion, weak-symbol miss); otherwise the row is omitted.
   Note names the specific hazard(s).
2. **Performance** — fires when API timing returned. Captures hot-path
   worst-case latency, hot-path dominance (one callee >60% of budget),
   and noise margin (only when `.o.prev` exists: did the delta exceed
   `std_dev_ns`?). Status: ✅ within budget and within noise; ⚠️ near
   budget OR delta exceeds std-dev; ❌ over budget. Note format:
   `worst <X> µs (vs. <budget> when known); dominant: <callee> (<pct>%)`.
3. **Energy** — fires when the API returned energy. Threshold follows the
   target context: ISR / battery-powered tighter than once-per-boot.
   Note format: `<X> µWs`.
4. **Stack** — only when stack-depth was invoked this run. Note:
   `stack: <N> B (<usage>%) — <verdict>` (verbatim from stack-depth).
5. **Memory** — only when memory-report was invoked this run. Note:
   `memory: ROM <X>% / RAM <Y>% — <verdict>`.

Build success and symbol-resolution are NOT table rows. The
`LOCI · build` block at the top already reports compiler/flags/target.
If compile or symbol-extract fails, the skill STOPs before reaching
the conclusion table — no state in which a "Build ✅" row carries new
information.

### Conditional per-callee breakdown (between table and verdict)

Per-callee timing is usually hidden to keep clean runs compact, but it
appears automatically when the engineer needs it. Render a "Hot-path
breakdown" block between the table and the verdict line WHEN any of
these triggers match:

- The **Performance** row's status is ⚠️ or ❌, OR
- The **Performance** Note names a dominant callee (>60% of hot-path worst)

Show top-5 callees along the hot path, sorted by
`worst_ns_summed_across_callee_hot_path` desc. The per-callee
`worst_ns` here is the **summed** body cost, NOT the entry-block
worst — same expansion as the Step 2 sub-step. External callees
appear with `≥ <bl_cost>` and a `(body unmeasured)` tag:

```
Hot-path breakdown (top-5 by worst):
  <in_binary_callee_1>   <summed_worst_ns> (<pct>%)   <summed_energy_uWs>
  <in_binary_callee_2>   ...
  <external_callee>      ≥ <bl_cost_ns> (<pct>%)      ≥ <bl_energy_uWs>   (body unmeasured)
  ...
```

Omit this block when neither trigger matches (clean runs stay short).
When fewer than 5 callees contributed to the hot path, show what's
there — don't pad.

**Table footer** (always): bolded single-line verdict.
`Execution fit: **GOOD** — proceed with plan` ·
`**ADJUST PLAN** — <one-sentence change>` ·
`**STOP** — <one-sentence reason>`

### Template

```
## Loci Plan: <FunctionName>

| Gate                     | Status | Note                              |
|--------------------------|:------:|-----------------------------------|
| <row 1 when applicable>  |   ?   | <cited reason>                     |
| ...                      |   ?   | ...                                |

<Hot-path breakdown block — only if Performance ⚠️/❌ or its Note names a dominant callee>

Execution fit: **<GOOD|ADJUST PLAN|STOP>** — <one sentence>
```

### Example (typical clean run, ~10 lines)

```
## Loci Plan: process_message

| Gate         | Status | Note                              |
|--------------|:------:|-----------------------------------|
| Safety       |   ⚠️   | dispatch via function pointer — benign |
| Performance  |   ✅   | hot-path worst 1.8 µs              |
| Energy       |   ✅   | 0.05 µWs                           |

Execution fit: **GOOD** — proceed with plan
```

For modifying an existing function with `.o.prev` available, the
**Performance** row's Note carries the noise-margin sub-finding
(`|delta| vs std_dev_ns`). The Before/After comparison lives inside
that Note, not as a separate Delta block.

## Re-reasoning triggers (table-driven)

Before emitting the final conclusion table, inspect what the first-pass
analysis produced. If any of the row patterns below matches, loop back
— re-query the API, escalate, or re-read source — BEFORE emitting. Each
looped-back pass increments `R` (co-reasoning); each extra API call
increments `M`. The table the user sees is the post-loop version, not
the first-pass draft.

| Row pattern | Trigger |
|---|---|
| **Performance** Note shows dominance > 80% | Re-query the API on the dominant callee's per-block timings (not just the entry block). One extra API call. Often reveals a specific block as the leverage point, which the hot-path-summary hid. |
| **Safety** ❌ with missing-decl sub-finding | Before STOP: re-read the source to check for alternate callees that share the name (macro redefinition, weak symbol, LTO-inlined). Don't STOP on the first miss; verify. |
| **Safety** with indirect-call sub-finding AND function is on an ISR path | Escalate to stack-depth even if usual triggers don't match — indirect dispatch can hide call-graph depth from static analysis. |
| **Safety** with recursion sub-finding | Escalate to stack-depth (already the existing rule, restated here for table-completeness). |
| **Performance** Note shows `|delta|` within `std_dev` | Downgrade any ⚠️ on Performance/Energy to ✅ automatically — the measured regression is within measurement noise. Verdict stays GOOD even if raw numbers suggested ADJUST. |

Per-callee timing detail appears in the conditional "Hot-path breakdown"
block above, but only when the Performance row is ⚠️/❌ or its Note names
a dominant callee — clean runs skip it to stay short. If the engineer
needs per-block breakdown beyond top-5 callees, re-extract via
`asm-analyze extract-assembly` directly.

## Adjusting the plan based on findings

The value of running the plan analysis during thinking is that findings change the
plan, not just add comments:

- A missing forward declaration → add it as a step before the function edit
- An unbounded loop in a callee → plan to add a termination guard or budget
- A callee timing violation → plan to cache the result, call asynchronously,
  or choose a lighter alternative before committing to the design
- An energy concern → plan to batch calls, use a lighter alternative, or move
  work off the hot path

Write the adjusted plan, then write the code. Do not write the code and then
note risks afterward — that defeats the purpose.

## LOCI voice remark

Before the footer, add one short LOCI voice remark (max 15 words) that
acknowledges the user's work grounded in a specific number from the
analysis. Attribute improvements to the user ("clean work", "smart move",
"tight code"). For concerns, be honest and constructive with specifics.
Skip if the analysis produced no results or the user needs raw data only.

## LOCI footer

After emitting the plan report (or all-clear shorthand), append the
footer as the last thing printed — **only if N > 0** (at least one
function was sent to LOCI). If no functions were processed (LOCI API
unavailable or no functions to measure), do NOT emit the footer.

**Record cumulative stats** (run via Bash before rendering the footer).
Pass `--verdict "<verbatim-verdict-line>"` so the verdict ride-along
ships alongside the per-function trends payload — the line is the same
string already rendered to chat (`Execution fit: GOOD — proceed with plan`,
`Execution fit: ADJUST PLAN — <reason>`, or `Execution fit: STOP — <reason>`),
unbolded, no surrounding asterisks.

Also pass `--gates '<gates-json>'` — a compact JSON object capturing
the per-row Status from the conclusion table just rendered. Map the
icons: `✅→pass · ⚠️→warn · ❌→fail`. Only include gates that fired
this run (omitted gates were not part of the table). Allowed gate
names: `Safety` · `Performance` · `Energy` · `Stack` · `Memory`.
Example for the clean-run plan example:
`{"Safety":"warn","Performance":"pass","Energy":"pass"}`.
```
<venv-python> <plugin-dir>/lib/loci_stats.py record --context-file "<project-context>" --skill preflight --functions <N> --api-calls <M> --co-reasoning <R> --verdict "<verbatim-verdict-line>" --gates '<gates-json>'
```

**Record per-function measurements** (single Bash call for all functions).
Pipe all measurements as JSONL via stdin. Skip functions where API timing
was unavailable.
```
echo '<jsonl_records>' | <venv-python> <plugin-dir>/lib/loci_stats.py record-measurement --context-file "<project-context>" --stdin --skill preflight
```
Where each line is one function:
```
{"fn":"<func1>","worst_ns":<execution_time_ns>,"energy_uws":<E>}
{"fn":"<func2>","worst_ns":<execution_time_ns>,"energy_uws":<E>}
```

The `worst_ns` field name is the storage-schema key consumed by
`loci_stats.py` (preserved for compat with prior on-disk measurements);
pass `execution_time_ns` into it.

### Render the footer — compact by default

One line. Icon-led, no surrounding bars, middle-dot separators, spaces
around any `→` arrow:

```
<icon> LOCI plan · <N> functions · fit <GOOD|ADJUST|STOP>
```

- `<icon>` — mirrors the body's Execution-fit verdict: `✅` for GOOD,
  `⚠️` for ADJUST, `❌` for STOP.

Worked example (clean run):
```
✅ LOCI plan · 2 functions · fit GOOD
```

### Clean-escalation suffix

When the plan analysis escalated into `stack-depth` or `memory-report` AND the
escalated skill returned clean, append a space-separated `+<skill>`
marker to the primary scalar so the compact line still surfaces that
the deeper check ran:

```
✅ LOCI plan · 2 functions · fit GOOD  +stack-depth
✅ LOCI plan · 5 functions · fit GOOD  +stack-depth +memory-report
```

A non-clean escalated result already flips a Stack/Memory row in the
plan conclusion table to ⚠️/❌ and the verdict to ADJUST/STOP, so
`+<skill>` only ever appears next to a green icon. The conclusion
table itself carries the bad news — the footer stays compact regardless
of verdict, and the cumulative branch-stats line is not included.

Counter definitions (used by `loci_stats.py record` above):

- **N** = unique functions whose assembly was sent to LOCI (callees of
  new code, or modified functions themselves)
- **M** = HTTP API calls to
  `POST /mcp/v1/get_assembly_block_exec_behavior` (one per timing CSV chunk)
- **R** = co-reasoning: 1 for the initial LOCI result pass, +1 for each
  re-query loop iteration, +2 for each escalated skill (1 at trigger,
  1 when reasoning over results)
