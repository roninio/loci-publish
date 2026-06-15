---
name: control-flow
description: Create annotated CFG (Control Flow Graphs) in text format optimised for LLM analysis on compiled assembly code to provide execution insights. Use when the user asks about call dependencies, function impact, or control flow analysis from compiled code.
when_to_use: When user asks about call dependencies, function impact, or control flow analysis from compiled code.
---

# LOCI Control Flow Analysis

Read these values from the LOCI session context (system-reminder block at session start) and substitute them wherever the placeholders appear below:
- `asm-analyze command: <path>` → use as `<asm-analyze-cmd>`
- `venv python: <path>` → use as `<venv-python>`
- `plugin dir: <path>` → use as `<plugin-dir>`
- `LOCI target: <arch>` → use as `<loci_target>` (one of `aarch64`, `armv7e-m`, `armv6-m`, `tc399`)

## Tool boundary: asm-analyze only — never objdump

All assembly and ELF inspection in this skill goes through `<asm-analyze-cmd>`.
Do **not** use `objdump`, `readelf`, `addr2line`, or `nm` as substitutes —
asm-analyze produces LOCI-ready annotated CFG output that binutils cannot.
If asm-analyze returns an error, surface it and stop; do not fall back to
objdump or other disassemblers.

Always pass `--arch <loci_target>` on every asm-analyze call, reading the
value verbatim from the SessionStart `LOCI target:` line.

For example, to generate annotated CFG for a function called `apply_filter` from `filter.elf`:
```
<asm-analyze-cmd> extract-cfg --elf-path filter.elf --functions apply_filter --arch <loci_target>
```
The output is in a text format optimized for LLM analysis. Use it in step 5.

## Step 0: Resolve Architecture and Toolchain

The LOCI target architecture is already resolved in the SessionStart context
(`LOCI target:` line). Use it as `<loci_target>`. Do not re-detect it.

1. **User's own compilation** — if the user already compiled targeting a LOCI architecture, reuse their binary. Skip directly to CFG extraction (step 2 of the full compilation path).
2. **Existing ELF/object files** — if the project already has .elf, .out, .o, or .axf files, use them directly.
3. **No context** — ask the user which target, or default to `<loci_target>` from SessionStart context.

### Cross-compilation defaults

Use these defaults only when the user has no existing build.

| LOCI target | Compiler | Flags | Build dir |
|---|---|---|---|
| aarch64  | `aarch64-linux-gnu-g++` | `-g -O2 -march=armv8-a`              | `.loci-build/aarch64/`  |
| armv7e-m | `arm-none-eabi-g++`     | `-g -O2 -mcpu=cortex-m4 -mthumb`     | `.loci-build/armv7e-m/` |
| armv6-m  | `arm-none-eabi-g++`     | `-g -O2 -mcpu=cortex-m0plus -mthumb` | `.loci-build/armv6-m/`  |
| tc399    | `tricore-elf-g++`       | `-g -O2 -mcpu=tc3xx`                 | `.loci-build/tc399/`    |

In all steps below, replace `<compiler>` and `<flags>` with values from the
resolved LOCI target.

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
4. Generate CFG's (Control Flow Graphs) for only `modified`/`added` functions:
   ```
   <asm-analyze-cmd> extract-cfg --elf-path .o --functions <changed_funcs> --arch <loci_target>
   ```
   The output is in a text format optimized for LLM analysis. Use it in step 5.
5. Report change analysis based on the generated graphs.

If no `.o` exists yet, fall through to full compilation.

## Full Compilation Path

1. Cross-compile the target file for the resolved architecture:
   ```
   <compiler> <flags> -o <binary> <source>
   ```
2. Extract annotated CFG's for analysis:
   ```
   <asm-analyze-cmd> extract-cfg --elf-path <binary> --functions <func> --arch <loci_target>
   ```
   The output is in a text format optimized for LLM analysis. Use it in step 3.
3. Report analysis for selected functions based on the generated CFG's

## LOCI voice remark

Before the footer, add one short LOCI voice remark (max 15 words) that
acknowledges the user's work grounded in a specific number from the
analysis. Attribute improvements to the user ("clean work", "smart move",
"tight code"). For concerns, be honest and constructive with specifics.
Skip if the analysis produced no results or the user needs raw data only.

## LOCI footer

After the control-flow analysis and voice remark, append the footer as
the last thing printed — **only if N > 0**. If no functions were
processed, do NOT emit the footer.

**Record cumulative stats + verdict** (run via Bash before rendering the footer).
Pass `--verdict "<verbatim-verdict-line>"` so the gate outcome ships alongside
the trends payload on the next Stop-hook flush. The line follows the same
shape used in the expanded footer (`Verdict: <CLEAN|FINDINGS|BLOCK> — <one-line summary>`),
synthesised regardless of whether the compact or expanded form is rendered to
chat — the `<one-line summary>` should match the compact footer's `<shape>`
field (e.g., `clean`, `2 indirect`, `unbounded recursion`). Pass it unbolded,
no surrounding asterisks.
```
<venv-python> <plugin-dir>/lib/loci_stats.py record --context-file "<project-context>" --skill control-flow --functions <N> --api-calls 0 --co-reasoning 0 --verdict "<verbatim-verdict-line>"
```

Worked examples of `<verbatim-verdict-line>`:
```
Verdict: CLEAN — no findings across 3 functions
Verdict: FINDINGS — 2 indirect call sites on non-ISR paths
Verdict: BLOCK — unbounded recursion in parser_descend
```

Do NOT call `loci_stats.py summary` here. The cumulative branch-stats
line is deliberately removed from skill footers — it is available via
the `trends` skill when the user asks for it.

### Render the footer — compact by default

One line. Icon-led, no surrounding bars, middle-dot separators:

```
<icon> LOCI control-flow · <N> fn · <shape>
```

- `<icon>` — `✅` when the analysis is clean (no unbounded cycles, no
  unresolved indirect calls in a context that forbids them); `⚠️` when
  non-critical findings surface (indirect calls on non-ISR paths,
  bounded recursion); `❌` when unbounded recursion or CFI violations
  are found.
- `<shape>` — one of: `clean` (no findings), `<K> cycles` (K
  back-edges/loops reported), `<K> indirect` (K indirect-call sites
  flagged), or a combined `<K> cycles · <L> indirect`.

Worked examples:
```
✅ LOCI control-flow · 3 fn · clean
⚠️ LOCI control-flow · 5 fn · 2 indirect
❌ LOCI control-flow · 1 fn · unbounded recursion
```

### Expand when...

Replace the compact form with the expanded multi-line form if the
verdict is `⚠️` or `❌` **and** the per-function findings need the
room (e.g. several flagged functions or a mix of cycles and indirect
calls that a one-line shape description cannot fairly summarize).

Expanded form:
```
─── LOCI · control-flow ────────────────
  <N> functions analyzed
  Verdict: <CLEAN | FINDINGS | BLOCK> — <one-line summary>
────────────────────────────────────────
```

The expanded form does **not** include the cumulative branch-stats line.

- **N** = unique functions whose CFG was extracted and analyzed
