---
name: exec-trace
description: Analyze function execution timing and energy from compiled assembly. Use when the user asks for timing, energy, latency, or execution cost of a specific function from compiled assembly.
when_to_use: When user asks for timing/energy of a specific function from compiled assembly.
---

# LOCI Timing Analysis

Read these values from the LOCI session context (system-reminder block at session start) and substitute them wherever the placeholders appear below:
- `asm-analyze command: <path>` → use as `<asm-analyze-cmd>`
- `venv python: <path>` → use as `<venv-python>`
- `plugin dir: <path>` → use as `<plugin-dir>`
- `LOCI target: <arch>` → use as `<loci_target>` (one of `aarch64`, `armv7e-m`, `armv6-m`, `tc399`)

## Tool boundary: asm-analyze only — never objdump

All assembly and ELF inspection in this skill goes through `<asm-analyze-cmd>`.
Do **not** use `objdump`, `readelf`, `addr2line`, or `nm` as substitutes —
asm-analyze produces LOCI-ready output (per-block CSV, annotated CFG, symbol
map) that the binutils do not. If asm-analyze returns an error, surface it
and stop; do not fall back to objdump or other disassemblers.

Always pass `--arch <loci_target>` on every asm-analyze call, reading the
value verbatim from the SessionStart `LOCI target:` line. Do not guess or
retry with alternative architecture names — the pipeline expects exactly one
of `aarch64`, `armv7e-m`, `armv6-m`, `tc399`.

For example, to extract assembly for functions `function_1` and `function_2` from `filter.elf`:
```
<asm-analyze-cmd> extract-assembly --elf-path filter.elf --functions function_1,function_2 --arch <loci_target>
```
The output is JSON. Use the `timing_csv`, and `timing_architecture` fields from it in step 3.
Use the `control_flow_graph` field when generating analysis results.

## Step 0: Resolve Architecture and Toolchain

The LOCI target architecture is already resolved in the SessionStart context
(`LOCI target:` line). Use it as `<loci_target>`. Do not re-detect it.

1. **User's own compilation** — if the user already compiled targeting a LOCI architecture, reuse their binary. Skip directly to assembly extraction (step 2 of the full compilation path).
2. **Existing ELF/object files** — if the project already has .elf, .out, .o, or .axf files, use them directly.
3. **No context** — ask the user which target, or default to `<loci_target>` from SessionStart context.

### Cross-compilation defaults

Use these defaults only when the user has no existing build. `<loci_target>`
values (`aarch64` / `armv7e-m` / `armv6-m` / `tc399`) are the same vocabulary
used on every asm-analyze command below.

| LOCI target | Compiler | Flags | Build dir |
|---|---|---|---|
| aarch64  | `aarch64-linux-gnu-g++` | `-g -O2 -march=armv8-a`           | `.loci-build/aarch64/`  |
| armv7e-m | `arm-none-eabi-g++`     | `-g -O2 -mcpu=cortex-m4 -mthumb`  | `.loci-build/armv7e-m/` |
| armv6-m  | `arm-none-eabi-g++`     | `-g -O2 -mcpu=cortex-m0plus -mthumb` | `.loci-build/armv6-m/`  |
| tc399    | `tricore-elf-g++`       | `-g -O2 -mcpu=tc3xx`              | `.loci-build/tc399/`    |

In all steps below, replace `<compiler>` and `<flags>` with values from the
resolved LOCI target.

### Timing/energy goes through the LOCI HTTP API

Timing/energy calls use the HTTP API helper at
`<plugin-dir>/lib/api_client.py` (stdlib `urllib`, no extra deps), not the
legacy MCP tool. Do NOT call
`mcp__plugin_loci_loci__get_assembly_block_exec_behavior`.

The helper resolves the bearer token from `$LOCI_API_KEY`, else from
`.loci/config.json` (key `"LOCI_API_KEY"`) in the working directory.
**Pre-flight token check** — before any timing call, verify a token is
reachable:

