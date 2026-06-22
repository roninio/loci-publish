---
name: loci-post-edit
description: >
  Compare pre-edit and post-edit compiled artifacts to report execution
  timing % diff, energy consumption, and control-flow analysis.
when_to_use: >
  MANDATORY after any Edit/Write/MultiEdit to C/C++/Rust source
  (.c,.cc,.cpp,.cxx,.h,.hpp,.hxx,.rs). Invoke IMMEDIATELY — do not skip,
  batch, or wait. Also: "analyze the change", "measure the edit", "timing diff".
---

# loci-post-edit

This skill merges execution-trace (timing/energy) and control-flow (CFG)
analysis into a single post-edit report. It compares pre-edit and post-edit
compiled artifacts to show exactly how the change affects hardware execution.

## Tool boundary: asm-analyze only — never objdump

All assembly, CFG, symbol, and ELF inspection in this skill goes through
`<asm-analyze-cmd>`. Do **not** use `objdump`, `readelf`, `addr2line`, or
`nm` as substitutes — asm-analyze produces the per-block CSV, timing CSV,
and annotated CFG the LOCI API expects; binutils output is not equivalent.
If asm-analyze returns an error, surface it and stop; do not fall back to
objdump.

Always pass `--arch <loci_target>` on every asm-analyze call, reading the
value verbatim from the SessionStart `LOCI target:` line.


## LOCI HTTP API — replaces the legacy MCP tool

All exec-behavior calls go through the HTTP API, not the MCP tool. Use the
helper at `<plugin-dir>/lib/api_client.py` (stdlib `urllib`, no extra deps).
Do NOT call `mcp__plugin_loci_loci__get_assembly_block_exec_behavior` — that
path is retired.

**Endpoint** — `POST https://mcp.auroralabs.com/mcp/v1/get_assembly_block_exec_behavior`
**Auth** — `Authorization: Bearer <LOCI_API_KEY>`. The helper resolves the
token in this order (first hit wins):
  1. `$LOCI_API_KEY` environment variable.
  2. `.loci/config.json` in the **current working directory**, key
     `"LOCI_API_KEY"`. Example:
     ```json
     { "LOCI_API_KEY": "sk-loci-..." }
     ```
Either source is fine — pick whichever fits the user's workflow. Do not
embed the token in any command line or commit it to the repo; the helper
keeps it out of process argv.
**Request body** — JSON `{"csv_text": "<one chunk>", "architecture": "<A53|CortexM4|CortexM0P|TC399>"}`.
**Response body** — text/csv with columns
`function_name,std_dev_ns,execution_time_ns,energy_ws`.

**Helper invocation** — one chunk per call, CSV chunk on stdin:

```
echo "<chunk>" | <venv-python> <plugin-dir>/lib/api_client.py exec-behavior \
    --architecture <loci_arch>
```

Or, when the chunk is large, write it to a project-local file first (NEVER
`/tmp/`) and pass `--csv-file`:

```
<venv-python> <plugin-dir>/lib/api_client.py exec-behavior \
    --architecture <loci_arch> --csv-file .loci-build/chunk_0.csv
```

**Exit codes** (the skill must branch on these — see Step 4 degradation):

| Exit | Meaning |
|---|---|
| 0 | Success — CSV on stdout |
| 3 | No `LOCI_API_KEY` found (neither in env nor `.loci/config.json`) |
| 4 | HTTP non-2xx (auth, server error); body on stderr |
| 5 | Quota / rate limit (HTTP 429); body on stderr verbatim |
| 6 | Network/transport error (DNS, TLS, timeout) |

**Pre-flight check** — before Step 1, verify a token is reachable from at
least one of the two sources:

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

## Step 0: Check session context

Read the persisted detection results from the `<project-context>` path (the
per-session keyed file, listed as `project context:` in this session's
context). It is written by session-init.sh at session start and is the single
source of truth for compiler, architecture, and build system.
**Do NOT re-run detection or fall back to ELF/build-system sniffing.**

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

Map the LOCI target to LOCI API supported timing architectures and binary targets:

