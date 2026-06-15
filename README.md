# LOCI

Agent Skills for execution-aware firmware analysis.

AI Writes Code. LOCI Gates It.

LOCI's quality gate agent models regressions, power, latency, and bugs from the binary. From plan to merge. 

Without running code. No instrumentation. No code changes.

## Prerequisites

| Requirement | Version | Required for |
|-------------|---------|-------------|
| [Claude Code](https://claude.ai/code) | latest | everything |
| Python | 3.12+ | asm-analyze (local ELF analysis) |
| [uv](https://docs.astral.sh/uv/) | any | Python venv setup — auto-installed by `setup.sh` |
| jq | any | session hooks — auto-installed by `setup.sh` |
| Compiled binaries | `.elf` / `.o` / `.axf` | all skills |
| Network access to `https://app.auroralabs.com` | — | `exec-trace`, `loci-preflight`, `loci-post-edit` |

**Cross-compiler** (one required, depending on your target):

| Target | Compiler |
|--------|----------|
| ARM Cortex-M | `arm-none-eabi-gcc` |
| ARM Cortex-A | `aarch64-linux-gnu-gcc` |
| TriCore | `tricore-elf-gcc` |
| TI ARM | `tiarmclang` or `armcl` |
| x86/x64 | `g++` or `clang++` |

Skills that work without a cross-compiler or MCP: `stack-depth`, `memory-report`, `control-flow`

## Install

```
/plugin marketplace add auroralabs-loci/loci-claude
/plugin install loci@loci
```

## Quick Start

After installing, try these in any C/C++/Rust project with compiled binaries.

AI Writes Code. LOCI Gates It.
1. **Timing & energy** — ask: *"What's the execution cost of main()?"*
2. **Memory budget** — ask: *"How much ROM/RAM does my build use?"*
3. **Stack safety** — ask: *"Is my stack safe for TaskMain?"*
4. **Control-flow safety** — ask: *"What does the call graph for process_data() look like?"*

LOCI also runs automatically:
- **loci-preflight** fires during `/plan` - analyzes callees at the binary level before code is written.
- **loci-post-edit** fires after every edit - diffs the binary and returns a regression verdict.

## Skills

Gate — Human Decides. Define what matters. LOCI enforces it.

LOCI is packaged in the Agent Skills format: each capability lives in
`skills/<name>/SKILL.md` with discovery metadata (`name` and `description`) and
task-specific instructions. Agents can discover the available skills at startup
and load the full instructions only when the user's task matches a skill.

```
skills/
├── exec-trace/
│   └── SKILL.md
├── stack-depth/
│   └── SKILL.md
└── ...
```

Shared plugin code stays in `lib/`; per-skill fixtures and evaluations live
inside that skill folder when needed.

Workspace custom agents mirror the same skills under `agents/`. Each
`<skill>.agent.md` file is a thin VS Code agent wrapper that loads and follows
the matching `skills/<skill>/SKILL.md` workflow.

| Skill | Trigger | What it does |
|-------|---------|--------------|
| **loci-preflight** | Auto in `/plan` mode | Audits the plan at binary level before code is written — timing, energy, and CFG impact. |
| **loci-post-edit** | Auto after edits | Diffs pre/post compiled artifacts — regression verdict on  timing, energy, and control-flow. |
| **exec-trace** | User-invoked | Function-level timing and energy from real-time hardware traces, powered by LCLM. |
| **stack-depth** | User-invoked | Worst-case stack depth via call-graph traversal, per-function frame sizes |
| **memory-report** | User-invoked | ROM/RAM section breakdown and top consumers from compiled ELF binaries. No runtime instrumentation. No code modifications. |
| **control-flow** | User-invoked | Annotated control-flow graphs optimized for LLM analysis |
| **trends** | User-invoked | Per-function measurement history and optimization progress on the current branch. |

## Hooks

| Hook | Trigger | Action |
|------|---------|--------|
| `SessionStart` | startup | project detection, venv setup, context injection |
| `PreToolUse` | Edit, Write, MultiEdit | call-graph safety check, `.o` snapshot for delta analysis |

## LOCI MCP

Powered by LCLM (Large Code Language Model — trained on billions of ASM blocks and real hardware traces from IoT, networking, and safety-critical systems) — the only execution-aware model for code. Not a GPT wrapper.

Connects to `https://mcp.auroralabs.com/mcp/v1` for Binary Execution Grounding powered by LCLM — real-time execution data, no instrumentation required.
Plug LOCI into your CI/CD pipeline at any stage — code, build, test, or merge.

## Troubleshooting

### MCP connection failed

LOCI's binary analysis — regressions, power, latency, and bugs — requires the MCP server to be authorized.

1. Run `/mcp` in Claude Code and approve the **loci** server.
2. If the loci server doesn't appear in the list, restart Claude Code — the plugin registers it on startup.
3. If it still fails, check your network can reach `https://app.auroralabs.com`.

Skills that work without MCP: `stack-depth`, `memory-report`, `control-flow`  
Skills that require MCP: `exec-trace`, `loci-preflight`, `loci-post-edit`

### LOCI was not called / skills didn't trigger

**Auto-skills didn't fire:**

- `loci-preflight` only runs in `/plan` mode. Make sure you're planning new logic, not just asking a question.
- `loci-post-edit` Validation only runs after edits to C/C++/Rust source files.
- Both auto-skills require compiled binaries (`.elf`, `.o`, `.axf`) to be present. If your project hasn't been built yet, compile it first.

**On-demand skills didn't respond:**

- Type `/help` to confirm LOCI is loaded and see the full skill list.
- Verify the build environment was detected at session start — restart Claude Code from inside your project directory if needed.
- Check that a cross-compiler is installed and on your PATH:
  - ARM Cortex-M: `arm-none-eabi-gcc`
  - ARM Cortex-A: `aarch64-linux-gnu-gcc`
  - TriCore: `tricore-elf-gcc`

**Nothing seems to work:**

Run `/bug-report` to generate a full diagnostic report.

---

## Further Reading

- [LOCI Portal](PORTAL.md) — sessions, binary analysis results, quality gate verdicts, PR review, and account plans
- [setup/setup.sh](setup/setup.sh) — full setup script with platform-specific install logic
- [LICENSE](LICENSE) — Aurora Labs Proprietary License
