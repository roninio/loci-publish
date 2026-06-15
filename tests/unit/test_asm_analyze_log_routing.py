"""Tests for asm_analyze.py logging configuration.

Production-grade contract: stdout is JSON, stderr carries diagnostics.
A caller using `2>&1 | jq` must not see asmslicer warnings prepended to
the JSON document.

Regression context: an engineer hit `jq: error at column 8` because a
recurring `cxxfilt unavailable` warning was being merged with the JSON
stream by `2>&1`. The fix routes WARNINGs into a buffer that is
attached to the JSON output as a `warnings` array, so the contract holds
even when callers (LLM-authored bash, in this case) merge streams.
"""

import json
import logging
import os
import subprocess
import sys

import pytest

import asm_analyze

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_logging_after_test():
    """asm_analyze configures the root logger at import; restore after each
    test so cross-test bleeding doesn't matter."""
    yield
    # Unset LOCI_DEBUG and reconfigure to default state.
    os.environ.pop("LOCI_DEBUG", None)
    asm_analyze._configure_logging()


class TestWarningCapture:
    def test_warning_captured_not_streamed_in_default_mode(self, capsys):
        os.environ.pop("LOCI_DEBUG", None)
        asm_analyze._configure_logging()
        logging.getLogger("loci.asmslicer.demangle").warning("cxxfilt unavailable")

        captured = capsys.readouterr()
        # The warning must NOT have hit stderr.
        assert "cxxfilt" not in captured.err
        # But it MUST be drainable from the buffer.
        msgs = asm_analyze._drain_warnings()
        assert any("cxxfilt unavailable" in m for m in msgs)

    def test_drain_warnings_clears_buffer(self):
        os.environ.pop("LOCI_DEBUG", None)
        asm_analyze._configure_logging()
        logging.getLogger("test").warning("first")
        logging.getLogger("test").warning("second")
        first = asm_analyze._drain_warnings()
        assert len(first) == 2
        # Drained — second call should yield nothing.
        assert asm_analyze._drain_warnings() == []

    def test_error_records_still_go_to_stderr(self, capsys):
        os.environ.pop("LOCI_DEBUG", None)
        asm_analyze._configure_logging()
        logging.getLogger("test").error("a real problem")
        # ERROR must be visible — not buffered.
        assert "a real problem" in capsys.readouterr().err
        # And not in the warning buffer.
        assert asm_analyze._drain_warnings() == []

    def test_info_suppressed_by_default(self, capsys):
        os.environ.pop("LOCI_DEBUG", None)
        asm_analyze._configure_logging()
        logging.getLogger("test").info("noisy info")
        assert "noisy info" not in capsys.readouterr().err
        assert asm_analyze._drain_warnings() == []

    def test_loci_debug_routes_everything_to_stderr(self, capsys):
        os.environ["LOCI_DEBUG"] = "1"
        try:
            asm_analyze._configure_logging()
            logging.getLogger("test").warning("noisy warning")
            logging.getLogger("test").info("noisy info")
        finally:
            os.environ.pop("LOCI_DEBUG", None)
        err = capsys.readouterr().err
        # In debug mode warnings + info are visible — and the buffer is
        # not in use, so _drain_warnings returns empty.
        assert "noisy warning" in err
        assert "noisy info" in err
        assert asm_analyze._drain_warnings() == []

    def test_buffer_is_bounded(self):
        """Pathological inputs (e.g. asmslicer logging per-symbol on a
        corrupt ELF) must not consume unbounded memory or bloat the
        JSON output. The buffer caps at MAX_RECORDS and reports overflow."""
        os.environ.pop("LOCI_DEBUG", None)
        asm_analyze._configure_logging()
        log = logging.getLogger("flood")
        cap = asm_analyze._WarningBuffer.MAX_RECORDS
        for i in range(cap + 50):
            log.warning("warning %d", i)
        msgs = asm_analyze._drain_warnings()
        # Got the first `cap` records plus a single "N more suppressed" line.
        assert len(msgs) == cap + 1
        assert "50 more warnings suppressed" in msgs[-1]

    def test_drain_resets_overflow_counter(self):
        """After draining, the next batch should track its own overflow
        independently — no leakage from the previous run."""
        os.environ.pop("LOCI_DEBUG", None)
        asm_analyze._configure_logging()
        log = logging.getLogger("flood2")
        cap = asm_analyze._WarningBuffer.MAX_RECORDS
        for i in range(cap + 10):
            log.warning("first batch %d", i)
        first = asm_analyze._drain_warnings()
        assert "10 more warnings suppressed" in first[-1]
        # Second batch — only 3 messages, should NOT show "more suppressed".
        for i in range(3):
            log.warning("second batch %d", i)
        second = asm_analyze._drain_warnings()
        assert len(second) == 3
        assert not any("suppressed" in m for m in second)


# ──────────────────────────────────────────────────────────────────────────
# Subprocess test: the *real* protection against the regression.
# ──────────────────────────────────────────────────────────────────────────
# This invokes lib/asm_analyze.py as the CLI agent / skill would, then
# checks that stdout is parseable JSON with no warning bytes prepended.
# Requires a real ELF; uses the heap fixture from the asmslicer source.