```
test -n "$LOCI_API_KEY" \
  || jq -e 'has("LOCI_API_KEY") and (.LOCI_API_KEY | length > 0)' .loci/config.json >/dev/null 2>&1 \
  || echo "no LOCI_API_KEY in env or .loci/config.json"
```

If neither source has a token, stop and tell the user:

> No `LOCI_API_KEY` found. Either export it
> (`export LOCI_API_KEY=...`) or create `.loci/config.json` in this
> working directory with `{"LOCI_API_KEY": "..."}`, then re-run.

The helper exits 0 on success (CSV on stdout), 3 (no token), 4 (HTTP
non-2xx), 5 (quota/429), 6 (network). Branch on these in step 4.

## Incremental Path (preferred)

If a previous `.o` exists in `.loci-build/<loci_target>/`, use incremental compilation:

1. Save the existing `.o` as `.o.prev`
2. Compile only the changed source with `-c`.
   Always include `-g` to emit DWARF debug info (required by asm-analyze):
   ```
   <compiler> -g <flags> -c <source> -o .loci-build/<loci_target>/<basename>.o
   ```
3. Diff `.o.prev` vs `.o` to find changed functions:
   ```
   <asm-analyze-cmd> diff-elfs --elf-path .o.prev --comparing-elf-path .o --arch <loci_target>
   ```
4. Extract assembly for only `modified`/`added` functions:
   ```
   <asm-analyze-cmd> extract-assembly --elf-path .o --functions <changed_funcs> --arch <loci_target>
   ```
5. Skip to step 3 (HTTP API call) below.

If no `.o` exists yet, fall through to full compilation.

## Full Compilation Path

1. Cross-compile the target file for the resolved architecture:
   ```
   <compiler> <flags> -o <binary> <source>
   ```
2. Extract assembly with per-block granularity:
   ```
   <asm-analyze-cmd> extract-assembly --elf-path <binary> --functions <func> --blocks blocks.csv --arch <loci_target>
   ```
   The JSON output contains `timing_csv_chunks` (list of per-block CSV chunks like `calculate_0x718,...`) and `timing_architecture`.
3. Send each chunk to `api_client.py exec-behavior` (one call per chunk):
   ```
   echo "<chunk>" | <venv-python> <plugin-dir>/lib/api_client.py exec-behavior \
       --architecture <timing_architecture>
   ```
   `<timing_architecture>` is the value from step 2's JSON output (`A53`,
   `CortexM4`, `CortexM0P`, `TC399`). Run the chunk calls concurrently
   (background Bash calls in a single response, then wait) rather than
   strictly sequentially. Concatenate the result CSVs (skip duplicate
   headers) before reporting.
4. Branch on `api_client.py`'s exit code:
   - **exit 3 (no token)** — stop; emit the no-`LOCI_API_KEY` message from Step 0.
   - **exit 5 (quota/429)** — **stop the skill entirely**. Do not emit the
     report template or footer. Output only:
     ```
     LOCI usage quota reached — timing analysis skipped.

     <stderr body from api_client.py verbatim>
     ```
     Then end the skill. No record/record-measurement calls fire on this path.
   - **exit 4 (HTTP) / 6 (network)** — surface the stderr body. On 401/403,
     tell the user the `LOCI_API_KEY` was rejected. If only some chunks
     failed, report the functions that did resolve and mark coverage partial
     (⚠️ footer); if all failed, treat as a zero-result run (no footer).
5. Report execution time and standard deviation in microseconds, and energy consumption in Watt-seconds (`energy_ws`)
6. When reporting results, 
   - note that these measurements come from LOCI's LCLM trained on real HW traces — they reflect actual silicon behavior on the target board, not theoretical IPC estimates. 
   - High `std_dev_ns` indicates the assembly pattern is underrepresented in the training data; low `std_dev_ns` means strong empirical backing.
   - API response CSV columns are exactly: `function_name`, `std_dev_ns`, `execution_time_ns`, `energy_ws`. There is no bare `std_dev` column — reference column names literally.
   - using the annotated CFG (Control Flow Graphs) from the `control_flow_graph` field from step 2, select a most likely execution path to do performance analysis on with the timing data.
   - highlight the hottest blocks in source code if source code info is available in the annotated CFG.
   - Note for the model (not user-facing): exec-trace's `worst_ns` is a body-only sum and excludes callees because asm-analyze's CFG terminates at every `bl`. Cross-skill comparison with `post-edit` worst_ns isn't apples-to-apples — post-edit is path-traced and includes callee transitions.

