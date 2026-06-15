#!/usr/bin/env python3
"""LOCI cumulative per-branch stats tracker."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 for all Python I/O before touching stdout/stderr. The env var
# also propagates to any child Python process this script spawns without
# needing an explicit env= override.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower().replace("-", "") != "utf8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HOME_LOCI_DIR = Path.home() / ".loci"

# Shared file-only logger (no-op unless LOCI_LOG_LEVEL is set).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import loci_log  # noqa: E402
IMPACT_TOKEN_FILE = HOME_LOCI_DIR / "impact-token.json"
IMPACT_ERRORS_LOG = HOME_LOCI_DIR / "impact-errors.log"


def _resolve_state_dir() -> Path:
    """Resolve the directory for persistent state.

    Precedence:
      1. LOCI_STATE_DIR env var (set by session-init.sh after verifying it's
         writable) — authoritative for the current session.
      2. ~/.loci/state — default unversioned user-scoped location.
      3. <plugin>/state — fall back if home isn't writable; this means the
         user loses state across plugin upgrades (old behaviour), but the
         plugin still works.

    State that predates this change may still live under <plugin>/state of
    an older install. We don't migrate automatically — state regenerates
    naturally from the next session onward in the new location.
    """
    env = os.environ.get("LOCI_STATE_DIR")
    if env:
        return Path(env)
    home_state = HOME_LOCI_DIR / "state"
    try:
        home_state.mkdir(parents=True, exist_ok=True)
        return home_state
    except OSError:
        return PLUGIN_DIR / "state"


STATE_DIR = _resolve_state_dir()
IMPACT_ENDPOINT = os.environ.get(
    "LOCI_IMPACT_ENDPOINT",
    "https://loci.auroralabs.com/impact/v1",
)

# Telemetry ride-along cache. A single global boolean per user — the server
# is source of truth and rides the current value back on every /impact/v1
# response. Cache miss / corrupt JSON is NOT an error: opt-out default
# means we treat absence as enabled until the first response populates it.
#
# (Replaces the older per-(project,branch,function) toggle-tree cache; the
# server collapsed to a single user-level switch in pull request #18.)
#
# Lives under STATE_DIR (not HOME_LOCI_DIR directly) so it honours the same
# LOCI_STATE_DIR override that stats/measurements use — keeps the cache
# isolated in tests and migrates cleanly if state ever relocates.
TRENDS_TELEMETRY_CACHE = STATE_DIR / "trends-telemetry.json"


def _ctx_path(cli_arg: str | None = None) -> Path | None:
    """Resolve the per-session project-context JSON.

    Precedence:
      1. explicit --context-file CLI flag (preferred — passed by skills
         from the "project context:" line injected by session-init.sh)
      2. LOCI_CONTEXT_FILE env var (ad-hoc invocation)
      3. hash(PWD) keyed file — matches bash $(pwd) logical-path semantics
      4. deprecated unkeyed symlink — races between concurrent sessions,
         removed next release
    """
    if cli_arg:
        p = Path(cli_arg)
        if p.exists():
            return p
    env = os.environ.get("LOCI_CONTEXT_FILE")
    if env:
        p = Path(env)
        if p.exists():
            return p
    pwd = os.environ.get("PWD") or os.getcwd()
    h = hashlib.sha256(pwd.encode()).hexdigest()[:12]
    keyed = STATE_DIR / f"project-context-{h}.json"
    if keyed.exists():
        return keyed
    legacy = STATE_DIR / "project-context.json"
    return legacy if legacy.exists() else None


def _stats_path(context_file: str | None = None) -> Path | None:
    """Resolve stats file from the session's project-context JSON."""
    ctx_file = _ctx_path(context_file)
    if not ctx_file:
        return None
    with open(ctx_file, encoding="utf-8") as f:
        ctx = json.load(f)
    cwd_hash = ctx.get("cwd_hash", "default")
    slug = ctx.get("branch_slug", "unknown")
    return STATE_DIR / f"loci-stats-{cwd_hash}-{slug}.json"


def _load(path: Path) -> dict:
    if path and path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {
        "functions": 0,
        "mcp_calls": 0,
        "skills_invoked": 0,
        "co_reasoning": 0,
        "branch": "unknown",
        "first_recorded": datetime.now(timezone.utc).isoformat(),
        "last_recorded": None,
    }


def _global_stats_path() -> Path:
    """Global stats file — all projects, all branches, since inception."""
    return STATE_DIR / "loci-stats-global.json"


def _load_global() -> dict:
    path = _global_stats_path()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {
        "functions": 0,
        "mcp_calls": 0,
        "skills_invoked": 0,
        "co_reasoning": 0,
        "projects_seen": [],
        "first_recorded": datetime.now(timezone.utc).isoformat(),
        "last_recorded": None,
    }