| LOCI target |   Time from CPU  |
|---|---|
| aarch64 | A53 |
| armv7e-m | CortexM4|
| armv6-m | CortexM0P |
| tc399 | TC399 |

If the architecture is **not** in this table, emit and stop:

```
Supported: aarch64 , armv7e-m , armv6-m , tc399
```

## Step 1: Compile post-edit using loci-plan's flags

Compile the edited source with the exact compiler + flags loci-plan used. The
pre-edit hook captured loci-plan's metadata at
`.loci-build/<loci_target>/<basename>.o.meta.json.prev`. Pass it via
`--meta-prev` so the post-edit build inherits those flags rather than
re-detecting them:

```
<build-metadata-cmd> compile \
    --source <path/to/src.cpp> \
    --loci-target <loci_target> \
    --context "<project-context>" \
    --meta-prev .loci-build/<loci_target>/<basename>.o.meta.json.prev \
    --phase post-edit
```

The command writes only the `.meta.json` sidecar to disk and exits
silently. **Do not print the build-metadata block to the user** — the
sidecar is the source of truth, and Step 1b's `build-metadata diff`
already surfaces a `LOCI · build mismatch` block on its own when parity
fails, which is the only case the user needs to see.

If `.o.meta.json.prev` does not exist, loci-plan did not run before this
edit. Omit `--meta-prev`; `build-metadata` will re-detect flags and record
them. Report absolute timing only in Step 5 — no % diff is available without
a loci-plan baseline.

**Validate the .o** — a standalone `-c` compile can exit 0 yet produce an
empty object file when the source is wrapped in `#if` / `#ifdef` guards
whose defines (`-D`) were not on the command line. After compiling, run:
```
<asm-analyze-cmd> extract-symbols --elf-path .loci-build/<loci_target>/<basename>.o --arch <loci_target>
```
If the result shows 0 symbols or an error mentions "no code" / "preprocessor",
the target function was compiled out. Ask the user for the `-D` flags the
project build system uses, re-run `<build-metadata-cmd> compile`, and
re-validate. Do not fall back to a project-built `.elf` with unknown flags.

## Step 1b: Verify build parity between loci-plan and post-edit

```
<build-metadata-cmd> diff \
    --prev .loci-build/<loci_target>/<basename>.o.meta.json.prev \
    --curr .loci-build/<loci_target>/<basename>.o.meta.json
```

`build-metadata diff` is **informational only — never a stop condition**.
A non-zero exit means there is a delta to surface to the user; it does
NOT mean skip analysis or skip the report. Always proceed to Step 2 and
run the full timing/CFG analysis regardless of this exit code.

Exit code:
- **0** — compiler, version, flags, and target match → the timing diff is
  apples-to-apples; proceed normally.
- **non-zero** — the command prints a `LOCI · build mismatch` block.
  **Emit it verbatim** in the post-edit report, tag the final verdict as
  `LOW CONFIDENCE — build environment changed between loci-plan and
  post-edit`, and **continue with full timing analysis**. The % diffs may
  reflect the toolchain delta rather than the code change — note that,
  but still report the numbers. Do not stop, skip steps, or omit the
  per-function table on a build mismatch.

  A `flag_source` kind regression (e.g. loci-plan used `gmake-dry-run`
  but post-edit fell through to `defaults`) shows up as a dedicated line
  in the mismatch block: `flag_source   kind 'X' → 'Y' — discovery
  regressed between loci-plan and post-edit; baseline unreliable`.
  Treat that as a stronger signal than a flag-list-only delta, but the
  same rule applies: surface it, do not stop.

Skip this step entirely only if loci-plan did not run (no
`.o.meta.json.prev`).

## Step 2: diff-elfs — find modified/added functions

### Case A: `.o.prev` exists (loci-plan ran before the edit)

The pair of artifacts to compare lives in `.loci-build/<loci_target>/`:
- pre-edit:  `<basename>.o.prev`   (captured by the pre-edit hook)
- post-edit: `<basename>.o`        (just compiled in Step 1)

```
<asm-analyze-cmd> diff-elfs \
    --elf-path .loci-build/<loci_target>/<basename>.o.prev \
    --comparing-elf-path .loci-build/<loci_target>/<basename>.o \
    --arch <loci_target>
```