7. **Aggregate per-function from the LCLM block CSV + CFG.** For each function `fn` produced by step 2:
   - `worst_ns` = sum of `execution_time_ns` across every block whose `function_name` matches `<fn>_0x*`
   - `happy_ns` = sum along the longest acyclic path through the CFG starting at the entry block (back-edges contribute zero, callees not traced)
   - `energy_uws` = Σ(`energy_ws` × 1e6) across the same blocks (LCLM emits Joules; the schema field is microWatt-seconds = µJ)
   - `src` = the source file most frequently cited in the CFG block annotations for that function (project-relative path; strip absolute prefixes like `/Users/.../<project_root>/` when present, otherwise basename)
   Skip any function whose blocks all returned errors from the API.

7.5. **Look up the previous `worst_ns` and `ts` per function** from the LOCI state JSONL BEFORE the new measurement is appended. Honor `$LOCI_STATE_DIR` if set; otherwise fall back to `~/.loci/state`. Read `cwd_hash` and `branch_slug` from the project-context JSON. One per-function lookup is enough — the JSONL has one row per line:
   ```
   STATE_DIR="${LOCI_STATE_DIR:-$HOME/.loci/state}"
   PREV_LINE=$(grep -F '"fn":"<fn>"' "$STATE_DIR/loci-measurements-<cwd_hash>-<branch_slug>.jsonl" 2>/dev/null | tail -n1)
   if [ -n "$PREV_LINE" ]; then
     PREV_NS=$(printf '%s' "$PREV_LINE" | jq -r '.worst_ns // empty')
     PREV_TS=$(printf '%s' "$PREV_LINE" | jq -r '.ts // empty')
   else
     PREV_NS=""; PREV_TS=""
   fi
   ```
   Empty `PREV_NS` = no prior record (this fn baselines this run). The
   `[ -n "$PREV_LINE" ]` guard avoids feeding empty stdin to `jq`, which
   would print a parse error to stderr.

8. **Synthesise the verdict line** using the regression-based taxonomy in §Verdict semantics below. Render the line as the final line of the report body (just before the voice remark) so the user sees the same string that gets passed to `record --verdict`:
   - All-baseline (zero functions with priors): `Verdict: OK — baseline established for N functions (measurement milestone set, no prior data).`
   - K of N have priors and all `delta_pct ≤ +10%`: `Verdict: OK — K of N within ±10% vs last run (max delta <signed-pct>% on <fn>); <N-K> baselined.` Drop the `; <N-K> baselined` clause when K == N.
   - Any function with priors has `delta_pct > +10%`: `Verdict: CAUTION — <fn> regressed +<pct>% (<prev_ns>→<curr_ns> ns, last run <prev_ts>); <K-1> others stable, <N-K> baselined.` Cite the worst-regressing function. Drop the `, <N-K> baselined` clause when K == N.

   Note: the §LOCI footer skips both record commands when N == 0, so a "FLAG"
   verdict is never persisted. If you want a no-resolved-functions state to
   show up in the dashboard, that's a separate behavior change — for now,
   N == 0 runs exit silently (no footer, no record calls).

## Verdict semantics

Regression-gated, not quality-gated. Quality indicators (std_dev, partial coverage) belong in the report body — they don't drive the verdict. For each analyzed function `fn`:

```
delta_pct(fn) = (current_worst_ns - prev_worst_ns) / prev_worst_ns × 100
```