def _update_global(args):
    """Silently accumulate into global stats — never shown to users."""
    data = _load_global()
    data["functions"] += args.functions
    data["mcp_calls"] += args.mcp_calls
    data["skills_invoked"] += 1
    data["co_reasoning"] += args.co_reasoning
    data["last_recorded"] = datetime.now(timezone.utc).isoformat()
    ctx_file = _ctx_path(getattr(args, "context_file", None))
    if ctx_file:
        with open(ctx_file, encoding="utf-8") as f:
            ctx = json.load(f)
        project = ctx.get("project_root", "unknown")
        if project not in data["projects_seen"]:
            data["projects_seen"].append(project)
    with open(_global_stats_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def cmd_record(args):
    path = _stats_path(getattr(args, "context_file", None))
    if not path:
        return
    data = _load(path)
    data["functions"] += args.functions
    data["mcp_calls"] += args.mcp_calls
    data["skills_invoked"] += 1
    data["co_reasoning"] += args.co_reasoning
    data["last_recorded"] = datetime.now(timezone.utc).isoformat()
    ctx_file = _ctx_path(getattr(args, "context_file", None))
    if ctx_file:
        with open(ctx_file, encoding="utf-8") as f:
            ctx = json.load(f)
        data["branch"] = ctx.get("git_branch", "unknown")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    _update_global(args)

    # Verdict ride-along: when the skill passes --verdict, append a line to
    # the per-project skill-runs JSONL. Read by the trends ride-along during
    # the Stop-hook flush. No extra CLI invocation — piggybacks on the record
    # call the skill already runs.
    verdict = (getattr(args, "verdict", None) or "").strip()
    if verdict and args.skill in (
        "preflight", "post-edit", "exec-trace",
        "stack-depth", "control-flow", "memory-report",
    ):
        runs_path = _skill_runs_path(getattr(args, "context_file", None))
        if runs_path:
            run = {
                "ts": data["last_recorded"],
                "skill": args.skill,
                "verdict": verdict,
                "commit": _resolve_commit(args),
            }
            # Optional structured gates snapshot. Parsed loosely here; the
            # backend's normalizeRuns enforces the allow-list (gate names +
            # statuses). Bad input is silently dropped so a malformed --gates
            # never blocks verdict capture.
            gates_raw = getattr(args, "gates", None)
            if gates_raw:
                try:
                    parsed = json.loads(gates_raw)
                    if isinstance(parsed, dict) and parsed:
                        run["gates"] = parsed
                except (ValueError, TypeError):
                    pass
            try:
                runs_path.parent.mkdir(parents=True, exist_ok=True)
                with open(runs_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(run, separators=(",", ":")) + "\n")
                _rotate_if_needed(runs_path)
            except OSError:
                pass  # Verdict capture must never break stats recording.


def cmd_summary(args):
    path = _stats_path(getattr(args, "context_file", None))
    if not path:
        return
    data = _load(path)
    if data["skills_invoked"] == 0:
        return
    branch = data.get("branch", "unknown")
    parts = []
    if data["functions"] > 0:
        parts.append(f"{data['functions']} functions")
    if data["mcp_calls"] > 0:
        parts.append(f"{data['mcp_calls']} API calls")
    parts.append(f"{data['skills_invoked']} skills")
    suffix = f" on {branch}" if branch != "unknown" else ""
    print(f"    ↳ *{' · '.join(parts)}{suffix}*")


def cmd_global_summary(args):
    data = _load_global()
    if data["skills_invoked"] == 0:
        return
    parts = []
    if data["functions"] > 0:
        parts.append(f"{data['functions']} functions")
    if data["mcp_calls"] > 0:
        parts.append(f"{data['mcp_calls']} API calls")
    parts.append(f"{data['skills_invoked']} skills")
    n_projects = len(data.get("projects_seen", []))
    if n_projects > 0:
        parts.append(f"{n_projects} project{'s' if n_projects != 1 else ''}")
    first = data.get("first_recorded", "")
    since = first[:10] if first else ""
    suffix = f" since {since}" if since else ""
    print(f"    ↳ *{' · '.join(parts)}{suffix}*")


# ---------------------------------------------------------------------------
# Measurement history (JSONL)
# ---------------------------------------------------------------------------

MAX_MEASUREMENTS = 500
ROTATE_KEEP = 250


def _measurements_path(context_file: str | None = None) -> Path | None:
    """Resolve JSONL measurement file for current project+branch."""
    ctx_file = _ctx_path(context_file)
    if not ctx_file:
        return None
    with open(ctx_file, encoding="utf-8") as f:
        ctx = json.load(f)
    cwd_hash = ctx.get("cwd_hash", "default")
    slug = ctx.get("branch_slug", "unknown")
    return STATE_DIR / f"loci-measurements-{cwd_hash}-{slug}.jsonl"


def _skill_runs_path(context_file: str | None = None) -> Path | None:
    """Resolve JSONL skill-runs file for current project+branch.

    One row per /loci-preflight or /loci-post-edit invocation, capturing
    the verdict line verbatim (e.g. "Verdict: OK — debug log added
    intentionally; +203 ns overhead is expected ..."). Read by the trends
    ride-along during the Stop-hook flush.
    """
    ctx_file = _ctx_path(context_file)
    if not ctx_file:
        return None
    with open(ctx_file, encoding="utf-8") as f:
        ctx = json.load(f)
    cwd_hash = ctx.get("cwd_hash", "default")
    slug = ctx.get("branch_slug", "unknown")
    return STATE_DIR / f"loci-skill-runs-{cwd_hash}-{slug}.jsonl"


def _rotate_if_needed(path: Path) -> None:
    """Keep the file under MAX_MEASUREMENTS lines by dropping the oldest."""
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= MAX_MEASUREMENTS:
        return
    path.write_text(
        "\n".join(lines[-ROTATE_KEEP:]) + "\n", encoding="utf-8"
    )


def _read_jsonl(path: Path | None) -> list[dict]:
    """Read all JSONL rows from a file. Tolerates missing file and
    malformed lines (skipped, not raised). Used for both the per-function
    measurements file and the per-skill-run verdict file — same on-disk
    shape, different filename patterns.
    """
    if not path or not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# Back-compat alias — `_read_measurements` was the historical name and is
# referenced widely below. The function is generic; the alias stays.
_read_measurements = _read_jsonl


def _resolve_commit(args) -> str:
    """Resolve commit hash for a record write.

    If --commit was passed explicitly, honour it. Otherwise run
    `git rev-parse --short HEAD` from the project root recorded in the
    session context file. On any failure (no git, not a repo, broken
    .git, missing context file) return the literal "none" so the field
    is always present in the JSONL.
    """
    explicit = getattr(args, "commit", None)
    if explicit:
        return explicit
    cwd = None
    ctx_path = getattr(args, "context_file", None)
    if ctx_path:
        try:
            ctx = json.loads(Path(ctx_path).read_text(encoding="utf-8"))
            cwd = ctx.get("project_root") or str(Path(ctx_path).parent)
        except (OSError, json.JSONDecodeError):
            cwd = str(Path(ctx_path).parent)
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=2,
        ).strip()
        return out or "none"
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired, OSError):
        return "none"


