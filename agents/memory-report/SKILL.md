---
name: memory-report
description: >
  ROM/RAM memory usage analysis for embedded firmware: section breakdown, top
  consumers, and region budgets from compiled ELF binaries.
when_to_use: >
  When user says "memory report", "ROM/RAM usage", "how much flash/RAM",
  "memory footprint", "memory map", "memory delta", "size impact". Do NOT
  invoke for web/script projects without flash/ROM/RAM constraints.
---

# LOCI Memory Report

Read these values from the LOCI session context (system-reminder block at session start) and substitute them wherever the placeholders appear below:
- `asm-analyze command: <path>` → use as `<asm-analyze-cmd>`
- `venv python: <path>` → use as `<venv-python>`
- `plugin dir: <path>` → use as `<plugin-dir>`
- `LOCI target: <arch>` → use as `<loci_target>` (one of `aarch64`, `armv7e-m`, `armv6-m`, `tc399`)

## Tool boundary: asm-analyze only — never objdump

All assembly, section, and symbol inspection in this skill goes through
`<asm-analyze-cmd> memmap`. Do **not** use `objdump`, `size`, `readelf`,
`nm`, or `addr2line` as substitutes — asm-analyze parses section, symbol,
and map-file data into the structured report this skill expects; binutils
output is not equivalent. If asm-analyze returns an error, surface it and
stop; do not fall back to binutils.

The `memmap` subcommand auto-detects architecture from the ELF and does not
accept `--arch`. For all other asm-analyze subcommands used alongside
memmap (e.g. `extract-symbols` for validation), pass `--arch <loci_target>`
verbatim from the SessionStart `LOCI target:` line.

## Step 0: Check Session Context

Read architecture and compiler from the LOCI session context (the
`system-reminder` block emitted at session start). Look for:

    Target: <target>, Compiler: <compiler>, Build: <build>
    LOCI target: <loci_target>

Map the LOCI target to supported architectures:

| LOCI target | CPU |
|---|---|
| aarch64 | A53 |
| armv7e-m | CortexM4 |
| armv6-m | CortexM0P |
| tc399 | TC399 |

If the architecture is not in this table, emit and stop:

    Supported: aarch64, armv7e-m, armv6-m, tc399

If no compiler was detected, inform the user and stop.

Do not re-run detection scripts — use the values already in the session context.

If the user provides their own binary (.elf, .out, .o, .axf), asm_analyze.py
auto-detects architecture from the ELF.

## Step 1: Identify the Binary and Optional Map File

Determine which binary to analyze:

1. **User provides a binary** — use it directly
2. **Build from source** — cross-compile for the resolved architecture:
       <compiler> <flags> -o .loci-build/<arch>/<basename>.elf <source>
   For per-file analysis, compile with `-c` to get a `.o` file.

If a linker `.map` file is available (often next to the ELF), the user may
provide its path for region budget analysis. Supported map file formats:

- **GCC / GNU ld** (also used by TI toolchains) — "Memory Configuration" section
- **IAR EWARM** — "PLACEMENT SUMMARY" section with `place in [start-end]` entries
- **Keil / ARM Compiler (armlink)** — "Execution Region" entries with Base/Max

The parser auto-detects the format. If a `--map-file` was passed but
parsing failed (file missing, unreadable, or unrecognized format), the
report completes without region budgets AND a structured entry appears
in the JSON `warnings` array — see "Map-file warnings" below.

## Step 2: Run Memory Map Analysis

### Single report — full ELF binary

    <asm-analyze-cmd> memmap --elf-path <binary> [--map-file <path.map>] [--top-n 10]

### Single report — relocatable .o file

    <asm-analyze-cmd> memmap --elf-path <file.o>

For `.o` files: section sizes are reported but memory regions are not available
(no linker placement). Map files are not applicable.

### Delta comparison — two ELF binaries or two .o files

    <asm-analyze-cmd> memmap --elf-path <old_binary> --comparing-elf-path <new_binary> [--map-file <path.map>]

