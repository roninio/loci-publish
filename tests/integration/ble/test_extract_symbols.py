"""Integration tests: extract_symbols() on real BLE ELF."""

import pytest

from asm_analyze import extract_symbols


class TestExtractSymbols:
    def test_returns_list(self, ble_basic_ble_elf, require_asmslicer):
        result = extract_symbols(str(ble_basic_ble_elf))
        symbols = result.get("symbols", [])
        assert len(symbols) > 0

    def test_fields(self, ble_basic_ble_elf, require_asmslicer):
        result = extract_symbols(str(ble_basic_ble_elf))
        symbols = result.get("symbols", [])
        for sym in symbols[:5]:  # check first 5
            assert "name" in sym
            assert "long_name" in sym
            assert "start_address" in sym
            assert "size" in sym

    def test_architecture(self, ble_basic_ble_elf, require_asmslicer):
        result = extract_symbols(str(ble_basic_ble_elf))
        assert result.get("architecture") == "cortexm"

    def test_timing_architecture_auto_detect(self, ble_basic_ble_elf, require_asmslicer):
        result = extract_symbols(str(ble_basic_ble_elf))
        assert result.get("timing_architecture") == "armv7e-m"

    def test_timing_architecture_explicit_arch(self, ble_basic_ble_elf, require_asmslicer):
        result = extract_symbols(str(ble_basic_ble_elf), architecture="armv7e-m")
        assert result.get("timing_architecture") == "armv7e-m"
        assert result.get("architecture") == "cortexm"
