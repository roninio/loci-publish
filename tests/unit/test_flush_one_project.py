"""Unit tests for `_flush_one_project` cursor-divergence handling.

Regression coverage for the bug where trends/runs cursors could not catch
up once they fell behind `impact_cursor_high`. The early-exit at the top
of `_flush_one_project` skipped every flush attempt when impact was at
the head, so trends rode along on no batch and never advanced.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def loci_stats(monkeypatch, tmp_path):
    """Re-import loci_stats with HOME and LOCI_STATE_DIR redirected to a
    tmp dir so the module's STATE_DIR / token-file constants don't touch
    the developer's real ~/.loci."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "lib"))
    sys.modules.pop("loci_stats", None)
    return importlib.import_module("loci_stats")


def _write_state(loci_stats, *, records, stats, ctx, runs=None):
    """Write the four state files _flush_one_project consumes and return
    their paths (meas, stats, ctx). The runs file is keyed by cwd_hash +
    branch_slug and located inside STATE_DIR by the function itself."""
    state = loci_stats.STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    cwd_hash = ctx["cwd_hash"]
    slug = ctx["branch_slug"]

    meas = state / f"loci-measurements-{cwd_hash}-{slug}.jsonl"
    meas.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records),
        encoding="utf-8",
    )

    stats_path = state / f"loci-stats-{cwd_hash}-{slug}.json"
    stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")

    ctx_path = state / f"project-context-{cwd_hash}.json"
    ctx_path.write_text(json.dumps(ctx, indent=2), encoding="utf-8")

    runs_path = state / f"loci-skill-runs-{cwd_hash}-{slug}.jsonl"
    if runs:
        runs_path.write_text(
            "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in runs),
            encoding="utf-8",
        )

    # Telemetry cache ON so trends collection runs.
    loci_stats._trends_save_telemetry(True)

    return meas, stats_path, ctx_path


def _ctx(cwd_hash="abc123", branch_slug="main", project_root="/u/proj"):
    return {
        "cwd_hash": cwd_hash,
        "branch_slug": branch_slug,
        "project_root": project_root,
        "git_branch": branch_slug,
    }


# ── Regression: trends cursor must catch up when impact is at the head ────

def test_flush_ships_trends_when_impact_cursor_already_current(
    loci_stats, monkeypatch,
):
    """impact_cursor_high == latest record ts, trends_cursor_high lags.

    Before the fix: early-exit returned None, no POST, trends_cursor stays
    stuck forever and dashboard never sees the data.

    After the fix: a synthetic trends-only batch ships with zero impact
    aggregate, both trends_cursor_high and runs_cursor_high advance.
    """
    records = [
        {"ts": "2026-05-14T13:40:00+00:00", "fn": "f1", "skill": "exec-trace",
         "worst_ns": 100.0, "energy_uws": 0.05},
        {"ts": "2026-05-14T13:40:30+00:00", "fn": "f2", "skill": "exec-trace",
         "worst_ns": 200.0, "energy_uws": 0.10},
    ]
    runs = [
        {"ts": "2026-05-14T13:40:00+00:00", "skill": "exec-trace",
         "verdict": "Verdict: OK"},
    ]
    stats = {
        "functions": 2, "mcp_calls": 0, "skills_invoked": 1, "co_reasoning": 0,
        "branch": "main", "first_recorded": "2026-05-14T13:40:00+00:00",
        "last_recorded": "2026-05-14T13:40:30+00:00",
        "impact_cursor_high": "2026-05-14T13:40:30+00:00",  # at head
        "trends_cursor_high": "2026-05-05T00:00:00+00:00",  # behind
        "runs_cursor_high":   "2026-05-05T00:00:00+00:00",  # behind
    }
    meas, stats_path, ctx_path = _write_state(
        loci_stats, records=records, stats=stats, ctx=_ctx(), runs=runs,
    )

    posted = []

    def fake_post(payload, token):
        posted.append(payload)
        return 200, {"ok": True, "trends": {"accepted": len(payload.get("trends", {}).get("records", []))}}

    monkeypatch.setattr(loci_stats, "_post_impact", fake_post)

    result = loci_stats._flush_one_project(meas, stats_path, ctx_path, "tok")

    # A POST was made and it carried the trends payload.
    assert len(posted) == 1, "expected exactly one trends-only POST"
    assert "trends" in posted[0], "POST must carry the trends sub-payload"
    assert posted[0]["functionsAnalyzed"] == 0, (
        "trends-only batch must ship a zero-valued impact aggregate; "
        "the server's daily upsert is additive"
    )
    assert len(posted[0]["trends"]["records"]) == 2

    # Cursors advanced.
    saved = json.loads(stats_path.read_text(encoding="utf-8"))
    assert saved["trends_cursor_high"] == "2026-05-14T13:40:30+00:00"
    assert saved["runs_cursor_high"]   == "2026-05-14T13:40:00+00:00"
    # Impact cursor unchanged (it was already at the head).
    assert saved["impact_cursor_high"] == "2026-05-14T13:40:30+00:00"

    assert result is None  # no token rotation in this test


