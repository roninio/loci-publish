# LOCI Usage Examples

LOCI runs two skills automatically on every Claude Code session — no slash command needed. Four additional skills are available on demand.

Each example shows a complete LOCI interaction — the trigger phrase, what LOCI does internally, and the output format you will see in Claude Code.

---

### 1. Quality Gate for AI Coding Agents

**Trigger:** Describe new logic during `/plan`

> "Add a retry mechanism to `uart_send`."

LOCI's plan analysis audits the plan before Claude writes a single line. It checks timing impact, energy budget, and stack depth against your quality contracts and returns a verdict:

- **GOOD** — plan is safe to proceed
- **ADJUST PLAN** — specific concern flagged with a recommendation
- **STOP** — regression risk is high; plan should be reworked

**Trigger:** Any source file edit (automatic, no command needed)

LOCI postflight compiles the changed file, diffs the binary, and reports the delta:

    uart_send
      Before:  1.24 µs   After:  1.31 µs   Δ +5.6%   ⚠ CAUTION

Both skills run without instrumentation, profilers, or a connected board — analysis comes directly from the compiled binary.

---

### 2. Regression Prevention Gate

**Trigger:** Ask after any code change

> "Did my last edit regress anything?"

LOCI compiles both versions of the file, diffs the ELF binaries, and surfaces timing and energy changes at the function level — before any test suite runs.

    Postflight: sensor_driver.c

    Function                Before     After      Delta    Status
    process_sensor_data     1.24 µs    1.31 µs    +5.6%    ⚠
    filter_apply            0.88 µs    0.82 µs    -6.8%    ✅

Regressions are caught at the binary level, not the source level — changes that look clean in a source diff can still alter execution behavior.

---

### 3. Execution-Aware Optimization

**Trigger:** Ask about any function in your firmware

> "What is the execution cost of `motor_control_loop`?"

LOCI extracts the compiled assembly, calls LCLM — Aurora Labs' Large Code Language Model trained on billions of ASM blocks and real hardware traces — and returns cycle-accurate timing and energy grounded in actual silicon behavior, not simulation.

    motor_control_loop
      Execution time:  3.82 µs  (std dev: 0.12 µs)
      Energy:          0.0041 Ws
      Hottest block:   pid_update — 61% of total cycles

Low standard deviation means strong empirical backing. High standard deviation flags that the assembly pattern is underrepresented and the estimate should be treated with caution.

---

### 4. Functional Safety & System Availability

**Trigger:** Ask before deploying to a safety-critical or RTOS target

> "Is the stack safe for `TaskMain` with a 2048-byte budget?"

LOCI traverses the full call graph from the compiled ELF, reports worst-case stack depth along the deepest path, and gives a pass/fail verdict. No board required. No instrumentation. No runtime.

    Stack Depth: TaskMain

      Worst-case depth:   312 bytes
      Worst-case path:    TaskMain → process_data → decode → crypto_verify
      Budget:             2048 bytes  (15.2% used)

      Verdict: ✅ PASS 15.2%

For RTOS projects, LOCI auto-detects task entry points from `xTaskCreate`,
`Task_construct`, `osThreadNew`, and `FreeRTOSConfig.h`. Stack budget
violations — the leading cause of hard faults on embedded targets — are
surfaced at compile time.

---

### 5. Control-Flow Integrity

**Trigger:** Ask when you want to inspect a function's call graph or verify control-flow integrity

> "Show me the call graph for `process_data()`."

LOCI extracts the annotated control-flow graph directly from the compiled binary — every branch, every call edge, every indirect dispatch site. No source code required. No instrumentation. No runtime.

    Control-Flow Analysis: process_data()

      Call graph:
        process_data()
          ├─ validate_header()        [direct]
          │    └─ crc32_compute()     [direct]
          ├─ dispatch_handler()       [direct]
          │    └─ r3 → ??            [indirect · bl r3]  ⚠
          ├─ apply_filter()           [direct]
          └─ finalize_output()        [direct]

      CFG edges: 6 · depth: 3 levels · indirect calls: 1

      Findings:
        ⚠  dispatch_handler — indirect call via register (bl r3)
           CFI hazard: target not resolvable from binary. Verify dispatch
           table is bounds-checked before this path is reachable.

      control-flow shape: cycles no · indirect 1 · recursion no

      Verdict: ⚠ findings — 1 item flagged

Indirect calls through registers (`bl r3`, `blx r2`) are the finding class code review alone can't see — the source shows a clean function-pointer call, but only the binary reveals whether the target is statically resolvable or reachable from attacker-controlled input. LOCI flags them with the specific call site so you can verify the dispatch table is bounds-checked before merge.

A clean graph returns `✅ clean` with no table. Unbounded recursion or broken linkage returns `❌ blocking` and names the function.

`/control-flow` requires no MCP connection and no network access — it runs entirely from your compiled binary.