This returns lists of `modified` and `added` functions. Only these functions
need analysis — skip unchanged code entirely.

### Case B: no `.o.prev` (loci-plan did not run)

Do **NOT** invoke `diff-elfs` — it requires both artifacts and will error
on a missing `--elf-path`. Skip directly to Step 3 and extract assembly from
the post-edit `.o` only; treat every function in the output as "added" for
reporting purposes. Note in the final report:
`(no loci-plan baseline — first-edit measurement; % diff not available)`.

## Step 3 + 4a: extract assembly and fetch timing in one call

Steps 3 (extract-assembly) and 4 (HTTP API timing) are fused into a single
mechanical helper, `<plugin-dir>/lib/extract_and_time.py` (it imports the
`api_client.py` next to it). One invocation extracts assembly from the
pre-edit `.o.prev` and post-edit `.o`, fans out **every** timing CSV chunk —
pre and post — to the LOCI API concurrently, and writes
the concatenated per-side timing CSVs plus the CFG text into `--out-dir`.

```
<venv-python> <plugin-dir>/lib/extract_and_time.py \
    --asm-analyze "<asm-analyze-cmd>" \
    --arch <loci_target> \
    --build-dir .loci-build/<loci_target> \
    --basename <basename> \
    --functions <func1>,<func2> \
    --out-dir .loci-build
```

- **Modified** functions: run as above — the helper extracts and times both
  the `.o.prev` (pre) and `.o` (post) sides.
- **Added** functions (no `.o.prev`): pass `--added`. The helper skips the pre
  side and emits post-only outputs; no % diff baseline exists. If a run mixes
  modified and added functions, invoke the helper once per group.
- The `--functions` list comes from Step 2's `diff-elfs` (modified + added).
- It forwards `timing_architecture` to the API verbatim. If the API needs the
  short name from the Step 0 table (e.g. `A53` for `aarch64`), pass
  `--api-arch A53`.

It prints a manifest JSON naming every output file:

```
{
  "api_arch": "<arch sent to API>",
  "post": {"extract_json":"…","cfg_txt":"…","timing_csv":"…","chunks":N,"block_rows":N},
  "pre":  {"extract_json":"…","cfg_txt":"…","timing_csv":"…","chunks":N,"block_rows":N}
}
```

`pre` is absent on `--added` runs. Read the outputs with `jq`/file reads — do
**NOT** re-run `asm-analyze` or call the API again; the helper already did both:
- `timing_csv` (pre + post) — CSV columns `function_name,std_dev_ns,execution_time_ns,energy_ws`, one row per block. Use `execution_time_ns` and `energy_ws`; `std_dev_ns` is surfaced only for the bl-expansion sum below.
- `cfg_txt` (pre + post) — the annotated CFG text for hot-path / bl-site reasoning.

All paths stay inside the working directory (`.loci-build/`); the helper NEVER
writes to `/tmp/`.

### Helper exit codes (drive the degradation rules below)

`extract_and_time.py` mirrors `api_client.py`'s exit codes:

| Exit | Meaning |
|---|---|
| 0 | Success — manifest JSON on stdout, CSVs + CFG written |
| 2 | Bad invocation / extract-assembly failure / no chunks |
| 3 | No `LOCI_API_KEY` — stop the skill |
| 4 | HTTP non-2xx; body on stderr |
| 5 | Quota / rate-limit (HTTP 429); body on stderr — stop the skill |
| 6 | Network/transport error |

## Step 4b: compute % diff from the fetched CSVs

The timing CSVs are already on disk from Step 3 + 4a — **do not call the API
again**. Load the `pre`/`post` `timing_csv` files named in the manifest and,
using the `cfg_txt` for hot-path structure, compute:
- **Timing** = `execution_time_ns`
- **Energy** = `energy_ws` (report in uWs)

### Expand `bl` / `blx` call-site rows (pre AND post)