def _build_record(fn: str, skill: str, commit: str | None,
                   source: str | None, **values) -> dict:
    """Build a single measurement record dict."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "fn": fn,
        "skill": skill,
    }
    for key, val in values.items():
        if val is not None:
            record[key] = val
    if commit:
        record["commit"] = commit
    if source:
        record["src"] = source
    return record


def cmd_record_measurement(args):
    path = _measurements_path(getattr(args, "context_file", None))
    if not path:
        return

    commit = _resolve_commit(args)

    if args.stdin:
        # Batch mode: read JSONL from stdin, merge with CLI shared fields
        ts = datetime.now(timezone.utc).isoformat()
        records = []
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            record = {"ts": ts, "fn": row.get("fn", "unknown"), "skill": args.skill}
            for key in ("worst_ns", "happy_ns", "energy_uws", "stack_b", "rom_b",
                        "heap_sites", "heap_static_b", "src"):
                if key in row:
                    record[key] = row[key]
            record["commit"] = commit
            records.append(record)
        if records:
            with open(path, "a", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")
            _rotate_if_needed(path)
        return

    # Single-record mode (backwards compatible)
    record = _build_record(
        fn=args.function, skill=args.skill,
        commit=commit, source=args.source,
        worst_ns=args.worst_ns, happy_ns=args.happy_ns,
        energy_uws=args.energy_uws, stack_b=args.stack_bytes,
        rom_b=args.rom_bytes,
        heap_sites=args.heap_sites, heap_static_b=args.heap_static_b,
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
    _rotate_if_needed(path)


def _direction(values: list[float]) -> str:
    """Classify trend direction from a list of chronological values.

    All LOCI metrics are lower-is-better (timing, stack, ROM), so
    latest > first means regression, latest < first means improvement.
    """
    if len(values) < 2:
        return "baseline"
    first, last, peak = values[0], values[-1], max(values)
    if abs(last - first) / max(abs(first), 1e-9) < 0.03:
        return "stable"
    if last < first:
        return "improved"
    # last > first — regressed from baseline
    if last < peak and (peak - last) / max(abs(peak), 1e-9) > 0.05:
        return "recovering"
    return "regressed"


def _format_value(val: float, unit: str) -> str:
    """Format a measurement value with its unit."""
    if unit == "ns":
        return f"{val:.0f} ns" if val >= 1 else f"{val:.2f} ns"
    if unit == "uWs":
        return f"{val:.2f} uWs"
    if unit == "B":
        return f"{int(val)} B"
    return f"{val}"


_METRIC_DEFS = [
    ("worst_ns", "ns", "worst-path"),
    ("stack_b", "B", "stack"),
    ("rom_b", "B", "rom"),
]


def _detect_metrics(records: list[dict]) -> list[tuple[str, str, str]]:
    """Return all (key, unit, label) tuples present in a group of records."""
    found = []
    for key, unit, label in _METRIC_DEFS:
        if any(key in r for r in records):
            found.append((key, unit, label))
    return found


def cmd_trend(args):
    path = _measurements_path(getattr(args, "context_file", None))
    records = _read_measurements(path)
    if not records:
        return

    if args.function:
        # Single function — chronological list
        fn_records = [r for r in records if r.get("fn") == args.function]
        if not fn_records:
            return
        for r in fn_records:
            ts = r.get("ts", "")[:10]
            commit = r.get("commit", "")
            parts = []
            if "worst_ns" in r:
                parts.append(f"worst={_format_value(r['worst_ns'], 'ns')}")
            if "energy_uws" in r:
                parts.append(f"energy={_format_value(r['energy_uws'], 'uWs')}")
            if "stack_b" in r:
                parts.append(f"stack={_format_value(r['stack_b'], 'B')}")
            if "rom_b" in r:
                parts.append(f"rom={_format_value(r['rom_b'], 'B')}")
            commit_str = f"  ({commit})" if commit else ""
            print(f"  {ts}  {', '.join(parts)}{commit_str}")
        return

    # All functions — summary table (one row per function per metric type)
    groups: dict[str, list[dict]] = {}
    for r in records:
        fn = r.get("fn", "unknown")
        groups.setdefault(fn, []).append(r)

    rows = []
    for fn, fn_records in groups.items():
        for metric_key, unit, metric_label in _detect_metrics(fn_records):
            values = [r[metric_key] for r in fn_records if metric_key in r]
            if not values:
                continue
            edits = len(values)
            first_val = values[0]
            latest_val = values[-1]
            direction = _direction(values)
            if direction == "baseline":
                net = "--"
            else:
                peak = max(values)
                if peak > latest_val and peak != first_val:
                    pct = ((latest_val - peak) / abs(peak)) * 100
                    net = f"{pct:+.0f}% from peak"
                else:
                    pct = ((latest_val - first_val) / max(abs(first_val), 1e-9)) * 100
                    net = f"{pct:+.0f}%"
            # Only add suffix when the metric isn't the default (timing)
            label = fn if metric_label == "worst-path" else f"{fn} ({metric_label})"
            rows.append((label, edits, _format_value(first_val, unit),
                          _format_value(latest_val, unit), direction, net))

    if not rows:
        return

    # Compute column widths
    headers = ("Function", "Edits", "First", "Latest", "Direction", "Net")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))

    total = sum(r[1] for r in rows)
    print(f"\nBranch summary: {len(rows)} functions tracked, {total} measurements")


def _trend_line_for(fn: str, records: list[dict]) -> str | None:
    """Return a single trend line string for a function, or None."""
    fn_records = [r for r in records if r.get("fn") == fn]
    if len(fn_records) < 2:
        return None
    metrics = _detect_metrics(fn_records)
    if not metrics:
        return None
    metric_key, unit, metric_label = metrics[0]
    values = [r[metric_key] for r in fn_records if metric_key in r]
    if len(values) < 2:
        return None

    arrow_parts = [_format_value(v, unit).split()[0] for v in values[-5:]]
    trail = " -> ".join(arrow_parts) + f" {unit}"

    peak = max(values)
    latest = values[-1]
    if peak > latest and peak != values[0]:
        pct = ((latest - peak) / abs(peak)) * 100
        note = f"{pct:+.0f}% from peak"
    else:
        pct = ((latest - values[0]) / max(abs(values[0]), 1e-9)) * 100
        note = f"{pct:+.0f}%"

    return f"{fn} {metric_label}: {trail} ({len(values)} edits, {note})"


def cmd_trend_line(args):
    path = _measurements_path(getattr(args, "context_file", None))
    records = _read_measurements(path)
    if not records:
        return
    # Accept comma-separated functions or a single function
    functions = [f.strip() for f in args.function.split(",") if f.strip()]
    for fn in functions:
        line = _trend_line_for(fn, records)
        if line:
            print(line)


def _compute_impact(records: list[dict], scope: set[str], skill: str | None,
                    co_reasoning: int) -> dict:
    """Core scoring math shared by export-impact (CLI) and flush-impacts (hook)."""
    by_fn: dict[str, list[dict]] = {}
    for r in records:
        by_fn.setdefault(r.get("fn", "unknown"), []).append(r)

    if not scope:
        scope = set(by_fn.keys())

    counts = {"improved": 0, "regressed": 0, "stable": 0, "recovering": 0, "baseline": 0}
    total_energy_saved = 0.0
    total_stack_saved = 0
    improvement_pcts = []

    for fn in scope:
        fn_records = by_fn.get(fn, [])
        if not fn_records:
            counts["baseline"] += 1
            continue

        worst_vals = [r["worst_ns"] for r in fn_records if "worst_ns" in r]
        direction = _direction(worst_vals) if worst_vals else "baseline"
        counts[direction] = counts.get(direction, 0) + 1

        if direction == "improved" and len(worst_vals) >= 2:
            pct = (worst_vals[0] - worst_vals[-1]) / max(abs(worst_vals[0]), 1e-9) * 100
            improvement_pcts.append(pct)

        energy_vals = [r["energy_uws"] for r in fn_records if "energy_uws" in r]
        if len(energy_vals) >= 2 and energy_vals[-1] < energy_vals[0]:
            total_energy_saved += energy_vals[0] - energy_vals[-1]

        stack_vals = [r["stack_b"] for r in fn_records if "stack_b" in r]
        if len(stack_vals) >= 2 and stack_vals[-1] < stack_vals[0]:
            total_stack_saved += int(stack_vals[0] - stack_vals[-1])

    skills_used = {skill: 1} if skill else {}

    return {
        "functionsAnalyzed": len(scope),
        "functionsImproved": counts["improved"],
        "functionsRegressed": counts["regressed"],
        "functionsStable": counts["stable"],
        "functionsRecovering": counts["recovering"],
        "functionsBaseline": counts["baseline"],
        "improvementPctSum": round(sum(improvement_pcts), 2) if improvement_pcts else 0,
        "improvedCount": len(improvement_pcts),
        "totalEnergySavedUws": round(total_energy_saved, 2),
        "totalStackSavedB": total_stack_saved,
        "regressionsCaught": counts["recovering"] + counts["regressed"],
        "coReasoningSessions": co_reasoning,
        "skillsUsed": skills_used,
    }


def cmd_export_impact(args):
    """Export session-scoped impact metrics as JSON to stdout (CLI debugging)."""
    path = _measurements_path(getattr(args, "context_file", None))
    records = _read_measurements(path)
    if not records:
        print(json.dumps({"functionsAnalyzed": 0}))
        return
    scope = {f.strip() for f in args.functions.split(",") if f.strip()} if args.functions else set()
    co_reasoning = getattr(args, "co_reasoning", 0) or 0
    print(json.dumps(_compute_impact(records, scope, args.skill, co_reasoning),
                     separators=(",", ":")))


def _load_token() -> str | None:
    try:
        data = json.loads(IMPACT_TOKEN_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data.get("token") if isinstance(data, dict) else None


def _write_token(token: str) -> None:
    """Atomically rewrite the token file (tmp + rename)."""
    HOME_LOCI_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"token": token, "issued_at": datetime.now(timezone.utc).isoformat()},
        separators=(",", ":"),
    )
    fd, tmp = tempfile.mkstemp(dir=str(HOME_LOCI_DIR), prefix=".impact-token.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, IMPACT_TOKEN_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _log_error(msg: str) -> None:
    try:
        HOME_LOCI_DIR.mkdir(parents=True, exist_ok=True)
        if IMPACT_ERRORS_LOG.exists() and IMPACT_ERRORS_LOG.stat().st_size > 100_000:
            IMPACT_ERRORS_LOG.write_text("", encoding="utf-8")
        ts = datetime.now(timezone.utc).isoformat()
        with open(IMPACT_ERRORS_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────────
# Trends ride-along helpers.
#
# These piggy-back on the existing /impact/v1 POST: cmd_flush_impacts reads
# the cached telemetry flag, attaches a `trends` sub-payload to the first
# skill batch when the flag is ON, and writes the flag back from the
# response so the server stays the source of truth.
#
# Every helper here is wrapped at the call site so any failure is logged and
# falls through — the impact aggregate must keep shipping even if trends I/O
# blows up.
# ────────────────────────────────────────────────────────────────────────


def _trends_load_telemetry() -> dict:
    """Read the telemetry cache. Returns {"telemetry_enabled": bool|None}.

    Cache miss / unreadable / malformed → {"telemetry_enabled": None}.
    Callers treat None as "enabled" (opt-out default) so a fresh install
    ships everything on the first session; the response then primes the
    cache for next time.
    """
    try:
        data = json.loads(TRENDS_TELEMETRY_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"telemetry_enabled": None}
    if not isinstance(data, dict):
        return {"telemetry_enabled": None}
    enabled = data.get("telemetry_enabled")
    if not isinstance(enabled, bool):
        enabled = None
    return {"telemetry_enabled": enabled}


def _trends_save_telemetry(enabled) -> None:
    """Atomic temp+rename rewrite of the telemetry cache."""
    TRENDS_TELEMETRY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"telemetry_enabled": bool(enabled)},
        separators=(",", ":"),
    )
    fd, tmp = tempfile.mkstemp(
        dir=str(TRENDS_TELEMETRY_CACHE.parent),
        prefix=".trends-telemetry.",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, TRENDS_TELEMETRY_CACHE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _trends_collect(records: list[dict], stats: dict, ctx: dict,
                    runs: list[dict] | None = None,
                    ) -> tuple[dict | None, str | None, str | None]:
    """Build the trends sub-payload from JSONL rows.

    Returns (payload, new_records_cursor, new_runs_cursor). Payload is None
    only when both records AND runs are empty since their cursors. Each
    cursor advances independently — the two watermarks don't clobber each
    other and don't clobber impact's `impact_cursor_high`.

    Per-entity filtering is gone: the user's single global telemetry switch
    decides whether the trends payload gets built at all (caller's job),
    and the server applies the same gate as a safety net at ingest.
    """
    rec_cursor = stats.get("trends_cursor_high", "") or ""
    fresh = [r for r in records
             if isinstance(r, dict) and r.get("ts", "") > rec_cursor]

    runs = runs or []
    runs_cursor = stats.get("runs_cursor_high", "") or ""
    fresh_runs = [r for r in runs
                  if isinstance(r, dict) and r.get("ts", "") > runs_cursor]

    if not fresh and not fresh_runs:
        return None, rec_cursor, runs_cursor

    # Cap records at 500 per POST (matches the existing impact-side cap).
    new_rec_cursor = max((r.get("ts", "") for r in fresh), default=rec_cursor)
    if len(fresh) > 500:
        fresh = fresh[:500]
        new_rec_cursor = max((r.get("ts", "") for r in fresh), default=rec_cursor)

    # Cap runs at 200 per POST. Verdict rows are far lower volume than
    # per-function measurements — one row per skill invocation, not per fn.
    new_runs_cursor = max((r.get("ts", "") for r in fresh_runs), default=runs_cursor)
    if len(fresh_runs) > 200:
        fresh_runs = fresh_runs[:200]
        new_runs_cursor = max((r.get("ts", "") for r in fresh_runs), default=runs_cursor)

    project_root = ctx.get("project_root") or ""
    display_name = os.path.basename(project_root) if project_root else None

    payload = {
        "project": {
            "cwd_hash": ctx.get("cwd_hash") or "",
            "display_name": display_name,
        },
        "branch": {
            "slug": ctx.get("branch_slug") or "",
            "git_branch": ctx.get("git_branch") or "",
        },
        "records": fresh,
    }
    if fresh_runs:
        payload["runs"] = fresh_runs
    return payload, new_rec_cursor, new_runs_cursor


def _post_impact(payload: dict, token: str) -> tuple[int, dict]:
    """POST a single skill-scoped payload to /impact/v1. Returns (status, body)."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        IMPACT_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    loci_log.info("loci-stats", f"start: POST {IMPACT_ENDPOINT}")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
            loci_log.info("loci-stats", f"end: POST {IMPACT_ENDPOINT} (status={resp.status})")
            return resp.status, body
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except (json.JSONDecodeError, ValueError):
            body = {}
        loci_log.warn("loci-stats", f"end: POST {IMPACT_ENDPOINT} (status={e.code})")
        return e.code, body
    except Exception as e:
        loci_log.error("loci-stats", f"end: POST {IMPACT_ENDPOINT} ({type(e).__name__}: {e})")
        _log_error(f"POST failed: {type(e).__name__}: {e}")
        return 0, {}


# Filename pattern for keyed measurements: loci-measurements-<cwd_hash>-<branch_slug>.jsonl
# The branch slug is whatever session-init.sh produced — usually [a-zA-Z0-9_.-], but
# we accept anything except `/` so legacy slugs keep working.
_MEASUREMENTS_FILENAME_RE = re.compile(
    r"^loci-measurements-([0-9a-f]+)-([^/]+)\.jsonl$"
)


def cmd_flush_impacts(args):
    """Silent Stop-hook ingress: ship unshipped measurements to /impact/v1.

    Filesystem-driven sweep — every Stop hook invocation flushes EVERY
    project whose state lives in STATE_DIR, not just the project whose
    session triggered the hook. This sidesteps the single-symlink race
    that made parallel-project sessions clobber each other's context.

    Always exits 0. Per-project failures (network, malformed state, etc.)
    are logged and skipped; one bad project never blocks the rest. Token
    handling and cursor-on-2xx semantics are unchanged — they just run
    once per project.
    """
    token = _load_token()
    if not token:
        return  # No credential yet; SessionStart will mint one.

    if not STATE_DIR.exists():
        return

    new_token_holder: dict[str, str] = {}

    for meas_path in sorted(STATE_DIR.glob("loci-measurements-*.jsonl")):
        m = _MEASUREMENTS_FILENAME_RE.match(meas_path.name)
        if not m:
            continue
        cwd_hash, slug = m.group(1), m.group(2)
        stats_path = STATE_DIR / f"loci-stats-{cwd_hash}-{slug}.json"
        ctx_path = STATE_DIR / f"project-context-{cwd_hash}.json"
        if not (stats_path.exists() and ctx_path.exists()):
            continue
        try:
            # Use the latest token rotated by any prior project's flush in
            # this same sweep — silent rolling rotation still works, and
            # every project pays at most one rotation per sweep.
            current_token = new_token_holder.get("token", token)
            rotated = _flush_one_project(
                meas_path, stats_path, ctx_path, current_token,
            )
            if rotated:
                new_token_holder["token"] = rotated
        except Exception as e:
            _log_error(
                f"flush {cwd_hash}-{slug} crashed: {type(e).__name__}: {e}"
            )

    final_token = new_token_holder.get("token")
    if final_token and final_token != token:
        try:
            _write_token(final_token)
        except Exception as e:
            _log_error(f"token rotate failed: {type(e).__name__}: {e}")


def _flush_one_project(meas_path: Path, stats_path: Path,
                       ctx_path: Path, token: str) -> str | None:
    """Ship unshipped measurements for a single (project, branch) pair.

    Returns a freshly-signed token from the server's rolling rotation
    when the server hands one back, or None otherwise. Token persistence
    is the caller's responsibility — this function never writes the
    token file directly.

    Per-project semantics match the previous singleton flush exactly:
      - cursor_high advances only on 2xx
      - 401 unlinks the local token and bails (next SessionStart re-mints)
      - co_reasoning_shipped advances only when its carrier batch is 2xx
      - trends ride-along still attaches to one skill batch per project.
        When impact_cursor_high is already at the head but trends/runs
        have fresh data, a synthetic zero-valued impact batch is shipped
        as the trends carrier so those cursors can advance independently.
    """
    stats = _load(stats_path)
    cursor_high = stats.get("impact_cursor_high", "")

    records = _read_measurements(meas_path)
    unshipped = [r for r in records if r.get("ts", "") > cursor_high]

    # Unshipped co-reasoning delta: ships piggy-backed on the first skill
    # batch below. Shipped watermark advances only if that batch succeeds,
    # so failures safely retry without double-counting.
    co_reasoning_total = int(stats.get("co_reasoning", 0))
    co_reasoning_shipped = int(stats.get("co_reasoning_shipped", 0))
    co_reasoning_pending = max(0, co_reasoning_total - co_reasoning_shipped)
    co_reasoning_carrier: str | None = None
    co_reasoning_newly_shipped = co_reasoning_shipped

    # ── Trends ride-along ───────────────────────────────────────────────
    # Read the cached telemetry flag and (when ON) gather trends data.
    # Both wrapped in try/except: any failure here MUST NOT break the
    # impact aggregate ship below. On error we fall through with neutral
    # defaults — the server-side filter remains the safety net.
    #
    # Collected BEFORE the empty-impact early-exit so trends/runs can ship
    # even when impact_cursor_high is already at the head. The two cursors
    # are independent (a prior flush can advance impact while a trends-
    # carrier batch failure leaves trends/runs behind); without this,
    # trends never catches up because there is no impact batch to ride.
    cached_telemetry: dict = {"telemetry_enabled": None}
    try:
        cached_telemetry = _trends_load_telemetry()
    except Exception as e:
        _log_error(f"trends cache read failed: {type(e).__name__}: {e}")

    # None (cache miss) is treated as enabled — opt-out default. False
    # short-circuits collection so we don't even build the payload.
    telemetry_on = cached_telemetry.get("telemetry_enabled") is not False

    trends_payload: dict | None = None
    new_trends_cursor: str | None = None
    new_runs_cursor: str | None = None
    if telemetry_on:
        try:
            with open(ctx_path, encoding="utf-8") as f:
                ctx = json.load(f)
            cwd_hash = ctx.get("cwd_hash") or ""
            slug = ctx.get("branch_slug") or ""
            runs_path = STATE_DIR / f"loci-skill-runs-{cwd_hash}-{slug}.jsonl"
            runs = _read_jsonl(runs_path)
            trends_payload, new_trends_cursor, new_runs_cursor = _trends_collect(
                records, stats, ctx, runs=runs,
            )
        except Exception as e:
            _log_error(f"trends collect failed: {type(e).__name__}: {e}")
            trends_payload, new_trends_cursor, new_runs_cursor = None, None, None

    # Nothing pending on either cursor — done.
    if not unshipped and trends_payload is None:
        return None

    trends_carrier: str | None = None
    trends_shipped_ok = False

    # Group unshipped rows by skill; ship one payload per skill.
    by_skill: dict[str, list[dict]] = {}
    for r in unshipped:
        by_skill.setdefault(r.get("skill", "unknown"), []).append(r)

    new_high = cursor_high
    new_token: str | None = None
    any_failure = False
    auth_failed = False

    for skill, rows in by_skill.items():
        scope = {r.get("fn", "unknown") for r in rows}
        cr_for_batch = 0
        if co_reasoning_pending > 0 and co_reasoning_carrier is None:
            cr_for_batch = co_reasoning_pending
            co_reasoning_carrier = skill
        payload = _compute_impact(records, scope, skill, cr_for_batch)
        # Always send the cached telemetry flag (cheap, lets server skip
        # riding back the value when it matches). Only attach trends to
        # ONE batch (the first), matching the co-reasoning piggy-back
        # pattern above.
        payload["telemetry_enabled"] = cached_telemetry.get("telemetry_enabled")
        if trends_payload is not None and trends_carrier is None:
            payload["trends"] = trends_payload
            trends_carrier = skill
        # Use rotated token from earlier batches in this same project.
        active_token = new_token if new_token else token
        status, body = _post_impact(payload, active_token)
        if status == 401:
            try:
                IMPACT_TOKEN_FILE.unlink()
            except FileNotFoundError:
                pass
            auth_failed = True
            break  # cursor held for this project; sweep continues with stale
                   # token (next project also gets 401, also bails — fine).
        if 200 <= status < 300:
            rotated = body.get("token") if isinstance(body, dict) else None
            if rotated:
                new_token = rotated
            batch_max = max((r.get("ts", "") for r in rows), default=cursor_high)
            if batch_max > new_high:
                new_high = batch_max
            if skill == co_reasoning_carrier:
                co_reasoning_newly_shipped = co_reasoning_total
            if skill == trends_carrier:
                trends_shipped_ok = True
            # Telemetry ride-along: any 2xx that returns telemetry_enabled
            # is authoritative — server is source of truth for the global
            # switch. Save to the local cache so the next session honours
            # the latest value before even building the payload.
            if (isinstance(body, dict)
                    and body.get("telemetry_enabled") is not None):
                try:
                    _trends_save_telemetry(body.get("telemetry_enabled"))
                except Exception as e:
                    _log_error(
                        f"trends cache write failed: {type(e).__name__}: {e}"
                    )
        else:
            any_failure = True
            _log_error(f"skill={skill} status={status} body={body}")
            if skill == co_reasoning_carrier:
                # Carrier failed: reset so the pending delta rides the
                # next successful batch instead of silently dropping.
                co_reasoning_carrier = None
            if skill == trends_carrier:
                trends_carrier = None  # will not advance the trends cursor

    # Trends-only fallback ship. Triggered when no impact batch carried
    # the trends payload — either because impact_cursor_high was already
    # current (no `unshipped` rows) or because the trends-carrier batch
    # failed and reset the carrier. Sends a zero-valued impact aggregate;
    # the server's daily upsert is additive (`field += 0`) and merges an
    # empty skills_json as a no-op, so this never double-counts.
    if (trends_payload is not None
            and trends_carrier is None
            and not auth_failed):
        payload = _compute_impact([], set(), None, 0)
        payload["telemetry_enabled"] = cached_telemetry.get("telemetry_enabled")
        payload["trends"] = trends_payload
        active_token = new_token if new_token else token
        status, body = _post_impact(payload, active_token)
        if status == 401:
            try:
                IMPACT_TOKEN_FILE.unlink()
            except FileNotFoundError:
                pass
            auth_failed = True
        elif 200 <= status < 300:
            rotated = body.get("token") if isinstance(body, dict) else None
            if rotated:
                new_token = rotated
            trends_shipped_ok = True
            if (isinstance(body, dict)
                    and body.get("telemetry_enabled") is not None):
                try:
                    _trends_save_telemetry(body.get("telemetry_enabled"))
                except Exception as e:
                    _log_error(
                        f"trends cache write failed: {type(e).__name__}: {e}"
                    )
        else:
            _log_error(f"trends-only batch status={status} body={body}")

    dirty = False
    if new_high != cursor_high and not any_failure and not auth_failed:
        stats["impact_cursor_high"] = new_high
        dirty = True
    if co_reasoning_newly_shipped != co_reasoning_shipped:
        stats["co_reasoning_shipped"] = co_reasoning_newly_shipped
        dirty = True
    if (trends_shipped_ok and new_trends_cursor
            and new_trends_cursor != stats.get("trends_cursor_high", "")):
        stats["trends_cursor_high"] = new_trends_cursor
        dirty = True
    # Verdict-runs cursor advances on the same 2xx as the trends carrier
    # (runs ride the same payload). Using a separate stat key so back-fills
    # don't clobber the records cursor.
    if (trends_shipped_ok and new_runs_cursor
            and new_runs_cursor != stats.get("runs_cursor_high", "")):
        stats["runs_cursor_high"] = new_runs_cursor
        dirty = True
    if dirty:
        try:
            with open(stats_path, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
        except Exception as e:
            _log_error(f"stats write failed: {type(e).__name__}: {e}")

    return new_token


def main():
    parser = argparse.ArgumentParser(description="LOCI cumulative stats tracker")
    sub = parser.add_subparsers(dest="cmd")

    # Shared flag: per-session project-context path (injected by session-init.sh
    # into Claude's additionalContext as "project context:"). Skills pass it to
    # avoid racing with the deprecated global symlink.
    ctx_parent = argparse.ArgumentParser(add_help=False)
    ctx_parent.add_argument("--context-file", default=None,
                            help="Path to per-session project-context JSON")

    rec = sub.add_parser("record", parents=[ctx_parent])
    rec.add_argument("--skill", required=True)
    rec.add_argument("--functions", type=int, default=0)
    rec.add_argument("--mcp-calls", "--api-calls", dest="mcp_calls", type=int, default=0)
    rec.add_argument("--co-reasoning", type=int, default=0)
    # Verdict ride-along: when present (preflight/post-edit only), append
    # to the per-project skill-runs JSONL alongside the stats write.
    rec.add_argument("--verdict", default=None,
                     help="Verbatim verdict line from the skill report")
    rec.add_argument("--gates", default=None,
                     help='JSON object of capability-gate statuses, e.g. '
                          '{"Safety":"warn","Performance":"pass"}. '
                          'Only gates that fired this run; allowed statuses: '
                          'pass | warn | fail.')
    rec.add_argument("--commit", default=None,
                     help="Optional commit SHA tagged on the verdict row")

    sub.add_parser("summary", parents=[ctx_parent])
    sub.add_parser("global-summary")

    rm = sub.add_parser("record-measurement", parents=[ctx_parent])
    rm.add_argument("--function", default=None)
    rm.add_argument("--skill", required=True, dest="skill")
    rm.add_argument("--stdin", action="store_true",
                    help="Read JSONL records from stdin (batch mode)")
    rm.add_argument("--worst-ns", type=float, default=None)
    rm.add_argument("--happy-ns", type=float, default=None)
    rm.add_argument("--energy-uws", type=float, default=None)
    rm.add_argument("--stack-bytes", type=int, default=None)
    rm.add_argument("--rom-bytes", type=int, default=None)
    rm.add_argument("--heap-sites", type=int, default=None,
                    help="Total alloc-site count for the function (memory-report --with-heap)")
    rm.add_argument("--heap-static-b", type=int, default=None,
                    help="Sum of statically-resolvable allocation sizes (bytes) for the function")
    rm.add_argument("--commit", default=None)
    rm.add_argument("--source", default=None)

    tr = sub.add_parser("trend", parents=[ctx_parent])
    tr.add_argument("--function", default=None)

    tl = sub.add_parser("trend-line", parents=[ctx_parent])
    tl.add_argument("--function", required=True)

    ei = sub.add_parser("export-impact", parents=[ctx_parent])
    ei.add_argument("--functions", default=None,
                    help="Comma-separated function names to scope")
    ei.add_argument("--skill", default=None,
                    help="Current skill name for skillsUsed")
    ei.add_argument("--co-reasoning", type=int, default=0,
                    help="Co-reasoning sessions from this skill run")

    sub.add_parser("flush-impacts",
                   help="Silent Stop-hook ingress for impact telemetry")

    args = parser.parse_args()
    # flush-impacts is wired as the Stop-hook command in hooks/hooks.json — log
    # an outer Stop-hook bracket so the inner cmd entry/exit sit between the
    # hook's own start/end lines, matching how SessionStart/PreToolUse are logged.
    if args.cmd == "flush-impacts":
        loci_log.info("loci-stats", "start: Stop hook")
    with loci_log.around("loci-stats", f"cmd={args.cmd or '?'}"):
        if args.cmd == "record":
            cmd_record(args)
        elif args.cmd == "summary":
            cmd_summary(args)
        elif args.cmd == "global-summary":
            cmd_global_summary(args)
        elif args.cmd == "record-measurement":
            cmd_record_measurement(args)
        elif args.cmd == "trend":
            cmd_trend(args)
        elif args.cmd == "trend-line":
            cmd_trend_line(args)
        elif args.cmd == "export-impact":
            cmd_export_impact(args)
        elif args.cmd == "flush-impacts":
            try:
                cmd_flush_impacts(args)
            except Exception as e:
                _log_error(f"flush-impacts crashed: {type(e).__name__}: {e}")
    if args.cmd == "flush-impacts":
        loci_log.info("loci-stats", "end: Stop hook")


if __name__ == "__main__":
    main()
