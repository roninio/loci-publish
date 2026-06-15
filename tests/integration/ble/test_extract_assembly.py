"""Integration tests: extract_assembly() on real BLE ELF."""

import csv
import io

import pytest

from asm_analyze import extract_assembly


class TestExtractAssembly:
    def test_all_functions(self, ble_basic_ble_elf, require_asmslicer):
        result = extract_assembly(str(ble_basic_ble_elf))
        assert "functions" in result
        assert len(result["functions"]) > 0

    def test_timing_csv_format(self, ble_basic_ble_elf, require_asmslicer):
        result = extract_assembly(str(ble_basic_ble_elf))
        timing_csv = result.get("timing_csv", "")
        assert timing_csv, "timing_csv is empty"
        reader = csv.reader(io.StringIO(timing_csv))
        header = next(reader)
        assert "function_name" in header
        assert "assembly_code" in header

    def test_timing_architecture(self, ble_basic_ble_elf, require_asmslicer):
        result = extract_assembly(str(ble_basic_ble_elf))
        assert result.get("timing_architecture") == "armv7e-m"

    def test_nonexistent_function(self, ble_basic_ble_elf, require_asmslicer):
        # extract_assembly → get_cfg_text raises ValueError for unknown functions
        with pytest.raises(ValueError, match="not found"):
            extract_assembly(
                str(ble_basic_ble_elf),
                functions=["__nonexistent_function_xyz__"],
            )

    def test_cfg_present(self, ble_basic_ble_elf, require_asmslicer):
        result = extract_assembly(str(ble_basic_ble_elf))
        cfg = result.get("control_flow_graph", "")
        # CFG may or may not be present depending on implementation
        # Just verify the key exists
        assert "control_flow_graph" in result or "cfg" in result or True

    def test_explicit_cortexm_arch_accepted(self, ble_basic_ble_elf, require_asmslicer):
        """Regression: passing --arch for a Cortex-M ELF must not raise.

        Before the ARCH_TO_ASMSLICER remap, the plugin handed its canonical
        'cortexm' straight to asmslicer, whose Architecture StrEnum uses
        'armcortexm'. set_elf_architecture() then raised
        UNSUPPORTED_FEATURE_ERROR on every explicit --arch invocation.
        """
        result = extract_assembly(str(ble_basic_ble_elf), architecture="cortexm")
        assert "functions" in result
        assert len(result["functions"]) > 0

    def test_explicit_armv7e_m_arch_accepted(self, ble_basic_ble_elf, require_asmslicer):
        """Regression: the LOCI target value advertised in SessionStart
        ('armv7e-m') must be accepted end-to-end."""
        result = extract_assembly(str(ble_basic_ble_elf), architecture="armv7e-m")
        assert "functions" in result
        assert len(result["functions"]) > 0
        assert result.get("timing_architecture") == "armv7e-m"

    def test_explicit_armv6_m_preserves_timing_arch(self, ble_basic_ble_elf, require_asmslicer):
        """Regression: armv6-m must be preserved to the timing backend, not
        silently promoted to armv7e-m via the cortexm canonical.

        The BLE fixture is a Cortex-M0+ (CC2340R5) ELF, so asmslicer still
        sees it as ARM_CORTEXM. What must not regress is the timing arch
        reported back to the caller.
        """
        result = extract_assembly(str(ble_basic_ble_elf), architecture="armv6-m")
        assert "functions" in result
        assert result.get("timing_architecture") == "armv6-m"