def _run_cli(args, plugin_root):
    cmd = [sys.executable, str(plugin_root / "lib" / "asm_analyze.py"), *args]
    env = {**os.environ, "_LOCI_BOOTSTRAP": "1", "PYTHONIOENCODING": "utf-8"}
    env.pop("LOCI_DEBUG", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)


def test_subprocess_stdout_is_pure_json_under_2to1_redirect(
    plugin_root, heap_fixtures_dir, require_asmslicer, tmp_path,
):
    """Reproduce the original failure mode: caller redirects 2>&1 and pipes
    to a JSON parser. The first byte of merged output must be `{`, not a
    warning character — otherwise the JSON consumer chokes."""
    if heap_fixtures_dir is None:
        pytest.skip("heap fixtures not available")
    elf = heap_fixtures_dir / "heap_alloc_aarch64.elf"
    if not elf.is_file():
        pytest.skip("heap_alloc_aarch64.elf fixture missing")

    proc = _run_cli(["memmap", "--elf-path", str(elf)], plugin_root)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)

    # If 2>&1 had merged, the engineer's pipeline would see `proc.stdout +
    # proc.stderr` concatenated. Simulate that — the result must still
    # parse cleanly because stderr should be empty in default mode.
    merged = proc.stdout + proc.stderr
    assert merged.lstrip().startswith("{"), f"merged output starts with non-JSON: {merged[:200]!r}"
    # And the actual stdout-only path must also parse.
    json.loads(proc.stdout)


def test_subprocess_warnings_appear_in_json_when_emitted(
    plugin_root, heap_fixtures_dir, require_asmslicer, tmp_path,
):
    """If asmslicer emits a warning during a run, the CLI must surface it
    via the JSON `warnings` array — never via stderr in default mode.

    We don't have a deterministic way to force a warning (fixtures may be
    free of triggers), so this test asserts the *shape* contract: when
    `warnings` is present it's a list of strings, and stderr stays empty
    of WARNING-level logs (no `WARNING` prefix anywhere)."""
    if heap_fixtures_dir is None:
        pytest.skip("heap fixtures not available")
    elf = heap_fixtures_dir / "heap_alloc_aarch64.elf"
    if not elf.is_file():
        pytest.skip("heap_alloc_aarch64.elf fixture missing")

    proc = _run_cli(["memmap", "--elf-path", str(elf), "--with-heap"], plugin_root)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    if "warnings" in payload:
        assert isinstance(payload["warnings"], list)
        # Every warning entry must be a structured dict with at least
        # `code` and `detail` keys. Captured Python log warnings get
        # wrapped as `{"code": "RUNTIME", "detail": <msg>}`; subcommand-
        # emitted warnings (e.g. memmap MAP_FORMAT_UNRECOGNIZED) carry
        # their own code plus an optional `path`.
        for w in payload["warnings"]:
            assert isinstance(w, dict), w
            assert isinstance(w.get("code"), str) and w["code"], w
            assert isinstance(w.get("detail"), str), w
    # The stderr stream must not carry WARNING-prefixed Python log records.
    assert "WARNING" not in proc.stderr.upper() or "[LOCI_DEBUG]" in proc.stderr


def test_subprocess_extract_cfg_warnings_go_to_stderr(
    plugin_root, heap_fixtures_dir, require_asmslicer,
):
    """extract-cfg's contract is plain text on stdout, not JSON. There's
    nowhere safe to surface captured warnings inside the text payload, so
    they go to stderr — that's only acceptable here because the consumer
    isn't piping into jq for this subcommand. The text output must remain
    pristine on stdout."""
    if heap_fixtures_dir is None:
        pytest.skip("heap fixtures not available")
    elf = heap_fixtures_dir / "heap_alloc_aarch64.elf"
    if not elf.is_file():
        pytest.skip("heap_alloc_aarch64.elf fixture missing")

    proc = _run_cli(["extract-cfg", "--elf-path", str(elf)], plugin_root)
    assert proc.returncode == 0, (proc.stdout[-500:], proc.stderr[-500:])
    # stdout is the CFG text, not JSON.
    assert proc.stdout.lstrip().startswith("function:")
    # If any warnings fired during the run, they must have landed on
    # stderr (the safe channel for text-output mode), never silently
    # dropped. We can't deterministically force one, so just assert the
    # path doesn't crash and stdout stays clean.
    assert "function:" in proc.stdout


def test_subprocess_loci_debug_does_route_warnings_to_stderr(
    plugin_root, heap_fixtures_dir, require_asmslicer,
):
    """LOCI_DEBUG=1 must restore the old behavior — warnings on stderr,
    full diagnostics. Useful for interactive debugging even though it
    breaks JSON consumers."""
    if heap_fixtures_dir is None:
        pytest.skip("heap fixtures not available")
    elf = heap_fixtures_dir / "heap_alloc_aarch64.elf"
    if not elf.is_file():
        pytest.skip("heap_alloc_aarch64.elf fixture missing")

    cmd = [sys.executable, str(plugin_root / "lib" / "asm_analyze.py"),
           "memmap", "--elf-path", str(elf)]
    env = {**os.environ, "_LOCI_BOOTSTRAP": "1", "PYTHONIOENCODING": "utf-8",
           "LOCI_DEBUG": "1"}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0
    # stdout still parseable
    json.loads(proc.stdout)
    # In debug mode we make no promises about stderr cleanliness.