Hot-path blocks that end in `bl` / `blx` are *call sites*; the API
returns only the branch-only / single-instruction cost for that block
(e.g. ~32 ns on Cortex-M0+), NOT the callee body. You MUST expand every
such site on both the pre-edit and post-edit hot paths before computing
the Worst path / Happy path / Energy values that go into the table —
otherwise both sides are entry-block-only and a callee-internal
regression (e.g. a 200 → 240 ns body change at a `bl` site) silently
shows up as 0 ns delta because both sides counted only the `bl`
instruction.

For each hot-path block ending in `bl` / `blx`:

1. **In-binary callee** (rows whose `function_name` starts with
   `<callee>_` are present in the same side's `timing_csv`): replace the
   call-site cost with `bl_cost + Σ over the callee's hot-path blocks
   of (execution_time_ns + std_dev_ns)`, and energy similarly.
   Recurse one more level if the callee itself contains an in-binary
   `bl`. Stop at depth 2.
2. **External callee** (no `<callee>_*` rows in that `timing_csv` — e.g.
   FreeRTOS / vendor library): keep `bl_cost` as a **lower bound**.
   Append `(≥ … ns — external callees unmeasured)` to the Note of
   every affected summary row — Worst path, Happy path, and/or Energy
   whenever that row's hot path includes the external callee — and add
   a CFG-Analysis line naming the external callee.

The Worst / Happy / Energy values that go into the Worst path / Happy
path / Energy rows are the expanded sums. If any included hot-path
block is an external callee kept at `bl_cost`, the corresponding row
remains a lower bound and must be annotated accordingly. The Hot
blocks breakdown between table and verdict still uses *per-block* CSV
rows (the user wants to see the heaviest blocks) — but a
`bl`-terminated block in that breakdown should be labelled
`bl <callee>` so the reader knows its measured cost is just the
branch, with the callee's body counted separately in the Worst path
total when available, or left unmeasured for external callees.

For modified functions, compute % diff:
```
diff_pct = ((post_value - pre_value) / pre_value) * 100
```

The diff is meaningful only after expansion — if either side is
entry-block-only, the % diff is between two understated baselines and
the noise-margin downgrade rule will silently mask real regressions.

### Graceful degradation

Branch on `extract_and_time.py`'s exit code (the table in Step 3 + 4a; it
mirrors `api_client.py`). The helper surfaces the underlying `api_client`
stderr verbatim, so the messages below still apply:

- **Missing token (exit 3)** — stop the skill entirely. Tell the user:
  > No `LOCI_API_KEY` found. Either export it
  > (`export LOCI_API_KEY=...`) or put it in `.loci/config.json` in this
  > working directory as `{"LOCI_API_KEY": "..."}`, then re-run. The
  > token is read at call time by the HTTP API helper.
- **Network error (exit 6)** — report CFG analysis only, note
  "(timing unavailable — LOCI HTTP API unreachable)". Surface the stderr
  message from `api_client.py` so the user can see whether it was DNS,
  TLS, or timeout. Do not retry in a sleep loop.
- **HTTP error (exit 4)** — surface the stderr body (which begins with
  `api_client: HTTP <code>`). On 401/403, tell the user:
  > `LOCI_API_KEY` was rejected. Verify the token is current and has
  > access to the exec-behavior endpoint.
  On 5xx, note "(timing unavailable — LOCI API server error)" and report
  CFG analysis only.
- **Quota / rate-limit (exit 5)** — **stop the skill entirely**. Do not
  emit the post-edit report template. Instead, output the quota message
  with reset time and upgrade CTA:
  ```
  LOCI usage quota reached — post-edit analysis skipped.

  <stderr body from api_client.py verbatim — includes usage/limit, reset countdown, and upgrade link>
  ```
  The server message already contains reset time and upgrade CTA, e.g.:
  "Daily token limit reached (31,000 / 30,000 tokens). Resets in 4h 23m.
  Upgrade to Premium at auroralabs.com for 300,000 tokens/day."
  Show it verbatim. Then end the skill.
- **No pre-edit artifact** — report absolute timing only, no % diff

## Step 5: Internal reasoning pass (mandatory)

Before emitting any output, think through each of these questions.
Increment `R` (co-reasoning counter) by 1 for this pass.

1. **Timing impact** — is the diff expected given the code change? Flag
   regressions >10% on `execution_time_ns` as a Performance sub-finding.
   Note when the change is timing-neutral or improves performance.