Use this to compare before/after a code change. The `--elf-path` is the
**base** (old) binary and `--comparing-elf-path` is the **current** (new)
binary — same convention as `diff-elfs`. The reported delta is signed
`new − old`, so a positive delta means growth.

### Incremental .o delta (preferred for per-file checks)

Use this when checking if a change to a single file affected memory usage.
Works on individual `.o` object files without needing a fully linked binary.

1. If a previous `.o` exists, save it as `.o.prev` (this is the **base** —
   the state *before* the change).
2. Compile only the changed source with `-c`.
   Always include `-g` to emit DWARF debug info (required by asm-analyze):
       <compiler> -g <flags> -c <source> -o .loci-build/<arch>/<basename>.o
3. Run delta comparison — base (`.o.prev`) is `--elf-path`, current
   (`.o`) is `--comparing-elf-path`:
       <asm-analyze-cmd> memmap --elf-path .loci-build/<arch>/<basename>.o.prev --comparing-elf-path .loci-build/<arch>/<basename>.o

This gives fast feedback on whether a change grew ROM/RAM without needing a full link.

### Optional flags

- `--comparing-elf-path <path>` — current/changed ELF for delta comparison
  (delta is computed as `comparing_elf − elf_path`, i.e. *new − old*)
- `--map-file <path>` — GCC linker map file; enables region budgets with usage %
- `--top-n <N>` — number of top consumers per category (default 10)
- `--with-heap` — opt-in heap allocation analysis: per-caller direct calls
  to known allocators (`malloc`, `calloc`, `free`, `pvPortMalloc`,
  `mbedtls_calloc`, `_Znwm`, ...) with static-size extraction where the
  size is a literal at the call site. Adds a `heap` (single mode) or
  `heap_delta` (delta mode) field to the JSON output. Currently supported
  on `aarch64` and `armv7e-m`/`armv6-m`; other architectures return an
  empty heap section.
- `--allocators-file <path>` — newline-separated list of allocator symbol
  names. Replaces the built-in catalog (use to track project-specific
  allocators, e.g. `os_malloc`, `tx_byte_allocate`). Lines starting with
  `#` are treated as comments.

### JSON output

**Single report** (`mode: "report"`):
- `sections` — per-section breakdown (name, address, size, type, flags, memory region)
- `summary` — ROM total, RAM static total, code/rodata/data/bss sizes
- `top_consumers` — largest functions (ROM) and variables (RAM)
- `memory_regions` — only when `--map-file` was provided AND parsing succeeded:
  per-region origin, length, used, usage_pct. `null` otherwise.
- `warnings` — list of structured `{code, path, detail}` entries.
  Always present (empty list when there are none). See "Map-file warnings".
- `heap` — only when `--with-heap` provided: `{totals: {alloc_sites, static_bytes, dynamic_sites, by_callee}, per_function, top_callers}`

**Delta report** (`mode: "delta"`):
- `section_deltas` — per-section before/after/delta/delta_pct
- `summary_delta` — ROM/RAM totals with before/after/delta
- `symbol_deltas` — added/removed/changed symbols sorted by delta size
- `memory_regions_delta` — only when `--map-file` was provided AND parsing succeeded.
  `null` otherwise.
- `warnings` — list of structured `{code, path, detail}` entries.
  Always present (empty list when there are none). See "Map-file warnings".
- `heap_delta` — only when `--with-heap` provided: `{alloc_sites_before, alloc_sites_after, static_bytes_before, static_bytes_after, dynamic_count_before, dynamic_count_after, added: [...], removed: [...]}`

### Map-file warnings

When `--map-file` was supplied but parsing did not yield region data, the
JSON `warnings` array contains a structured entry with one of these codes:

