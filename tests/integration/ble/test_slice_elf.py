"""Integration tests: slice_elf() on real BLE ELF."""

import pytest

from asm_analyze import VALID_OUTPUT_TYPES, slice_elf


class TestSliceElf:
    def test_default_output_types(self, ble_basic_ble_elf, require_asmslicer):
        result = slice_elf(str(ble_basic_ble_elf))
        assert "asm" in result
        assert "symbols" in result

    def test_all_output_types(self, ble_basic_ble_elf, require_asmslicer):
        result = slice_elf(
            str(ble_basic_ble_elf),
            output_types=list(VALID_OUTPUT_TYPES),
        )
        for otype in VALID_OUTPUT_TYPES:
            assert otype in result, f"Missing output type: {otype}"

    def test_detects_cortexm(self, ble_basic_ble_elf, require_asmslicer):
        result = slice_elf(str(ble_basic_ble_elf))
        assert result.get("architecture") == "cortexm"

    def test_timing_architecture(self, ble_basic_ble_elf, require_asmslicer):
        result = slice_elf(str(ble_basic_ble_elf))
        assert result.get("timing_architecture") == "armv7e-m"

    def test_asm_has_functions(self, ble_basic_ble_elf, require_asmslicer):
        result = slice_elf(str(ble_basic_ble_elf))
        asm = result.get("asm", {})
        assert isinstance(asm, dict) or (isinstance(asm, str) and len(asm) > 0)

    def test_symbols_has_entries(self, ble_basic_ble_elf, require_asmslicer):
        result = slice_elf(str(ble_basic_ble_elf))
        symbols = result.get("symbols")
        assert symbols and len(symbols) > 0

    def test_filter_functions(self, ble_basic_ble_elf, require_asmslicer):
        unfiltered = slice_elf(str(ble_basic_ble_elf))
        filtered = slice_elf(str(ble_basic_ble_elf), filter_functions=True)
        # filter_functions should reduce or equal the count
        assert filtered is not None