2. **Hotspot check** — does any new/changed block sit among the top 3
   hottest blocks? If yes, record as a Performance sub-finding
   (`new hot-path block <addr> (top-N)`).
3. **Energy budget** — is the energy delta acceptable for the target
   context? Battery-powered / ISR / hot-path: tighten. Once-per-boot:
   looser.
4. **Synthesize per-row Status** — when multiple sub-findings roll up
   to the same Gate (e.g. timing regression + new hot-path block both
   under Performance), the row's Status is the worst of the
   contributors and the Note lists them comma-separated, worst-first.
5. **Verdict cause comes from sub-findings, not Gate names** — the
   OK / CAUTION / FLAG one-sentence cause lifts the lead item from the
   driving row's Note (e.g. "FLAG — timing +147% past budget", not
   "FLAG — Performance row is ❌"). Gate names are for the table;
   the verdict speaks in concrete findings.
6. **Verdict** — OK / CAUTION / FLAG with one-sentence cause. The
   cause goes in the table footer verdict line.

## Step 6: Emit report

The output has three blocks in order: (1) conclusion table, (2) voice
remark, then the LOCI footer. No free-form prose sections, no
multi-paragraph Reasoning write-ups, no per-callee enumerations.

The build-metadata block from `build-metadata compile` is intentionally
NOT shown to the user. The only build-related thing that ever surfaces in
the report is the `LOCI · build mismatch` block, and only when Step 1b's
`build-metadata diff` actually finds a parity break — that block prints
itself, emit it verbatim when it appears.

Icon vocabulary: ✅ PASS · ⚠️ WARNING · ❌ FAIL.

**Row-inclusion rules:**
- Include a row only if the gate actually produced a value this run.
- Every ⚠️ / ❌ row MUST cite a reason in the Note column — the Note is
  the one-sentence synthesis of the Step 5 reasoning for that gate.
- Skipped gates are omitted (no fourth "N/A" icon).

### Row catalogue — with baseline (`.o.prev` present and non-empty)

Order when present. Before/After columns carry the metric value
(timing or energy); sub-findings ride in the Note.

1. **Safety** — fires only when a CFG-structural hazard is incidentally
   observed in the diff (recursion introduced, indirect call added,
   missing declaration). Status: ❌ for unbounded recursion or BLOCK-
   level missing declaration; ⚠️ for benign-but-noteworthy hazards.
   Rare in post-edit — the row is omitted when nothing was observed.
2. **Performance** — fires when the HTTP API returned timing. Captures
   `execution_time_ns` diff and hot-path position (new block in top-3).
   Status: ✅ if `|diff%| ≤ 10%` or improvement AND no new hot-path
   block; ⚠️ if `|diff%| > 10%` with absolute within budget OR a new
   hot-path block landed in top-3; ❌ if a known budget is exceeded.
   Before/After = `execution_time_ns`. Note format:
   `<pre>→<post> ns (±X%) [, new hot-path block <addr> (top-N)]`.
3. **Energy** — fires when the HTTP API returned energy. Status logic same as
   Performance with target-context thresholds (ISR/battery tighter
   than once-per-boot). Before/After = energy values. Note cites
   `±X%` and absolute when small.
4. **Stack** — only when stack-depth was invoked as an escalation.
   Note is the one-line summary handed back by stack-depth:
   `stack: <N> B (<usage>%) — <verdict>`. No Before/After.
5. **Memory** — only when memory-report was invoked as an escalation.
   Note: `memory: ROM <X>% / RAM <Y>% — <verdict>`. No Before/After.

Build-parity issues are NOT a table row. `build-metadata diff`'s own
`LOCI · build mismatch` block (emitted on non-zero exit) already
handles that case visibly and loudly.

### Row catalogue — no baseline (first-edit measurement or empty `.o.prev`)

Drop the Before column; single-column After for the Performance and
Energy rows (no `±%` in the Note since there is no baseline to diff
against — record the absolute values as the new baseline). Safety,
Stack, and Memory rows fire on the same triggers as the with-baseline
case.

### Template (with baseline)