def test_flush_no_op_when_both_cursors_current(loci_stats, monkeypatch):
    """Both cursors at the head, no fresh runs → no POST, no state write."""
    records = [
        {"ts": "2026-05-14T13:40:30+00:00", "fn": "f1", "skill": "exec-trace"},
    ]
    stats = {
        "impact_cursor_high": "2026-05-14T13:40:30+00:00",
        "trends_cursor_high": "2026-05-14T13:40:30+00:00",
        "runs_cursor_high":   "2026-05-14T13:40:30+00:00",
    }
    meas, stats_path, ctx_path = _write_state(
        loci_stats, records=records, stats=stats, ctx=_ctx(),
    )

    posted = []
    monkeypatch.setattr(
        loci_stats, "_post_impact",
        lambda p, t: posted.append(p) or (200, {"ok": True}),
    )

    result = loci_stats._flush_one_project(meas, stats_path, ctx_path, "tok")

    assert posted == [], "no POST should be made when nothing is pending"
    assert result is None


def test_flush_normal_path_attaches_trends_to_first_impact_batch(
    loci_stats, monkeypatch,
):
    """Impact has unshipped records → trends rides along on the first
    impact batch (existing behavior, must still work after the fix).

    Verifies the fallback synthetic batch does NOT fire when trends was
    already carried by an impact batch.
    """
    records = [
        {"ts": "2026-05-14T13:00:00+00:00", "fn": "f1", "skill": "exec-trace",
         "worst_ns": 100.0},
        {"ts": "2026-05-14T13:00:30+00:00", "fn": "f2", "skill": "post-edit",
         "worst_ns": 200.0},
    ]
    stats = {
        "impact_cursor_high": "2026-05-13T00:00:00+00:00",  # behind
        "trends_cursor_high": "2026-05-13T00:00:00+00:00",  # behind
        "runs_cursor_high":   "2026-05-13T00:00:00+00:00",
    }
    meas, stats_path, ctx_path = _write_state(
        loci_stats, records=records, stats=stats, ctx=_ctx(),
    )

    posted = []
    monkeypatch.setattr(
        loci_stats, "_post_impact",
        lambda p, t: posted.append(p) or (200, {"ok": True}),
    )

    loci_stats._flush_one_project(meas, stats_path, ctx_path, "tok")

    # Two impact batches (one per skill), no extra synthetic batch.
    assert len(posted) == 2
    trends_carriers = [p for p in posted if "trends" in p]
    assert len(trends_carriers) == 1, (
        "trends must ride exactly one impact batch — no double-shipping"
    )
    # Carrier batch reports a real skill (not the trends-only sentinel).
    assert trends_carriers[0]["skillsUsed"], (
        "trends carrier must be a real impact batch with a skillsUsed entry"
    )

    saved = json.loads(stats_path.read_text(encoding="utf-8"))
    assert saved["impact_cursor_high"] == "2026-05-14T13:00:30+00:00"
    assert saved["trends_cursor_high"] == "2026-05-14T13:00:30+00:00"


def test_flush_trends_only_fallback_handles_401(loci_stats, monkeypatch):
    """401 on the synthetic trends-only batch must unlink the local token,
    just like a real impact batch — next SessionStart re-mints."""
    records = [
        {"ts": "2026-05-14T13:40:30+00:00", "fn": "f1", "skill": "exec-trace"},
    ]
    stats = {
        "impact_cursor_high": "2026-05-14T13:40:30+00:00",  # at head
        "trends_cursor_high": "2026-05-05T00:00:00+00:00",  # behind
        "runs_cursor_high":   "2026-05-05T00:00:00+00:00",
    }
    meas, stats_path, ctx_path = _write_state(
        loci_stats, records=records, stats=stats, ctx=_ctx(),
    )

    # Pre-create the token file so we can assert it was unlinked.
    loci_stats.IMPACT_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    loci_stats.IMPACT_TOKEN_FILE.write_text(
        '{"token":"stale","issued_at":"x"}', encoding="utf-8",
    )

    monkeypatch.setattr(
        loci_stats, "_post_impact", lambda p, t: (401, {"error": "bad token"}),
    )

    loci_stats._flush_one_project(meas, stats_path, ctx_path, "stale")

    assert not loci_stats.IMPACT_TOKEN_FILE.exists(), (
        "401 on the trends-only fallback must unlink the local token"
    )

    saved = json.loads(stats_path.read_text(encoding="utf-8"))
    # Cursors unchanged on auth failure.
    assert saved["trends_cursor_high"] == "2026-05-05T00:00:00+00:00"
    assert saved["runs_cursor_high"]   == "2026-05-05T00:00:00+00:00"