| verdict | trigger |
|---|---|
| **OK (baseline)** | No prior `worst_ns` exists for ANY analyzed function (zero functions had priors). |
| **OK** | At least one function has prior data and every prior-bearing function has `delta_pct ≤ +10%` (improvements + stable both qualify). Functions without priors are silently treated as baselines and don't contribute to the verdict — they're counted in the line as `<N-K> baselined`. |
| **CAUTION** | At least one function with prior data has `delta_pct > +10%`. Cite the worst-regressing function in the cause. |

`FLAG` is reserved (no functions resolved / total API failure) but is not
currently shipped — the §LOCI footer skips record calls when N == 0, so
the verdict path isn't reached. Quota errors are handled by §4's
early-exit, also without recording.

Verdict line format matches `loci-post-edit`'s exactly:
```
Verdict: <OK|CAUTION> — <one-sentence cause grounded in numbers>
```

## LOCI voice remark

Before the footer, add one short LOCI voice remark (max 15 words) that
acknowledges the user's work grounded in a specific number from the
analysis. Attribute improvements to the user ("clean work", "smart move",
"tight code"). For concerns, be honest and constructive with specifics.
Skip if the analysis produced no results or the user needs raw data only.

## LOCI footer

After reporting timing results and the voice remark, append the footer
as the last thing printed — **only if N > 0**. If no functions were
processed, do NOT emit the footer.

**Record cumulative stats + verdict** (run via Bash before rendering the footer).
Pass `--verdict "<verbatim-verdict-line>"` so the verdict ride-along ships
alongside the per-function trends payload — the line is the same string
already rendered to chat (`Verdict: OK — <cause>`, `Verdict: CAUTION — <cause>`,
or `Verdict: FLAG — <cause>`), unbolded, no surrounding asterisks.
```
<venv-python> <plugin-dir>/lib/loci_stats.py record --context-file "<project-context>" --skill exec-trace --functions <N> --api-calls <M> --co-reasoning 0 --verdict "<verbatim-verdict-line>"
```

**Record per-function measurements** (single Bash call for all functions).
Pipe one JSON object per analyzed function as JSONL via stdin. Skip any
function for which LCLM returned no rows in step 7:
```
echo '<jsonl_records>' | <venv-python> <plugin-dir>/lib/loci_stats.py record-measurement --context-file "<project-context>" --stdin --skill exec-trace
```
Where `<jsonl_records>` is one JSON object per analyzed function:
```
{"fn":"<func1>","worst_ns":<W>,"happy_ns":<H>,"energy_uws":<E>,"src":"<source_file>"}
{"fn":"<func2>","worst_ns":<W>,"happy_ns":<H>,"energy_uws":<E>,"src":"<source_file>"}
```

Both record commands MUST run only when N > 0 — when the skill exits via
the §4 quota path (or any other zero-result path), skip the footer and
the record commands entirely.

Do NOT call `loci_stats.py summary` here. The cumulative branch-stats
line is deliberately removed from skill footers — it is available via
the `trends` skill when the user asks for it.

### Render the footer — compact by default

One line. Icon-led, no surrounding bars, middle-dot separators:

```
<icon> LOCI exec-trace · <N> fn · worst <T>
```

- `<icon>` — `✅` when the run completed with full API data; `⚠️` when
  some blocks were skipped (partial coverage).
- `<N>` — unique functions whose assembly was sent to LOCI.
- `<T>` — worst-case execution time, human-readable unit (ns / µs / ms).

Worked examples:
```
✅ LOCI exec-trace · 2 fn · worst 1.4 µs
⚠️ LOCI exec-trace · 3 fn · worst 780 ns
```

### Expand when...

Replace the compact form with the expanded multi-line form if **any**
of the following is true:
- The API returned partial data (some rows skipped).
- The engineer asked for per-function detail, or multiple functions
  where the one-line summary would hide critical deltas.

Expanded form:
```
─── LOCI · exec-trace ──────────────────
  <N> functions · <M> API calls for execution behavior
────────────────────────────────────────
```

The expanded form does **not** include the cumulative branch-stats line.

- **N** = unique functions whose assembly was sent to LOCI
- **M** = HTTP API calls to `POST /mcp/v1/get_assembly_block_exec_behavior` (one per timing CSV chunk)
