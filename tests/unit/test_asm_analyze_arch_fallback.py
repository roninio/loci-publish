"""Tests for run_analysis()'s architecture-fallback retry.

If asmslicer rejects a `provided_architecture` it should have known about
(e.g. an older or mis-built wheel that doesn't have `Architecture.ARM_CORTEXM`,
or a venv whose Python lacks `StrEnum` and so loses the str-equality
semantics asmslicer relies on), we retry once without `architecture=` so
the call falls back to ELF-driven auto-detection. This keeps `extract-symbols`
and friends working even when the engineer's environment is slightly off.
"""

import logging
import sys
import types
from pathlib import Path

import pytest

import asm_analyze

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_elf(tmp_path):
    """Need a real file path because run_analysis checks .is_file()."""
    p = tmp_path / "fake.elf"
    p.write_bytes(b"\x7fELF" + b"\x00" * 60)  # not a real ELF; we mock the wheel
    return p


def _patch_asmslicer(monkeypatch, process_impl):
    """Replace asmslicer.process and asmslicer module so run_analysis uses our stub."""
    fake_module = types.ModuleType("asmslicer")
    fake_module.process = process_impl
    package = types.ModuleType("loci.service.asmslicer")
    package.asmslicer = fake_module
    monkeypatch.setitem(sys.modules, "loci.service.asmslicer.asmslicer", fake_module)
    monkeypatch.setitem(sys.modules, "loci.service.asmslicer", package)


def test_no_retry_when_architecture_succeeds(monkeypatch, fake_elf):
    """Normal path: asmslicer accepts the architecture, no retry."""
    calls = []

    def fake_process(**kwargs):
        calls.append(dict(kwargs))
        # Simulate writing a small symmap output so run_analysis doesn't fail downstream.
        symmap = Path(kwargs.get("out_symmap", ""))
        if symmap and not symmap.exists():
            symmap.write_text("symbol,name,long_name,start_address,size,namespace\n")

    _patch_asmslicer(monkeypatch, fake_process)
    asm_analyze.run_analysis(str(fake_elf), architecture="cortexm")
    assert len(calls) == 1
    assert calls[0].get("architecture") == "armcortexm"


def test_retry_drops_architecture_on_unsupported(monkeypatch, fake_elf, caplog):
    """When asmslicer raises 'unsupported provided elf architecture armcortexm',
    we retry without architecture= so the wheel can auto-detect from EM_ARM."""
    calls = []

    def fake_process(**kwargs):
        calls.append(dict(kwargs))
        if calls[0].get("architecture") == "armcortexm" and len(calls) == 1:
            raise RuntimeError(
                "unsupported provided elf architecture armcortexm"
            )
        # Second call (no architecture) — simulate success.
        symmap = Path(kwargs.get("out_symmap", ""))
        if symmap and not symmap.exists():
            symmap.write_text("symbol,name,long_name,start_address,size,namespace\n")

    _patch_asmslicer(monkeypatch, fake_process)
    with caplog.at_level(logging.WARNING, logger="loci.asm-analyze"):
        asm_analyze.run_analysis(str(fake_elf), architecture="cortexm")
    # First call had architecture; second did not.
    assert len(calls) == 2
    assert calls[0].get("architecture") == "armcortexm"
    assert "architecture" not in calls[1]
    # The retry must be logged so users can see why it happened.
    assert any("retrying with auto-detection" in r.message for r in caplog.records)


def test_no_retry_when_unrelated_exception(monkeypatch, fake_elf):
    """An unrelated exception (e.g. file IO) must propagate, not trigger a retry."""
    calls = []

    def fake_process(**kwargs):
        calls.append(dict(kwargs))
        raise RuntimeError("disk write failed")

    _patch_asmslicer(monkeypatch, fake_process)
    with pytest.raises(RuntimeError, match="disk write failed"):
        asm_analyze.run_analysis(str(fake_elf), architecture="cortexm")
    assert len(calls) == 1  # no retry attempted


def test_no_retry_when_architecture_was_none(monkeypatch, fake_elf):
    """If the user didn't specify --arch, there's nothing to retry without."""
    calls = []

    def fake_process(**kwargs):
        calls.append(dict(kwargs))
        raise RuntimeError("unsupported provided elf architecture armcortexm")

    _patch_asmslicer(monkeypatch, fake_process)
    with pytest.raises(RuntimeError, match="unsupported"):
        asm_analyze.run_analysis(str(fake_elf), architecture=None)
    assert len(calls) == 1


def test_retry_only_when_message_mentions_provided_arch(monkeypatch, fake_elf):
    """The retry guard requires the exception message to actually reference
    the architecture we sent — otherwise we don't know it's the validator
    rejecting our value, and we'd risk masking a real error."""
    calls = []

    def fake_process(**kwargs):
        calls.append(dict(kwargs))
        # 'unsupported' but no 'armcortexm' — generic asmslicer error.
        raise RuntimeError("unsupported feature: jump tables")

    _patch_asmslicer(monkeypatch, fake_process)
    with pytest.raises(RuntimeError, match="unsupported feature"):
        asm_analyze.run_analysis(str(fake_elf), architecture="cortexm")
    assert len(calls) == 1  # no retry — guard saw arch wasn't mentioned