| `code` | Meaning |
|---|---|
| `MAP_FILE_NOT_FOUND` | The path given to `--map-file` does not exist. |
| `MAP_FILE_UNREADABLE` | The file exists but could not be opened (permissions, I/O error). |
| `MAP_FORMAT_UNRECOGNIZED` | The file was read but its header matched none of the supported formats (gcc-ld, iar, armlink). |
| `MAP_FILE_IGNORED_RELOCATABLE` | `--map-file` was given but the input is a relocatable `.o` — region budgets are not applicable. |

Each entry also carries `path` (the offending file) and `detail` (a
human-readable explanation, including the first line of the file for the
`MAP_FORMAT_UNRECOGNIZED` case). Render any non-empty `warnings` array as
a "Map-file notes" section immediately above the Conclusion table (see
Step 3). Do **not** silently drop them: a missing region-budgets table
combined with a silent warning would let CI gates pass a degraded report.

## Step 3: Report Results

### Section Breakdown

    ## Memory Report: <binary_name>

    Architecture: <arch>
    ELF type:     <executable | relocatable>

    ### Section Breakdown

    Section          Address      Size       Type     Region
    .text            0x08000000   14,832 B   code     ROM
    .rodata          0x0800XXXX    2,048 B   rodata   ROM
    .data            0x20000000      512 B   data     RAM
    .bss             0x20000200    4,096 B   bss      RAM

### Summary

    ### ROM/RAM Summary

    ROM total:        16,896 B  (code: 14,832  rodata: 2,064)
    RAM static total:  4,608 B  (data: 512  bss: 4,096)

### Top Consumers

    ### Top ROM Consumers (by size)

      1. main                    1,248 B  (function)
      2. process_data              896 B  (function)
      3. init_peripherals          784 B  (function)

    ### Top RAM Consumers (by size)

      1. rx_buffer               2,048 B  (variable)
      2. config                    512 B  (variable)

### Heap Allocations (only when `--with-heap`)

Render this section after Top Consumers when the JSON contains a `heap`
field. Skip the block entirely when `heap.totals.alloc_sites == 0`.

    ### Heap Allocations

      caller            callee          size
      parse_packet      malloc          128 B
      init_buffers      calloc          dynamic
      taskAlloc         pvPortMalloc    512 B
      cleanup           free            —

    Total: 4 sites · 640 B static · 1 dynamic

Notes:
- One row per `AllocSite` entry in `heap.per_function`. Order rows by
  caller using `heap.top_callers` first (highest site count), then any
  remaining functions; cap at 10 rows.
- `size` is the literal value when statically resolvable, or the string
  `dynamic` when the allocator is called with a variable argument (size
  came from a register or computation that the static lookback couldn't
  trace through).
- `free`-family callees (`free`, `_free_r`, `vPortFree`, `mbedtls_free`,
  `_ZdlPv`, `_ZdaPv`) render with `—` for size since they release rather
  than allocate. They count toward the site total but contribute zero
  bytes and zero dynamic.

### With Map File (region budgets)

    ### Memory Region Budgets

    Region    Used / Total          Usage
    FLASH     16,896 / 1,048,576   1.6%
    RAM        4,608 /   131,072   3.5%
    CCMRAM         0 /    65,536   0.0%

### For .o files (no linked addresses)

    ## Memory Report: sensor_driver.o (relocatable)

    Note: Addresses are zero-based (no linker placement).
    Memory regions are not available for object files.

    Section          Size       Type
    .text            1,248 B    code
    .rodata            128 B    rodata
    .data               32 B    data
    .bss               256 B    bss

    ROM estimate:   1,376 B  (code: 1,248  rodata: 128)
    RAM estimate:     288 B  (data: 32  bss: 256)

