"""Windows cp1252 encoding compatibility tests for LOCI entry-point scripts.

Regression (v0.1.25): Windows cp1252 stdout cannot encode Unicode characters
(→ U+2192, ✗ U+2717, ⚠ U+26A0, ↳ U+21B3) used in LOCI output. Entry-point
scripts call sys.stdout.reconfigure(encoding='utf-8', errors='replace') at
startup so they survive Windows console sessions without UnicodeEncodeError.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _PLUGIN_ROOT / "hooks"


def _run_cp1252(
    script: Path,
    *,
    stdin: bytes | None = None,
    args: tuple = (),
) -> subprocess.CompletedProcess:
    """Run a hook script with PYTHONIOENCODING=cp1252 (simulates Windows console)."""
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "_LOCI_BOOTSTRAP": "1"}
    return subprocess.run(
        [sys.executable, str(script), *args],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Guard condition
# ---------------------------------------------------------------------------

class TestEncodingGuardCondition:
    """The guard expression encoding.lower().replace('-','') != 'utf8' is correct."""

    @pytest.mark.parametrize("encoding,needs_fix", [
        ("cp1252",  True),
        ("cp850",   True),
        ("latin-1", True),
        ("ascii",   True),
        ("utf-8",   False),
        ("utf8",    False),
        ("UTF-8",   False),
        ("UTF8",    False),
    ])
    def test_guard_identifies_encoding(self, encoding: str, needs_fix: bool):
        assert (encoding.lower().replace("-", "") != "utf8") == needs_fix

    @pytest.mark.parametrize("char,label", [
        ("→", "right arrow → (CFG edges in asm_analyze)"),
        ("✗", "ballot X ✗ (BLOCK finding in preflight_check)"),
        ("⚠", "warning sign ⚠ (RISK finding in preflight_check)"),
        ("↳", "downwards arrow ↳ (branch summary in loci_stats)"),
    ])
    def test_loci_output_chars_fail_on_cp1252(self, char: str, label: str):
        """Each char used in LOCI output genuinely fails on cp1252 without the fix."""
        with pytest.raises(UnicodeEncodeError):
            char.encode("cp1252")


# ---------------------------------------------------------------------------
# render_report Unicode content
# ---------------------------------------------------------------------------

class TestRenderReportUnicode:
    """render_report (preflight_check) produces ⚠ and ✗ that need the encoding guard."""

    def test_risk_finding_contains_warning_sign(self):
        from preflight_check import Finding, render_report
        report = render_report("f", [Finding("call_graph", "RISK", "recursion")])
        assert "⚠" in report  # ⚠

    def test_block_finding_contains_ballot_x(self):
        from preflight_check import Finding, render_report
        report = render_report("f", [Finding("call_graph", "BLOCK", "unbounded")])
        assert "✗" in report  # ✗


# ---------------------------------------------------------------------------
# Fix pattern validation
# ---------------------------------------------------------------------------

class TestEncodingFixPattern:
    """Validate the reconfiguration pattern used in all entry-point scripts."""

    def test_pattern_prevents_crash_on_cp1252(self, tmp_path: Path):
        """With the fix, all four LOCI Unicode chars can be printed on cp1252 stdout."""
        script = tmp_path / "enc_fix.py"
        script.write_text(
            textwrap.dedent("""\
                import sys
                if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
                    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                # U+2192 → U+2717 ✗ U+26A0 ⚠ U+21B3 ↳
                print("\\u2192 \\u2717 \\u26a0 \\u21b3")
            """),
            encoding="utf-8",
        )
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        r = subprocess.run([sys.executable, str(script)], capture_output=True, env=env)
        assert r.returncode == 0
        assert b"UnicodeEncodeError" not in r.stderr

    def test_pattern_absent_causes_crash_on_cp1252(self, tmp_path: Path):
        """Without the fix, Unicode output to cp1252 stdout raises UnicodeEncodeError."""
        script = tmp_path / "enc_no_fix.py"
        script.write_text(
            textwrap.dedent("""\
                import sys
                # Intentionally no reconfigure — confirms the failure mode
                print("\\u2192")
            """),
            encoding="utf-8",
        )
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        r = subprocess.run([sys.executable, str(script)], capture_output=True, env=env)
        assert r.returncode != 0
        assert b"UnicodeEncodeError" in r.stderr


# ---------------------------------------------------------------------------
# Subprocess tests: hook scripts must not crash with cp1252
# ---------------------------------------------------------------------------

class TestPreflightCheckCp1252:
    def test_risk_finding_no_crash(self):
        """preflight_check.py survives cp1252 stdout when ⚠ RISK is emitted."""
        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "test.c",
                "content": "void foo() { foo(); }",  # direct recursion → RISK
            },
        }).encode()
        r = _run_cp1252(_HOOKS_DIR / "preflight_check.py", stdin=payload)
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert b"UnicodeEncodeError" not in r.stderr

    def test_clean_source_no_crash(self):
        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "main.c",
                "content": "int main(void) { return 0; }",
            },
        }).encode()
        r = _run_cp1252(_HOOKS_DIR / "preflight_check.py", stdin=payload)
        assert r.returncode == 0


class TestPostEditReminderCp1252:
    def test_c_source_no_crash(self):
        """post_edit_reminder.py survives cp1252 stdout when a C source is modified."""
        payload = json.dumps({"tool_input": {"file_path": "src/main.c"}}).encode()
        r = _run_cp1252(_HOOKS_DIR / "post_edit_reminder.py", stdin=payload)
        assert r.returncode == 0
        assert b"UnicodeEncodeError" not in r.stderr

    def test_non_source_exits_cleanly(self):
        payload = json.dumps({"tool_input": {"file_path": "README.md"}}).encode()
        r = _run_cp1252(_HOOKS_DIR / "post_edit_reminder.py", stdin=payload)
        assert r.returncode == 0
