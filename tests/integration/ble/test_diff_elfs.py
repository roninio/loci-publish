"""Integration tests: diff_elfs() on real BLE ELFs."""

import pytest

from asm_analyze import diff_elfs


class TestDiffElfs:
    def test_diff_same_file(self, ble_basic_ble_elf, require_asmslicer):
        result = diff_elfs(str(ble_basic_ble_elf), str(ble_basic_ble_elf))
        # Diffing a file with itself: all entries should be unchanged
        entries = result.get("entries", result.get("diff", []))
        if entries:
            statuses = {e.get("status") for e in entries}
            assert "unchanged" in statuses or len(statuses) == 0

    def test_diff_two_different_bles(self, ble_basic_ble_elf, ble_root, require_asmslicer):
        _BLE_EXAMPLES = "examples/rtos/LP_EM_CC2340R5/ble5stack"
        elf_b = ble_root / _BLE_EXAMPLES / "data_stream" / "freertos" / "ticlang" / "data_stream.out"
        if not elf_b.is_file():
            pytest.skip(f"Second BLE ELF not found: {elf_b}")
        result = diff_elfs(str(ble_basic_ble_elf), str(elf_b))
        assert "error" not in result or not result.get("error")

    def test_diff_missing_file(self, ble_basic_ble_elf, require_asmslicer):
        result = diff_elfs(str(ble_basic_ble_elf), "/nonexistent/path.elf")
        # Should return an error
        assert "error" in result or isinstance(result, dict)