### Delta report (two binaries compared)

    ## Memory Delta: old.elf -> new.elf

    Architecture: cortexm

    ### Section Deltas

    Section          Before       After        Delta
    .text            14,832 B     15,200 B     +368 B  (+2.5%)
    .rodata           2,048 B      2,048 B        0 B  (0.0%)
    .data               512 B        640 B     +128 B  (+25.0%)
    .bss              4,096 B      4,096 B        0 B  (0.0%)

    ### Summary

    ROM total:       16,880 B -> 17,248 B   +368 B  (+2.2%)
    RAM static:       4,608 B ->  4,736 B   +128 B  (+2.8%)

    ### Top ROM Growth (by delta)

      1. new_function         +368 B  (added)
      2. process_data         +128 B  (896 -> 1024)

    ### Top RAM Growth (by delta)

      1. new_buffer           +128 B  (added)

### Incremental .o delta

    ## Memory Delta: driver.o.prev -> driver.o

    Section          Before       After        Delta
    .text               896 B      1,024 B     +128 B  (+14.3%)
    .bss                256 B        256 B        0 B  (0.0%)

    ROM estimate:    +128 B  (+14.3%)
    RAM estimate:       0 B  (0.0%)

    ### Changed Symbols

      process_data:   +128 B  (896 -> 1024)

### With map file in delta mode

    ### Memory Region Budget Delta

    Region    Before             After              Delta
    FLASH     16,880 / 2,097,152 (0.8%)   17,248 / 2,097,152 (0.8%)   +368 B
    RAM        4,608 /   262,144 (1.8%)    4,736 /   262,144 (1.8%)   +128 B

### Heap Allocation Delta (only when `--with-heap`)

Render when the JSON contains a `heap_delta` field. Skip when both
`added` and `removed` are empty AND `dynamic_count_after ==
dynamic_count_before`.

    ### Heap Allocation Delta

      Added:
        parse_packet  -> malloc(128)
        init_buffers  -> calloc(dynamic)
      Removed:
        legacy_init   -> malloc(64)

    Net: +2 sites, +192 B static, +1 dynamic

Net line is computed from the totals: `+(after-before) sites,
+(static_bytes_after - static_bytes_before) B static,
+(dynamic_count_after - dynamic_count_before) dynamic`. Use signed
formatting (`+N` / `-N`).

### Map-file notes (only when `warnings[]` is non-empty)

If the JSON `warnings` array contains entries with map-file codes
(`MAP_FILE_NOT_FOUND`, `MAP_FILE_UNREADABLE`, `MAP_FORMAT_UNRECOGNIZED`,
`MAP_FILE_IGNORED_RELOCATABLE`), render them immediately above the
Conclusion table so the user knows the region-budgets table is missing
on purpose, not by oversight:

    ### Map-file notes
    - ⚠️ MAP_FORMAT_UNRECOGNIZED: <path>
      Header matched no known format (tried: gcc-ld, iar, armlink).
      First line: 'TI ARM Clang Linker PC v2.1.3'

One bullet per warning, with the `detail` field on a continuation line.
The Conclusion table verdict must reflect that no region budgets were
computed (do not claim "PASS <x>%" when `memory_regions` is null because
of a map-file warning — use the no-budget verdict form instead).

## Conclusion table

After the section breakdown, top-consumers, and (optional) region/delta blocks,
emit a single conclusion table that summarises the memory verdict. Include
only rows that apply this run. Every ⚠️ / ❌ row MUST cite a concrete reason
in the Note column.

Icon vocabulary: ✅ PASS · ⚠️ WARNING · ❌ FAIL.

### Row catalogue (order when present)

1. **ROM usage** — always, when ROM total is computable. Status by region
   usage when a map file was provided:
   - ✅ `usage_pct < 50%` (or, without map file, if total seems comfortable
     for the target)
   - ⚠️ `50% ≤ usage_pct ≤ 80%`
   - ❌ `usage_pct > 80%`
   Note cites the percentage + total bytes.
2. **RAM static total** — same rules as ROM, but for RAM.
3. **Largest single symbol** — only when one symbol is ≥ 25% of its region
   total. Actionable: its name in the Note so the engineer knows where to
   look first. Status: ⚠️ unless the allocation is clearly intentional
   (e.g., a known flash buffer).