```
## Post-Edit: <FunctionName>

| Gate               | Before    | After     | Status | Note                        |
|--------------------|-----------|-----------|:------:|-----------------------------|
| <row 1 applicable> | <val>     | <val>     |   ?   | <cited reason>               |
| ...                | ...       | ...       |   ?   | ...                          |

Verdict: **<OK|CAUTION|FLAG>** — <one sentence cause>
```

### Template (no baseline)

```
## Post-Edit: <FunctionName> (NEW)

| Gate               | After     | Status | Note                        |
|--------------------|-----------|:------:|-----------------------------|
| <row 1 applicable> | <val>     |   ?   | <cited reason>               |
| ...                | ...       |   ?   | ...                          |

Verdict: **<OK|CAUTION|FLAG>** — <one sentence cause>
(no pre-edit artifact — first measurement on this branch)
```

### Example (with baseline, typical ~6 lines)

```
## Post-Edit: process_message

| Gate         | Before   | After    | Status | Note                              |
|--------------|----------|----------|:------:|-----------------------------------|
| Performance  | 1404 ns  | 3474 ns  |   ⚠️   | +147%, new hot-path block bb_0x1ea (top-1) |
| Energy       | 0.20 µWs | 0.49 µWs |   ⚠️   | +148%, absolute <1 µWs             |

Verdict: **CAUTION (acceptable)** — explicable, once-per-event handler
```

### Action on CAUTION or FLAG

When the table footer is `CAUTION` or `FLAG`, don't stop at reporting.
The skill must:

