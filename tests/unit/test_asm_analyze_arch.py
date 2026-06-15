"""Tests for resolve_arch(), timing_arch(), and architecture constants."""

import pytest

from asm_analyze import (
    ARCH_ALIAS_TO_TIMING,
    ARCH_ALIASES,
    ARCH_TO_ASMSLICER,
    ARCH_TO_TIMING,
    TIMING_TO_ARCH,
    asmslicer_arch,
    resolve_arch,
    resolve_timing_arch,
    timing_arch,
)

pytestmark = pytest.mark.unit


# -- resolve_arch ----------------------------------------------------------

class TestResolveArch:
    def test_canonical(self):
        assert resolve_arch("aarch64") == "aarch64"

    def test_alias_arm64(self):
        assert resolve_arch("arm64") == "aarch64"

    def test_alias_cortex_m4(self):
        assert resolve_arch("cortex-m4") == "cortexm"

    def test_alias_armv8m_main(self):
        assert resolve_arch("armv8-m.main") == "cortexm"

    def test_alias_armv8m_base(self):
        assert resolve_arch("armv8-m.base") == "cortexm"

    def test_alias_cortex_m33(self):
        assert resolve_arch("cortex-m33") == "cortexm"

    def test_alias_cortex_m23(self):
        assert resolve_arch("cortex-m23") == "cortexm"

    def test_alias_cortex_m55(self):
        assert resolve_arch("cortex-m55") == "cortexm"

    def test_alias_cortex_m85(self):
        assert resolve_arch("cortex-m85") == "cortexm"

    def test_alias_tc399(self):
        assert resolve_arch("tc399") == "tricore"

    def test_case_insensitive(self):
        assert resolve_arch("AArch64") == "aarch64"

    def test_whitespace_stripped(self):
        assert resolve_arch(" cortexm ") == "cortexm"

    def test_unknown_returns_none(self):
        assert resolve_arch("riscv") is None

    def test_none_input(self):
        assert resolve_arch(None) is None


# -- timing_arch -----------------------------------------------------------

class TestTimingArch:
    def test_known_cortexm(self):
        assert timing_arch("cortexm") == "armv7e-m"

    def test_known_aarch64(self):
        assert timing_arch("aarch64") == "aarch64"

    def test_known_tricore(self):
        assert timing_arch("tricore") == "tc399"

    def test_passthrough_unknown(self):
        assert timing_arch("unknown") == "unknown"


# -- constant consistency -------------------------------------------------

class TestArchConstants:
    def test_aliases_map_to_known_arch(self):
        """Every alias value must be a key in ARCH_TO_TIMING."""
        for alias, canonical in ARCH_ALIASES.items():
            assert canonical in ARCH_TO_TIMING, (
                f"ARCH_ALIASES[{alias!r}] = {canonical!r} not in ARCH_TO_TIMING"
            )

    def test_timing_to_arch_roundtrip(self):
        """TIMING_TO_ARCH is the consistent inverse of ARCH_TO_TIMING."""
        for arch, timing in ARCH_TO_TIMING.items():
            assert TIMING_TO_ARCH[timing] == arch


# -- asmslicer_arch --------------------------------------------------------

class TestAsmslicerArch:
    """Regression: the plugin canonical 'cortexm' must be remapped to
    'armcortexm' before being handed to loci-service-asmslicer, otherwise
    set_elf_architecture() raises UNSUPPORTED_FEATURE_ERROR on every
    Cortex-M invocation that specifies --arch explicitly."""

    def test_cortexm_maps_to_armcortexm(self):
        assert asmslicer_arch("cortexm") == "armcortexm"

    def test_aarch64_passthrough(self):
        assert asmslicer_arch("aarch64") == "aarch64"

    def test_tricore_passthrough(self):
        assert asmslicer_arch("tricore") == "tricore"

    def test_none(self):
        assert asmslicer_arch(None) is None

    def test_unknown_passthrough(self):
        assert asmslicer_arch("riscv") == "riscv"

    def test_canonicals_covered(self):
        """Every canonical from ARCH_ALIASES values has a mapping."""
        for canonical in set(ARCH_ALIASES.values()):
            assert canonical in ARCH_TO_ASMSLICER


# -- resolve_timing_arch ---------------------------------------------------

class TestResolveTimingArch:
    """Cortex-M sub-arch (armv6-m) must survive to the timing backend rather
    than being collapsed to armv7e-m via the cortexm canonical."""

    def test_armv6_m_preserved(self):
        assert resolve_timing_arch("armv6-m") == "armv6-m"

    def test_armv7e_m_preserved(self):
        assert resolve_timing_arch("armv7e-m") == "armv7e-m"

    def test_armv8_m_base_to_armv6_m(self):
        assert resolve_timing_arch("armv8-m.base") == "armv6-m"

    def test_armv8_m_main_to_armv7e_m(self):
        assert resolve_timing_arch("armv8-m.main") == "armv7e-m"

    def test_cortex_m0_to_armv6_m(self):
        assert resolve_timing_arch("cortex-m0") == "armv6-m"

    def test_cortex_m23_to_armv6_m(self):
        assert resolve_timing_arch("cortex-m23") == "armv6-m"

    def test_cortex_m4_to_armv7e_m(self):
        assert resolve_timing_arch("cortex-m4") == "armv7e-m"

    def test_aarch64(self):
        assert resolve_timing_arch("aarch64") == "aarch64"

    def test_tc399(self):
        assert resolve_timing_arch("tc399") == "tc399"

    def test_tc3xx_alias_resolves_to_tc399(self):
        """'tc3xx' is an accepted alias (GCC -mcpu=tc3xx) but MCP only
        validates 'tc399', so the resolver must emit the server-accepted
        spelling."""
        assert resolve_timing_arch("tc3xx") == "tc399"

    def test_none(self):
        assert resolve_timing_arch(None) is None

    def test_unknown(self):
        assert resolve_timing_arch("riscv") is None

    def test_case_insensitive(self):
        assert resolve_timing_arch("ARMv6-M") == "armv6-m"

    def test_loci_targets_all_resolve(self):
        """Every LOCI target advertised in SessionStart must resolve to the
        exact name the MCP server's architecture_to_model dict accepts."""
        for loci_target in ("aarch64", "armv7e-m", "armv6-m", "tc399"):
            assert resolve_timing_arch(loci_target) == loci_target
