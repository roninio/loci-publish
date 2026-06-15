"""Unit tests for the trends ride-along helpers in loci_stats.

Covers `_trends_collect` (cursor + payload assembly) and the telemetry-flag
cache helpers `_trends_load_telemetry` / `_trends_save_telemetry`. The
per-(project, branch, function) filter logic is gone — the server now
gates on a single per-user telemetry_enabled boolean (see the
loci-claude-backend pull request that collapsed the toggle tree).
"""

import importlib
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def loci_stats(monkeypatch, tmp_path):
    """Re-import loci_stats with HOME redirected to a tmp dir so the module
    constants don't point at the developer's real ~/.loci."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "lib"))
    sys.modules.pop("loci_stats", None)
    return importlib.import_module("loci_stats")


# ── _trends_collect ────────────────────────────────────────────────────────


def _ctx(cwd_hash="p1", branch_slug="main", project_root="/u/proj"):
    return {
        "cwd_hash": cwd_hash,
        "branch_slug": branch_slug,
        "project_root": project_root,
        "git_branch": branch_slug,
    }


def test_collect_returns_none_when_no_records_after_cursor(loci_stats):
    records = [{"fn": "a", "ts": "2026-01-01T00:00:00Z", "skill": "s"}]
    stats = {"trends_cursor_high": "2026-12-31T00:00:00Z"}
    payload, cursor, _runs_cursor = loci_stats._trends_collect(records, stats, _ctx())
    assert payload is None
    assert cursor == "2026-12-31T00:00:00Z"


def test_collect_returns_payload_for_fresh_records(loci_stats):
    records = [
        {"fn": "x", "ts": "2026-04-27T08:00:00Z", "skill": "post-edit",
         "worst_ns": 100.0, "energy_uws": 0.5},
        {"fn": "y", "ts": "2026-04-27T08:01:00Z", "skill": "post-edit"},
    ]
    stats = {"trends_cursor_high": ""}
    payload, cursor, _runs_cursor = loci_stats._trends_collect(records, stats, _ctx())
    assert payload is not None
    assert payload["records"] == records
    assert cursor == "2026-04-27T08:01:00Z"


def test_collect_uses_basename_only_for_display_name(loci_stats):
    """display_name must never leak the absolute path / username."""
    records = [{"fn": "x", "ts": "2026-04-27T08:00:00Z", "skill": "post-edit"}]
    stats = {"trends_cursor_high": ""}
    payload, _, _ = loci_stats._trends_collect(
        records, stats, _ctx(project_root="/Users/alice/secret-proj"),
    )
    assert payload is not None
    assert payload["project"]["cwd_hash"] == "p1"
    assert payload["project"]["display_name"] == "secret-proj"
    assert payload["branch"]["slug"] == "main"


def test_collect_caps_at_500_records(loci_stats):
    records = [
        {"fn": "x", "ts": f"2026-04-27T08:{m:02d}:00Z", "skill": "s"}
        for m in range(60)
    ] * 10  # 600 total
    stats = {"trends_cursor_high": ""}
    payload, _, _ = loci_stats._trends_collect(records, stats, _ctx())
    assert payload is not None
    assert len(payload["records"]) == 500


def test_collect_skips_records_at_or_before_cursor(loci_stats):
    """Strictly-greater comparison: a record exactly at the cursor is
    considered already shipped and must not be re-sent."""
    records = [
        {"fn": "a", "ts": "2026-04-27T08:00:00Z", "skill": "s"},
        {"fn": "b", "ts": "2026-04-27T08:00:01Z", "skill": "s"},
    ]
    stats = {"trends_cursor_high": "2026-04-27T08:00:00Z"}
    payload, cursor, _runs_cursor = loci_stats._trends_collect(records, stats, _ctx())
    assert payload is not None
    assert [r["fn"] for r in payload["records"]] == ["b"]
    assert cursor == "2026-04-27T08:00:01Z"


# ── runs ride-along (verdict telemetry) ────────────────────────────────────


def test_collect_attaches_runs_when_fresh(loci_stats):
    """A run row newer than runs_cursor_high should ride the trends payload."""
    records = [{"fn": "x", "ts": "2026-04-27T08:00:00Z", "skill": "post-edit"}]
    runs = [
        {"ts": "2026-04-27T08:00:30Z", "skill": "post-edit",
         "verdict": "Verdict: OK — debug log added intentionally."},
    ]
    stats = {"trends_cursor_high": "", "runs_cursor_high": ""}
    payload, _rec_cursor, runs_cursor = loci_stats._trends_collect(
        records, stats, _ctx(), runs=runs,
    )
    assert payload is not None
    assert payload["runs"] == runs
    assert runs_cursor == "2026-04-27T08:00:30Z"


def test_collect_returns_payload_for_runs_only(loci_stats):
    """No measurement records but fresh runs → still build a payload."""
    runs = [
        {"ts": "2026-04-27T08:00:30Z", "skill": "preflight",
         "verdict": "Execution fit: GOOD — proceed with plan"},
    ]
    stats = {"trends_cursor_high": "", "runs_cursor_high": ""}
    payload, _rec_cursor, runs_cursor = loci_stats._trends_collect(
        [], stats, _ctx(), runs=runs,
    )
    assert payload is not None
    assert payload["records"] == []
    assert payload["runs"] == runs
    assert runs_cursor == "2026-04-27T08:00:30Z"


def test_collect_skips_runs_at_or_before_cursor(loci_stats):
    """Same strictly-greater comparison as records — exactly-at the cursor
    is already shipped."""
    runs = [
        {"ts": "2026-04-27T08:00:00Z", "skill": "preflight", "verdict": "old"},
        {"ts": "2026-04-27T08:00:01Z", "skill": "preflight", "verdict": "new"},
    ]
    records = [{"fn": "x", "ts": "2026-04-27T08:00:00Z", "skill": "preflight"}]
    stats = {"trends_cursor_high": "", "runs_cursor_high": "2026-04-27T08:00:00Z"}
    payload, _rec_cursor, runs_cursor = loci_stats._trends_collect(
        records, stats, _ctx(), runs=runs,
    )
    assert payload is not None
    assert [r["verdict"] for r in payload["runs"]] == ["new"]
    assert runs_cursor == "2026-04-27T08:00:01Z"


def test_collect_no_runs_omits_runs_field(loci_stats):
    records = [{"fn": "x", "ts": "2026-04-27T08:00:00Z", "skill": "preflight"}]
    stats = {"trends_cursor_high": "", "runs_cursor_high": ""}
    payload, _rec_cursor, _runs_cursor = loci_stats._trends_collect(
        records, stats, _ctx(),
    )
    assert payload is not None
    assert "runs" not in payload


# ── telemetry-flag cache (_trends_load_telemetry / _trends_save_telemetry) ─


def test_load_telemetry_missing_file_returns_none(loci_stats):
    # tmp HOME has no cache file → opt-out default applies.
    assert loci_stats._trends_load_telemetry() == {"telemetry_enabled": None}


def test_load_telemetry_corrupt_json_returns_none(loci_stats):
    cache = loci_stats.TRENDS_TELEMETRY_CACHE
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("not-json{{", encoding="utf-8")
    assert loci_stats._trends_load_telemetry() == {"telemetry_enabled": None}


def test_load_telemetry_round_trip_true(loci_stats):
    loci_stats._trends_save_telemetry(True)
    assert loci_stats._trends_load_telemetry() == {"telemetry_enabled": True}


def test_load_telemetry_round_trip_false(loci_stats):
    loci_stats._trends_save_telemetry(False)
    assert loci_stats._trends_load_telemetry() == {"telemetry_enabled": False}


def test_load_telemetry_non_bool_normalised_to_none(loci_stats):
    """A malformed cache from a future schema or a hand-edit shouldn't
    crash the plugin — fall back to opt-out default."""
    cache = loci_stats.TRENDS_TELEMETRY_CACHE
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"telemetry_enabled": "yes"}', encoding="utf-8")
    assert loci_stats._trends_load_telemetry()["telemetry_enabled"] is None


def test_save_telemetry_overwrites_atomically(loci_stats):
    """Two writes back-to-back must leave the cache with the latest value
    (atomic temp+rename, no torn write)."""
    loci_stats._trends_save_telemetry(True)
    loci_stats._trends_save_telemetry(False)
    assert loci_stats._trends_load_telemetry() == {"telemetry_enabled": False}