1. Propose a concrete fix in one sentence, named by the ⚠️ or ❌ row.
   (Example: "`bb_0x1ea` is a wide-integer arithmetic step — consider
   narrowing the type to a 32-bit integer where the value range allows,
   saves ~500 ns.")
2. Ask the user whether to apply the rewrite. Do not silently proceed.

## Re-reasoning triggers (table-driven)

Before emitting the final conclusion table, inspect what the first-pass
reasoning produced. If any pattern below matches, loop back BEFORE
emitting. Each extra HTTP API call increments `M`; each looped-back synthesis
increments `R`. The table the user sees is the post-loop version.

| Row pattern | Trigger |
|---|---|
| **Performance** ⚠️ with both timing-regression AND new-hot-path-block sub-findings | The new block IS the regression. Don't just report — propose a concrete optimization (cache, lighter callee, inline, different data type) naming the specific block in the Note. Follow the "Action on CAUTION or FLAG" flow. |
| **Performance** AND **Energy** ⚠️ both regress | Real regression in two metrics, not isolated to one. Confidence in ⚠️ is high; proceed to propose root cause. |
| **Stack** Note shows usage > 80% of task budget | Re-run stack-depth with larger `--max-recursion-depth` to confirm; surface the top frame contributor by name in the Note before emitting. |
| **Memory** Note shows region > 90% | Re-run memory-report with `--top-n 20` to identify the specific symbols pushing the region toward its limit before emitting. |

## LOCI voice remark

Before the footer, add one short LOCI voice remark (max 15 words) that
acknowledges the user's work grounded in a specific number from the
analysis. Attribute improvements to the user ("clean work", "smart move",
"tight code"). For concerns, be honest and constructive with specifics.
Skip if the analysis produced no results or the user needs raw data only.

## LOCI footer

After emitting all per-function reports and the voice remark, append the
footer as the last thing printed — **only if N > 0**. If no functions
were processed, do NOT emit the footer.

**Record cumulative stats** (run via Bash before rendering the footer).
Pass `--verdict "<verbatim-verdict-line>"` so the verdict ride-along
ships alongside the per-function trends payload — the line is the same
string already rendered to chat (`Verdict: OK — <cause>`, `Verdict: CAUTION — <cause>`,
or `Verdict: FLAG — <cause>`), unbolded, no surrounding asterisks.

Also pass `--gates '<gates-json>'` — a compact JSON object capturing
the per-row Status from the conclusion table just rendered. Map the
icons: `✅→pass · ⚠️→warn · ❌→fail`. Only include gates that fired
this run (omitted gates were not part of the table). Allowed gate
names: `Safety` · `Performance` · `Energy` · `Stack` · `Memory`.
Example for the worked example above:
`{"Performance":"warn","Energy":"warn"}`.
```
<venv-python> <plugin-dir>/lib/loci_stats.py record --context-file "<project-context>" --skill post-edit --functions <N> --api-calls <M> --co-reasoning <R> --verdict "<verbatim-verdict-line>" --gates '<gates-json>'
```

**Record per-function measurements** (single Bash call for all functions).
Pipe all measurements as JSONL via stdin. Skip functions where the HTTP
API did not return timing.
```
echo '<jsonl_records>' | <venv-python> <plugin-dir>/lib/loci_stats.py record-measurement --context-file "<project-context>" --stdin --skill post-edit
```
Where `<jsonl_records>` is one JSON object per line for each modified/added
function with post-edit timing values:
```
{"fn":"<func1>","worst_ns":<execution_time_ns>,"energy_uws":<E>,"src":"<source_file>"}
{"fn":"<func2>","worst_ns":<execution_time_ns>,"energy_uws":<E>,"src":"<source_file>"}
```

The `worst_ns` field name is the storage-schema key consumed by
`loci_stats.py` (preserved for compat with prior on-disk measurements);
pass `execution_time_ns` into it. The `happy_ns` field is no longer
written.

**Read trend lines** (single Bash call for all functions; capture output):
```
<venv-python> <plugin-dir>/lib/loci_stats.py trend-line --context-file "<project-context>" --function <func1>,<func2>,...
```

### Render the footer — compact by default

One line. Icon-led, no surrounding bars, middle-dot separators, spaces
around the `→` arrow. The `trend-line` output is the primary scalar —
parse it into `<fn> · <pre> → <post> ns (<±pct>, <N> edits)`:

```
<icon> LOCI post-edit · <fn> · <pre> → <post> ns (<±pct>, <N> edits)
```

- `<icon>` — mirrors the body's conclusion-table verdict: `✅` for OK,
  `⚠️` for CAUTION, `❌` for FLAG.
- `<fn>` — when `N = 1`, the single edited function. When `N > 1`, the
  compact form is replaced by the expanded form (see below).

Worked example (clean run, N=1):
```
✅ LOCI post-edit · Connection_ConnEventHandler · 1815 → 1498 ns (-17%, 2 edits)
```

### Clean-escalation suffix

When post-edit escalated into `stack-depth` or `memory-report` AND the
escalated skill returned clean, append a space-separated `+<skill>`
marker to the primary scalar:

```
✅ LOCI post-edit · Connection_ConnEventHandler · 1815 → 1498 ns (-17%, 2 edits)  +stack-depth
```

A non-clean escalated result already flips a Stack/Memory row in the
post-edit conclusion table to ⚠️/❌, which flips the post-edit verdict
to CAUTION/FLAG and triggers expansion via the verdict rule below. So
`+<skill>` only ever appears next to a green icon.

### Expand when...

Replace the compact form with the expanded multi-line form if **any**
of the following is true:
- Verdict is `⚠️ CAUTION` or `❌ FLAG`.
- Build-parity mismatch — a `LOCI · build mismatch` block was emitted
  earlier in the report (toolchain changed between loci-plan and
  post-edit; % diffs are low-confidence).
- `N > 1` functions were modified/added in this run — the compact line
  cannot carry per-function trends honestly; render the expanded form
  with one `↳ trend:` line per function.

Expanded form:
```
─── LOCI · post-edit ───────────────────
  <N> functions · <M> API calls · <R> co-reasoning
  Verdict: <OK | CAUTION | FLAG> — <one-line summary>
    ↳ trend: <trend-line-output>       ← one line per function
────────────────────────────────────────
```

The expanded form does **not** include the cumulative branch-stats line.

- **N** = unique functions (modified + added) whose assembly was sent to LOCI
- **M** = HTTP API calls to
  `POST /mcp/v1/get_assembly_block_exec_behavior`, fanned out by
  `extract_and_time.py` — one per timing CSV chunk. Sum the manifest's
  `pre.chunks + post.chunks` (post-only `chunks` for added functions).
- **R** = co-reasoning (one per function that has a Reasoning section)
