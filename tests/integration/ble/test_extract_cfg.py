"""Integration tests: extract_cfg() on real BLE ELF."""

import pytest

from asm_analyze import extract_cfg


class TestExtractCfg:
    def test_returns_dict_with_cfg(self, ble_basic_ble_elf, require_asmslicer):
        """extract_cfg now returns {"control_flow_graph": <text>}; the CLI
        main() is what emits the text to stdout after capturing any
        library-side leakage. This prevents third-party prints from
        pre-pending garbage to the CFG output."""
        result = extract_cfg(str(ble_basic_ble_elf), architecture=None, functions=None)
        assert isinstance(result, dict)
        assert "control_flow_graph" in result
        assert isinstance(result["control_flow_graph"], str)
        assert len(result["control_flow_graph"]) > 0

    def test_contains_blocks(self, ble_basic_ble_elf, require_asmslicer):
        result = extract_cfg(str(ble_basic_ble_elf), architecture=None, functions=None)
        assert len(result["control_flow_graph"].strip()) > 0
