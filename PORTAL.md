# LOCI Portal

LOCI's quality gate agent models regressions, power, latency, and bugs from the binary. From plan to merge.

Without running code. No instrumentation. No code changes.

The LOCI Portal is your control centre for monitoring binary analysis results, configuring quality gates, and reviewing PR decisions — whether you are an individual developer or part of a team. All results are grounded in real execution data from your codebase.

Access the portal at [app.auroralabs.com](https://app.auroralabs.com).

---

## Contents

- [LOCI Portal](#loci-portal)
  - [Contents](#contents)
  - [Getting Started](#getting-started)
  - [Sessions](#sessions)
  - [Binary Analysis Results](#binary-analysis-results)
    - [What LOCI measures](#what-loci-measures)
    - [Reading a result](#reading-a-result)
  - [Quality Gate](#quality-gate)
    - [Gate verdicts](#gate-verdicts)
    - [Human-on-the-Loop](#human-on-the-loop)
  - [PR Review](#pr-review)
  - [Team Management](#team-management)
  - [Usage \& Quota](#usage--quota)
  - [Account Plans](#account-plans)
  - [Support](#support)

---

## Getting Started

1. Go to [app.auroralabs.com](https://app.auroralabs.com) and create your account.
2. Install the LOCI plugin in Claude Code — see [README](README.md) for setup instructions.
3. Run your first analysis from Claude Code. Results appear in the portal automatically.

No additional configuration is required to start seeing data. Every Claude Code and Cursor session that runs LOCI analysis is logged, analysed, and visualised in the portal in real time.

---

## Sessions

The Sessions view gives you a unified log of every LOCI analysis run — your own sessions on a Personal plan, or your entire team's activity on a Company User plan.

Each session entry shows:

| Field | Description |
|-------|-------------|
| **Developer** | Who triggered the analysis |
| **Timestamp** | When the session ran |
| **Skill** | Preflight Auditing, Postflight Validation, exec-trace, stack-depth, memory-report |
| **Verdict** | GOOD / ADJUST PLAN / STOP (preflight) or OK / CAUTION / FLAG (post-edit) |
| **Functions analysed** | Number of functions covered in the session |
| **Binary delta** | Timing and energy change vs. previous build (where applicable) |

Use the Sessions view to track what your AI coding agent is doing and confirm LOCI is catching regressions before they reach review. 

On Company User plans, sessions from all team members are visible in one place.

---

## Binary Analysis Results

LOCI grounds every result in real execution data — no simulation, no instrumentation, no code changes.

### What LOCI measures

| Metric | Description |
|--------|-------------|
| **Timing** | Worst-case and typical execution time per function |
| **Energy** | Power consumption per call, relevant for battery-constrained devices |
| **Stack depth** | Worst-case stack usage via call-graph traversal |
| **ROM / RAM** | Binary section breakdown — flash consumption and static RAM usage |
| **Regressions** | Delta between current and previous build across all metrics |
| **Bugs** | Control-flow anomalies, stack overflows, binary-level CFI violations |

### Reading a result

Each result is anchored to a specific commit and function. The portal shows the current measurement alongside the previous baseline so regressions are immediately visible. Results are flagged automatically when they exceed the thresholds you define in the Quality Gate.

---

## Quality Gate

The Quality Gate is where you define what matters. LOCI enforces it.

Configure thresholds per metric:

| Threshold | Example |
|-----------|---------|
| Timing regression | Flag if worst-case execution time increases by more than 10% |
| Energy regression | Flag if energy per call increases by more than 5% |
| Stack budget | Block if stack usage exceeds 80% of task budget |
| ROM growth | Warn if binary size grows by more than 2 KB per PR |

### Gate verdicts

LOCI returns one of three verdicts for each analysis:

- **GOOD** — all metrics within threshold. Safe to proceed.
- **CAUTION** — one or more metrics approaching threshold. Review recommended.
- **FLAG** — threshold exceeded. LOCI proposes a fix and blocks the gate.

### Human-on-the-Loop

The gate is advisory by default — a human always makes the final decision. When LOCI flags a result, it appears in your PR review queue for approval or override. You can calibrate gate sensitivity over time as LOCI learns your quality standards. 

On Company User plans, shared thresholds apply across the whole team.

---

## PR Review

The PR Review view surfaces LOCI's Postflight Validation results directly against your pull requests.

For each open PR, LOCI shows:

- Binary diff between the PR branch and base
- Regression summary across timing, energy, stack, and ROM
- Gate verdict with the specific functions and metrics that triggered it
- LOCI's proposed fix when a FLAG is raised

Approve or block the PR directly from the portal. All decisions are logged with the reviewer, timestamp, and gate verdict for audit purposes.

> LOCI integrates into your CI/CD pipeline at any stage — code, build, test, or merge. See your CI/CD setup guide for pipeline-level gate configuration.

---

## Team Management

Available on Company User plans.

| Action | Description |
|--------|-------------|
| **Invite developers** | Add team members by email |
| **Assign seats** | Manage per-seat licensing across your organisation |
| **View team activity** | See sessions, verdicts, and quota usage per developer |
| **Set shared gate config** | Apply quality thresholds across the whole team |

Team-wide gate configuration ensures consistent quality standards regardless of which developer or AI agent is writing the code.

---

## Usage & Quota

Monitor your LOCI usage against your plan limits.

| Metric | Description |
|--------|-------------|
| **Daily interactions** | Number of LOCI analysis calls today vs. your plan limit |
| **Historical usage** | Trend over the past 30 days |
| **Per-developer breakdown** | Company User plans only — usage per team member |

Quota resets daily. 

If you hit your limit, on-demand skills (`/exec-trace`, `/stack-depth`, `/memory-report`, `/control-flow`) will be unavailable until reset. 

Auto-running skills (`loci-preflight`, `loci-post-edit`) pause automatically and resume the following day.

---

## Account Plans

| | Free | Premium | Company User |
|--|------|---------|---------------|
| **Price** | $0 / month | $59 / seat / month | $59 / seat / month |
| **Who it's for** | Individual developers getting started | Individual developers who need full access | Teams with multi-developer licensing |
| **Daily interactions** | 30 | Unlimited | Unlimited |
| **Binary analysis** | Yes | Yes | Yes |
| **Priority signal processing** | No | Yes | Yes |
| **Advanced PR review grounding** | No | Yes | Yes |
| **Full usage analytics & history** | No | Yes | Yes |
| **CI/CD pipeline integration signals** | No | Yes | Yes |
| **Per-developer usage breakdown** | No | No | Yes |
| **Shared quality gate configuration** | No | No | Yes |
| **Team seat management** | No | No | Yes |
| **Dedicated email support** | No | Yes | Yes |

Upgrade at [app.auroralabs.com](https://app.auroralabs.com).

---

## Support

- Email: [loci@auroralabs.com](mailto:loci@auroralabs.com)
- Plugin issues: [github.com/auroralabs-loci/loci-claude/issues](https://github.com/auroralabs-loci/loci-claude/issues)
- Troubleshooting the plugin: see [README — Troubleshooting](README.md#troubleshooting)