4. **Region delta** (delta mode only) — one row per region that grew.
   Status by delta size vs the region's available headroom. Before/After
   columns if the mode supports them.
5. **Section growth concerns** (delta mode only) — one row per section
   that grew by > 20% of its previous size. Status ⚠️ with the growing
   section name + delta_pct in the Note.
6. **Heap allocations** (only when `--with-heap` was used).
   - **Single mode:** include the row only when `heap.totals.alloc_sites
     > 0`. Status: ⚠️ when `dynamic_sites > 0` (variable-size allocations
     are hardest to bound on embedded targets); ✅ otherwise. Note cites
     `<sites> sites · <static_b> B static · <dynamic> dynamic`.
   - **Delta mode:** include the row when any of `alloc_sites_after`,
     `static_bytes_after`, `dynamic_count_after` differs from its
     `_before` counterpart, OR when `added`/`removed` is non-empty.
     Status: ❌ when `dynamic_count_after > dynamic_count_before` (a new
     unknown-size allocation is the highest-risk delta on embedded);
     ⚠️ when `alloc_sites_after > alloc_sites_before` without a new
     dynamic allocation; ✅ otherwise (sites unchanged or shrunk). Note
     cites the net change, e.g. `+2 sites · +192 B · +1 dynamic`.

Omit "ROM usage is clean" / "RAM is clean" rows when they would just
restate the Summary block above — include them only when the values are
actionable (near or over threshold).

Table footer: bolded single-line verdict.
- With map file: `Verdict: **PASS** <top-region-usage>%` ·
  `**CAUTION** <top-region-usage>%` · `**FAIL** <top-region-usage>%`
- Without map file: `Verdict: **PASS** — ROM <X> B / RAM <Y> B` · or a
  CAUTION/FAIL equivalent when a row flagged a concern.

### Example (delta mode, with map file)

```
### Conclusion
| Gate                 | Before             | After              | Status | Note                              |
|----------------------|--------------------|--------------------|:------:|-----------------------------------|
| ROM usage            | 16,880 / 2,097,152 | 17,248 / 2,097,152 |   ✅   | 0.8% → 0.8%                        |
| RAM static total     |  4,608 /   262,144 |  4,736 /   262,144 |   ✅   | 1.8% → 1.8%                        |
| Section growth       |     512 B          |     640 B          |   ⚠️   | .data +25%                         |

Verdict: **PASS** 1.8%
```

### Escalation fold-back

When memory-report is invoked as an ESCALATION from loci-preflight or
loci-post-edit, still emit the full Conclusion table above, AND hand back
to the parent skill a one-line summary in the form:
`memory: ROM <X>% / RAM <Y>% — <PASS|CAUTION|FAIL>`. The parent skill
folds that line into its own "Memory escalation" row.

## LOCI voice remark

Before the footer, add one short LOCI voice remark (max 15 words) that
acknowledges the user's work grounded in a specific number from the
analysis. Attribute improvements to the user ("clean work", "smart move",
"tight code"). For concerns, be honest and constructive with specifics.
Skip if the analysis produced no results or the user needs raw data only.

## LOCI footer

After emitting the memory report (single or delta) and the voice
remark, append the footer as the last thing printed — **only if
N > 0**. If no symbols were processed, do NOT emit the footer.

**Record cumulative stats + verdict** (run via Bash before rendering the footer).
Pass `--verdict "<verbatim-verdict-line>"` so the gate outcome ships alongside
the per-function trends payload on the next Stop-hook flush — the line is the
same string already rendered in the conclusion-table footer
(`Verdict: PASS <top-region-usage>%`, `Verdict: CAUTION <top-region-usage>%`,
`Verdict: FAIL <top-region-usage>%`, or — without a map file —
`Verdict: PASS — ROM <X> B / RAM <Y> B`). Pass it unbolded, no surrounding
asterisks.
```
<venv-python> <plugin-dir>/lib/loci_stats.py record --context-file "<project-context>" --skill memory-report --functions <N> --api-calls 0 --co-reasoning 0 --verdict "<verbatim-verdict-line>"
```

