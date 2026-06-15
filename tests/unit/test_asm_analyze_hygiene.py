"""Tests for asm_analyze.py stdout/logging hygiene.

Regression: a user reported `json.load` failing at position 0 on the
output file of `asm_analyze.py extract-assembly`. Root cause was third-
party libraries printing to stdout during the analysis, prepending bytes
to what should be a pure JSON document. main() now routes analysis
through a _capture_stdout context and only emits the JSON document after
restoring the real stream.
"""

import io
import sys

import pytest

from asm_analyze import _capture_stdout

pytestmark = pytest.mark.unit


class TestCaptureStdout:
    def test_diverts_prints(self, capsys):
        """Writes inside the context land in the returned buffer, not on
        the real stdout."""
        with _capture_stdout() as buf:
            print("this should not reach the real stdout")
            sys.stdout.write("direct write\n")
        real = capsys.readouterr().out
        assert real == ""
        assert "this should not reach the real stdout" in buf.getvalue()
        assert "direct write" in buf.getvalue()

    def test_restores_stdout_after_context(self, capsys):
        real_before = sys.stdout
        with _capture_stdout():
            pass
        assert sys.stdout is real_before
        print("after-context write")
        assert "after-context write" in capsys.readouterr().out

    def test_restores_stdout_on_exception(self, capsys):
        real_before = sys.stdout
        with pytest.raises(RuntimeError):
            with _capture_stdout():
                raise RuntimeError("boom")
        assert sys.stdout is real_before
        print("still reachable")
        assert "still reachable" in capsys.readouterr().out

    def test_nested_capture_still_captures(self):
        """Nested use doesn't blow up; inner context captures relative to
        the caller's current stdout."""
        with _capture_stdout() as outer:
            print("outer visible")
            with _capture_stdout() as inner:
                print("inner only")
            # After inner context exits, stdout returns to the outer buffer
            print("back to outer")
        assert "inner only" in inner.getvalue()
        assert "outer visible" in outer.getvalue()
        assert "back to outer" in outer.getvalue()
        assert "inner only" not in outer.getvalue()