**Record per-function measurements** (single Bash call for all top ROM consumers).
Pipe all measurements as JSONL via stdin. Only record functions (not variables).
Skip if the report is a delta-only view with no absolute sizes.
```
echo '<jsonl_records>' | <venv-python> <plugin-dir>/lib/loci_stats.py record-measurement --context-file "<project-context>" --stdin --skill memory-report
```
Where `<jsonl_records>` is one JSON object per line for each function from the
top ROM consumers list:
```
{"fn":"<func>","rom_b":<size_bytes>,"src":"<source_file>"}
```

When `--with-heap` was used, append the heap fields for each function that
appears in `heap.per_function` — `heap_sites` (count of alloc sites in
that function) and `heap_static_b` (sum of statically-resolvable
allocation sizes for that function):
```
{"fn":"<func>","rom_b":<bytes>,"heap_sites":<n>,"heap_static_b":<bytes>,"src":"<source>"}
```
Functions without any allocator calls should not get heap fields at all
(omit them rather than emitting zeros — keeps JSONL queries simple).

Do NOT call `loci_stats.py summary` here. The cumulative branch-stats
line is deliberately removed from skill footers — it is available via
the `trends` skill when the user asks for it.

### Render the footer — compact by default

One line. Icon-led, no surrounding bars, middle-dot separators:

```
<icon> LOCI memory-report · ROM <X>% · RAM <Y>%
```

- `<icon>` — `✅` when every region is under its warning threshold;
  `⚠️` when any region is 70–90% full; `❌` when any region is ≥90%.
- `<X>` / `<Y>` — region usage as percent-of-budget when a linker map /
  region budget is available. When no budget is available, drop the
  `%` suffix and report the absolute byte delta instead (e.g.
  `ROM +24 B · RAM 0 B` for a delta view).

Worked examples:
```
✅ LOCI memory-report · ROM 42% · RAM 58%
⚠️ LOCI memory-report · ROM 72% · RAM 58%
❌ LOCI memory-report · ROM 94% · RAM 58%
✅ LOCI memory-report · ROM +24 B · RAM 0 B
```

When `--with-heap` was used and the heap section was non-trivial (single
mode: `alloc_sites > 0`; delta mode: any non-zero net change), append a
`Heap` segment to the footer in the same middle-dot format. Examples:
```
✅ LOCI memory-report · ROM 42% · RAM 58% · Heap 0 sites
⚠️ LOCI memory-report · ROM 42% · RAM 58% · Heap 7 sites · 1 dynamic
❌ LOCI memory-report · ROM 42% · RAM 58% · Heap +2 sites · +1 dynamic
```
The Heap segment uses the same icon as the leading status (it does not
override the ROM/RAM icon — the worst gate wins, and the icon is selected
from whichever Conclusion row produced it). When the heap row was elided
(none of the conditions in the Conclusion catalogue triggered), omit the
Heap segment too.

### Fold-back to parent (escalation mode)

When memory-report was invoked as an escalation from `preflight` /
`post-edit`, emit the full footer as described above AND hand the
parent a one-line summary for fold-back:

```
memory: ROM <X>% / RAM <Y>% — <PASS|CAUTION|FAIL>
```

The parent skill renders its own compact or expanded footer based on
whether this fold-back was clean.

### Expand when...

Replace the compact form with the expanded multi-line form if **any**
region is ≥70% of its budget, or the report is a cross-build delta
where the engineer needs per-region breakdown to interpret the change.

Expanded form:
```
─── LOCI · memory-report ──────────────
  <N> symbols (functions + variables) analyzed
  Verdict: <PASS | CAUTION | FAIL> — <one-line summary>
────────────────────────────────────────
```

The expanded form does **not** include the cumulative branch-stats line.

- **N** = unique symbols (functions + variables) reported in the top consumers or changed symbols sections
