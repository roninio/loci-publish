"""End-to-end orchestrator tests — cascade walker + doomed guard + diff.

These test the integration glue in build_metadata.py that stitches the
flag_sources modules together: cascade walking, partial-merge, the
doomed-compile static guard, and meta schema v1/v2 round-trips.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import build_metadata as bm
from build_metadata import (
    FlagDecision, AttemptRecord,
    _check_doomed_compile, _include_paths_from_flags,
    _format_insufficient_error, _read_includes,
    detect_flags_verbose, diff_metas,
)
from flag_sources import DiscoveryResult

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Orchestrator: detect_flags_verbose end-to-end
# ---------------------------------------------------------------------------

class TestDetectFlagsVerbose:
    def test_compile_commands_wins_when_present(self, tmp_path):
        src = tmp_path / "foo.c"
        src.write_text("")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(src),
            "arguments": ["arm-none-eabi-gcc",
                          "-g", "-O2", "-mcpu=cortex-m4", "-mthumb",
                          "-I/opt/inc", "-DFOO=1",
                          "-c", str(src), "-o", "foo.o"],
        }]))
        decision = detect_flags_verbose(src, "armv7e-m", tmp_path, {})
        assert decision.kind == "compile_commands"
        assert decision.confidence == "exact"
        assert "-DFOO=1" in decision.flags
        assert "-I/opt/inc" in decision.flags
        # attempts trace must contain compile_commands accepted
        assert any(a.kind == "compile_commands" and a.result == "accepted"
                   for a in decision.attempts)

    def test_defaults_when_no_sources(self, tmp_path):
        # Isolate fully — use a fresh subdir and restrict the build-root
        # ranker so the cascade can't pick up stray test artifacts.
        isolated = tmp_path / "iso"
        isolated.mkdir()
        src = isolated / "foo.c"
        src.write_text("")
        with patch("flag_sources.build_root._iter_candidates",
                   return_value=[isolated.resolve()]):
            decision = detect_flags_verbose(src, "armv6-m", isolated, {})
        assert decision.kind == "defaults"
        assert decision.degraded
        # Defaults for armv6-m
        assert "-mcpu=cortex-m0plus" in decision.flags
        assert "-mthumb" in decision.flags
        assert "-c" in decision.flags

    def test_cross_family_compile_commands_rejected(self, tmp_path):
        """compile_commands with wrong arch family must be rejected;
        cascade falls back to defaults with an attempts-trace rejection."""
        src = tmp_path / "foo.cpp"
        src.write_text("")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(src),
            "arguments": ["g++", "-O2", "-c", str(src)],
        }]))
        decision = detect_flags_verbose(src, "armv7e-m", tmp_path, {})
        # Should fall through to defaults
        assert decision.kind == "defaults"
        # Attempt record for compile_commands should be rejected-wrong-arch
        rejected = [a for a in decision.attempts
                    if a.kind == "compile_commands"]
        assert len(rejected) == 1
        assert rejected[0].result == "rejected-wrong-arch"

    def test_user_override_replace_wins_above_compile_commands(self, tmp_path):
        """When the user writes .loci-build/flags.json with mode=replace,
        the cascade should short-circuit before compile_commands."""
        src = tmp_path / "foo.c"
        src.write_text("")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(src),
            "arguments": ["arm-none-eabi-gcc", "-DREAL=1", "-I/real",
                          "-c", str(src)],
        }]))
        (tmp_path / ".loci-build").mkdir()
        (tmp_path / ".loci-build" / "flags.json").write_text(json.dumps({
            "mode": "replace",
            "compiler": "arm-none-eabi-gcc",
            "flags": ["-DOVERRIDE=1", "-I/override", "-mcpu=cortex-m0plus",
                      "-mthumb", "-g", "-c"],
        }))
        decision = detect_flags_verbose(src, "armv6-m", tmp_path, {})
        assert decision.kind == "user-override-replace"
        assert "-DOVERRIDE=1" in decision.flags
        # compile_commands.json was not consulted — cascade short-circuited
        assert "-DREAL=1" not in decision.flags

    def test_user_override_augment_adds_on_top(self, tmp_path):
        src = tmp_path / "foo.c"
        src.write_text("")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(src),
            "arguments": ["arm-none-eabi-gcc", "-I/base", "-mcpu=cortex-m0plus",
                          "-mthumb", "-g", "-c", str(src)],
        }]))
        (tmp_path / ".loci-build").mkdir()
        (tmp_path / ".loci-build" / "flags.json").write_text(json.dumps({
            "mode": "augment",
            "flags": ["-DADDED=1"],
        }))
        decision = detect_flags_verbose(src, "armv6-m", tmp_path, {})
        assert decision.kind == "compile_commands"
        assert decision.user_override_applied is True
        assert "-I/base" in decision.flags
        assert "-DADDED=1" in decision.flags

    def test_user_override_build_root_forces_build_dir(self, tmp_path):
        """Setting build_root in flags.json must force the cascade to
        look there, not at the default scored candidate."""
        src = tmp_path / "src" / "foo.c"
        src.parent.mkdir()
        src.write_text("")
        real_build = tmp_path / "real-build"
        real_build.mkdir()
        (real_build / "makefile").write_text(
            "CC=fake\nfoo.o: foo.c\n\t$(CC) -c $< -o $@\n"
        )
        (tmp_path / ".loci-build").mkdir()
        (tmp_path / ".loci-build" / "flags.json").write_text(json.dumps({
            "mode": "augment",
            "build_root": "real-build",
        }))
        # Patch gmake_dryrun to verify it sees our forced build_dir
        captured: list[Path] = []
        import flag_sources.gmake_dryrun as gd_mod
        orig_discover = gd_mod.discover

        def _capturing_discover(source, target, root, ctx, build_dir):
            captured.append(build_dir)
            return None

        with patch.object(gd_mod, "discover", side_effect=_capturing_discover):
            detect_flags_verbose(src, "armv6-m", tmp_path, {})
        # The gmake discover step should have been called with our forced dir
        assert any(bd == real_build.resolve() for bd in captured)

    def test_partial_merge_projectspec_plus_defaults(self, tmp_path):
        """When a source like a sibling_obj_dwarf or projectspec returns
        only -I/-D (partial), the cascade must still accept it and merge
        arch flags from defaults."""
        src = tmp_path / "foo.c"
        src.write_text("")
        ps = tmp_path / "p.projectspec"
        ps.write_text(
            '<?xml version="1.0"?>\n'
            '<projectSpec>\n'
            '  <project toolChain="TICLANG" '
            'compilerBuildOptions="-I/sdk -DCC23X0 -mcpu=cortex-m4 '
            '-mthumb -std=c99"/>\n'
            '</projectSpec>\n'
        )
        decision = detect_flags_verbose(src, "armv6-m", tmp_path, {})
        # Either defaults or merged-partial:projectspec-xml — but should
        # have the include and define preserved from the projectspec.
        assert "-DCC23X0" in decision.flags
        assert "-I/sdk" in decision.flags
        # Arch flags from defaults (the projectspec's m4 was stripped)
        assert "-mcpu=cortex-m0plus" in decision.flags or \
               "-mcpu=cortex-m4" not in decision.flags

    def test_effective_target_flip_on_cpu_downgrade(self, tmp_path):
        """Session says armv7e-m but compile_commands shows cortex-m0plus.
        Cascade should accept, flip effective_loci_target to armv6-m,
        and record cpu_override in decision details."""
        src = tmp_path / "foo.c"
        src.write_text("")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(src),
            "arguments": ["arm-none-eabi-gcc", "-mcpu=cortex-m0plus",
                          "-mthumb", "-I/sdk", "-c", str(src)],
        }]))
        decision = detect_flags_verbose(src, "armv7e-m", tmp_path, {})
        assert decision.kind == "compile_commands"
        assert decision.effective_loci_target == "armv6-m"
        assert decision.cpu_override is not None
        assert decision.cpu_override["discovered_cpu"] == "cortex-m0plus"


# ---------------------------------------------------------------------------
# Doomed-compile static guard
# ---------------------------------------------------------------------------

class TestDoomedCompileGuard:
    def _make_decision(self, flags=None) -> FlagDecision:
        return FlagDecision(
            compiler="arm-none-eabi-gcc",
            flags=flags or ["-g", "-c"],
            kind="defaults",
            confidence="low",
            details={},
            warnings=[],
            attempts=[AttemptRecord(
                kind="compile_commands", result="missing",
                detail="no compile_commands.json",
            )],
            degraded=True,
        )

    def test_stdlib_only_passes(self, tmp_path):
        src = tmp_path / "foo.c"
        src.write_text('#include <stdio.h>\n#include <string.h>\nint main(){}\n')
        dec = self._make_decision()
        err = _check_doomed_compile(src, dec, None)
        assert err is None

    def test_generated_header_treated_as_resolved(self, tmp_path):
        src = tmp_path / "foo.c"
        src.write_text(
            '#include <ti_drivers_config.h>\n'
            '#include <FreeRTOSConfig.h>\n'
            '#include "my_header.h"\n'
            'int main(){}\n'
        )
        dec = self._make_decision()
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        # Only 1 non-stdlib non-generated header (my_header.h), guard shouldn't fail
        err = _check_doomed_compile(src, dec, build_dir)
        assert err is None

    def test_multiple_unresolved_fires(self, tmp_path):
        src = tmp_path / "foo.c"
        src.write_text(
            '#include "a.h"\n#include "b.h"\n#include "c.h"\n'
            '#include <stdio.h>\nint main(){}\n'
        )
        dec = self._make_decision()  # no -I flags
        err = _check_doomed_compile(src, dec, None)
        assert err is not None
        assert "'a.h'" in err
        assert "'b.h'" in err
        assert "LOCI cannot reliably compile" in err
        assert "How to fix" in err

    def test_resolvable_include_passes(self, tmp_path):
        inc_dir = tmp_path / "inc"
        inc_dir.mkdir()
        (inc_dir / "mine.h").write_text("")
        src = tmp_path / "foo.c"
        src.write_text(
            '#include "mine.h"\n#include "other.h"\n#include <stdio.h>\nint main(){}\n'
        )
        dec = self._make_decision(flags=["-g", "-c", f"-I{inc_dir}"])
        err = _check_doomed_compile(src, dec, None)
        # mine.h resolves, other.h doesn't — 1/2 unresolved isn't "all", just warns
        assert err is None
        assert any("unresolved_includes" in w for w in dec.warnings)

    def test_include_paths_from_flags(self):
        paths = _include_paths_from_flags([
            "-I/a", "-I/b", "-isystem", "/c", "-isystem=/d", "-DFOO",
        ])
        assert Path("/a") in paths
        assert Path("/b") in paths
        assert Path("/c") in paths
        assert Path("/d") in paths


# ---------------------------------------------------------------------------
# meta.json round-trip and diff
# ---------------------------------------------------------------------------

class TestMetaRoundTrip:
    def test_inherited_no_false_regression(self):
        """If curr is inherited from prev, diff must NOT flag a kind regression."""
        prev = {
            "compiler": "tiarmclang", "compiler_version": "3.2",
            "loci_target": "armv6-m", "architecture": "armv6-m",
            "flags": ["-g", "-c"],
            "flag_source": "gmake-dry-run",
            "flag_source_v2": {"kind": "gmake-dry-run", "confidence": "exact"},
        }
        curr = {
            "compiler": "tiarmclang", "compiler_version": "3.2",
            "loci_target": "armv6-m", "architecture": "armv6-m",
            "flags": ["-g", "-c"],
            "flag_source": "inherited from foo.meta.json",
            "flag_source_v2": {
                "kind": "inherited",
                "details": {"upstream_kind": "gmake-dry-run"},
                "confidence": "exact",
            },
        }
        lines = diff_metas(prev, curr)
        assert all("regressed" not in l for l in lines)

    def test_real_kind_regression_still_flagged(self):
        """A genuine kind change (not via inherited) still reports."""
        prev = {
            "compiler": "tiarmclang", "compiler_version": "3.2",
            "loci_target": "armv6-m", "architecture": "armv6-m",
            "flags": ["-g", "-c"],
            "flag_source": "gmake-dry-run",
            "flag_source_v2": {"kind": "gmake-dry-run", "confidence": "exact"},
        }
        curr = {
            "compiler": "tiarmclang", "compiler_version": "3.2",
            "loci_target": "armv6-m", "architecture": "armv6-m",
            "flags": ["-g", "-c"],
            "flag_source": "defaults",
            "flag_source_v2": {"kind": "defaults", "confidence": "low"},
        }
        lines = diff_metas(prev, curr)
        assert any("regressed" in l for l in lines)

    def test_stale_meta_inherited_full_string_kind_suppressed(self):
        """Older/stale metas may store the full v1 flag_source string
        ('inherited from X.meta.json.prev') in v2.kind. _norm_kind must
        treat any kind starting with 'inherited' as the canonical short
        form, so the inherit carve-out applies and we don't false-positive
        a 'discovery regressed' on the inherit path."""
        prev = {
            "compiler": "tiarmclang", "compiler_version": "3.2",
            "loci_target": "armv7e-m", "architecture": "armv7e-m",
            "flags": ["-g", "-c"],
            "flag_source": "user-override-replace",
            "flag_source_v2": {"kind": "user-override-replace", "confidence": "exact"},
        }
        curr = {
            "compiler": "tiarmclang", "compiler_version": "3.2",
            "loci_target": "armv7e-m", "architecture": "armv7e-m",
            "flags": ["-g", "-c"],
            "flag_source": "inherited from rom_init.o.meta.json.prev",
            "flag_source_v2": {
                "kind": "inherited from rom_init.o.meta.json.prev",
                "confidence": "exact",
            },
        }
        lines = diff_metas(prev, curr)
        assert all("regressed" not in l for l in lines)

    def test_v1_v2_mixed_doesnt_crash(self):
        v1 = {
            "compiler": "gcc", "compiler_version": "10",
            "loci_target": "aarch64", "architecture": "aarch64",
            "flags": ["-g", "-O2", "-c"],
            "flag_source": "defaults",
        }
        v2 = dict(v1)
        v2["flag_source_v2"] = {"kind": "defaults", "confidence": "low"}
        assert diff_metas(v1, v2) == []


# ---------------------------------------------------------------------------
# CLI via compile_subcommand — smoke tests for exit codes and structured error
# ---------------------------------------------------------------------------

class TestCompileSubcommandErrorPaths:
    def test_structured_error_emitted_when_no_flags_resolve(self, tmp_path, capsys):
        """When the cascade produces only defaults and the source has
        multiple unresolved non-stdlib headers, compile must fail fast
        with the structured error — before invoking the compiler."""
        src = tmp_path / "foo.c"
        src.write_text(
            '#include "a.h"\n#include "b.h"\n'
            '#include "c.h"\nint main(){}\n'
        )
        import argparse
        args = argparse.Namespace(
            source=str(src),
            loci_target="armv6-m",
            context=None,
            project_root=str(tmp_path),
            output=None,
            meta_prev=None,
            phase="preflight",
            verbose=False,
        )
        # Isolate the build-root ranker so it doesn't pick up sibling tmps
        with patch("flag_sources.build_root._iter_candidates",
                   return_value=[tmp_path.resolve()]):
            rc = bm.compile_subcommand(args)
        captured = capsys.readouterr()
        assert rc == 1
        assert "LOCI cannot reliably compile" in captured.err
        assert "'a.h'" in captured.err or "'b.h'" in captured.err
        assert "How to fix" in captured.err
        assert "Flag source chain tried" in captured.err


# ---------------------------------------------------------------------------
# User override edge cases
# ---------------------------------------------------------------------------

class TestUserOverrideEdgeCases:
    def test_malformed_json_is_tolerated(self, tmp_path):
        (tmp_path / ".loci-build").mkdir()
        (tmp_path / ".loci-build" / "flags.json").write_text("{ not json")
        # Should not raise — load_override silently returns empty
        from flag_sources.user_override import load_override
        ov = load_override(tmp_path)
        assert ov.empty

    def test_override_file_missing_is_ok(self, tmp_path):
        from flag_sources.user_override import load_override
        ov = load_override(tmp_path)
        assert ov.empty

    def test_variables_expanded_in_flags(self, tmp_path):
        (tmp_path / ".loci-build").mkdir()
        (tmp_path / ".loci-build" / "flags.json").write_text(json.dumps({
            "mode": "replace",
            "compiler": "arm-none-eabi-gcc",
            "flags": ["-I${PROJECT_ROOT}/inc", "-I${SDK_DIR}/src"],
            "variables": {"SDK_DIR": "/opt/sdk"},
        }))
        src = tmp_path / "foo.c"
        src.write_text("")
        from flag_sources.user_override import discover as override_discover
        result = override_discover(src, "armv6-m", tmp_path, {}, None)
        assert result is not None
        assert any(str(tmp_path) in f for f in result.flags)
        assert "-I/opt/sdk/src" in result.flags


# ---------------------------------------------------------------------------
# Response file edge cases
# ---------------------------------------------------------------------------

class TestResponseFileEdgeCases:
    def test_nested_response_files(self, tmp_path):
        from flag_sources.response_file import expand_response_files

        inner = tmp_path / "inner.opt"
        inner.write_text("-DFROM_INNER=1\n")
        outer = tmp_path / "outer.opt"
        outer.write_text("@inner.opt\n-DFROM_OUTER=1\n")

        expanded, aug = expand_response_files(["@outer.opt"], tmp_path)
        assert "-DFROM_INNER=1" in expanded
        assert "-DFROM_OUTER=1" in expanded

    def test_circular_response_file_bounded(self, tmp_path):
        from flag_sources.response_file import expand_response_files

        a = tmp_path / "a.opt"
        b = tmp_path / "b.opt"
        a.write_text("@b.opt\n")
        b.write_text("@a.opt\n")

        # Should terminate, not stack-overflow
        expanded, aug = expand_response_files(["@a.opt"], tmp_path)
        # Output is whatever the depth-limit yielded — just assert non-crash
        assert isinstance(expanded, list)


# ---------------------------------------------------------------------------
# gmake_dryrun behavior
# ---------------------------------------------------------------------------

class TestGmakeDryrunBehavior:
    def test_raises_when_make_missing(self, tmp_path, monkeypatch):
        """If a makefile is present but GNU Make is not on PATH, the
        discover() must raise so the orchestrator records 'error' — not
        silently fall through to the next source."""
        from flag_sources import gmake_dryrun as gd
        build_dir = tmp_path / "bd"
        build_dir.mkdir()
        (build_dir / "makefile").write_text(
            "CC=gcc\nfoo.obj: foo.c\n\t$(CC) -c $< -o $@\n"
        )
        src = tmp_path / "foo.c"
        src.write_text("")

        monkeypatch.setattr(gd, "_find_make", lambda: (None, None))

        with pytest.raises(RuntimeError, match="GNU Make"):
            gd.discover(src, "armv6-m", tmp_path, {}, build_dir)

    def test_no_makefile_returns_miss(self, tmp_path):
        from flag_sources import DiscoveryMiss
        from flag_sources import gmake_dryrun as gd
        src = tmp_path / "foo.c"
        src.write_text("")
        build_dir = tmp_path / "bd"
        build_dir.mkdir()
        # No makefile in build_dir → DiscoveryMiss with a specific reason
        # (was bare None historically; replaced so the orchestrator can
        # surface a precise message instead of falling back to the
        # hardcoded `_reason_for_missing` category string).
        result = gd.discover(src, "armv6-m", tmp_path, {}, build_dir)
        assert isinstance(result, DiscoveryMiss)
        assert "no makefile in build_dir" in result.reason
        assert str(build_dir) in result.reason

    def test_no_build_dir_returns_miss(self, tmp_path):
        from flag_sources import DiscoveryMiss
        from flag_sources import gmake_dryrun as gd
        src = tmp_path / "foo.c"
        src.write_text("")
        result = gd.discover(src, "armv6-m", tmp_path, {}, None)
        assert isinstance(result, DiscoveryMiss)
        assert "no build_dir" in result.reason

    def test_build_dir_not_a_directory_returns_miss(self, tmp_path):
        from flag_sources import DiscoveryMiss
        from flag_sources import gmake_dryrun as gd
        src = tmp_path / "foo.c"
        src.write_text("")
        # A path that exists as a file, not a directory
        bd_as_file = tmp_path / "bd_is_a_file"
        bd_as_file.write_text("")
        result = gd.discover(src, "armv6-m", tmp_path, {}, bd_as_file)
        assert isinstance(result, DiscoveryMiss)
        assert "not a directory" in result.reason

    def test_makefile_without_donor_returns_miss(self, tmp_path):
        from flag_sources import DiscoveryMiss
        from flag_sources import gmake_dryrun as gd
        src = tmp_path / "foo.c"
        src.write_text("")
        build_dir = tmp_path / "bd"
        build_dir.mkdir()
        # Makefile with no OBJECTS/OBJS line and no <name>.obj: rule
        (build_dir / "makefile").write_text("all:\n\techo hello\n")
        result = gd.discover(src, "armv6-m", tmp_path, {}, build_dir)
        assert isinstance(result, DiscoveryMiss)
        assert "no donor target" in result.reason

    def test_donor_dryrun_failure_falls_back_to_next(self, tmp_path):
        """Regression for the May-2026 'no makefile in build_dir' false
        report (Melisa). The first OBJECTS entry's `.c` source is absent;
        make --dry-run exits rc=2 with empty stdout. The discoverer must
        NOT silently give up — it must retry the next donor and succeed."""
        from flag_sources import DiscoveryResult
        from flag_sources import gmake_dryrun as gd

        build_dir = tmp_path / "bd"
        build_dir.mkdir()
        # `good.c` exists; `missing_donor.c` does not.
        (build_dir / "good.c").write_text("int main(void){return 0;}\n")
        (build_dir / "makefile").write_text(
            "CC = gcc\n"
            "CFLAGS = -I. -DOK=1\n"
            "OBJECTS = missing_donor.obj good.obj\n"
            "missing_donor.obj: missing_donor.c\n"
            "\t$(CC) $(CFLAGS) -c $< -o $@\n"
            "good.obj: good.c\n"
            "\t$(CC) $(CFLAGS) -c $< -o $@\n"
        )
        src = tmp_path / "external.c"
        src.write_text("")

        # Force gcc to be discovered by _extract_compile_line (which keys
        # on `_KNOWN_COMPILER_BASENAMES`). 'gcc' is in that list.
        result = gd.discover(src, "armv6-m", tmp_path, {}, build_dir)
        assert isinstance(result, DiscoveryResult), \
            f"expected DiscoveryResult, got {type(result).__name__}: {result!r}"
        assert result.kind == "gmake-dry-run"
        # Picked the second donor after the first failed
        assert result.details.get("target") == "good.obj"
        # Fallback chain recorded for diagnostics
        assert "donor_fallbacks" in result.details
        assert any(
            "missing_donor.obj" in f for f in result.details["donor_fallbacks"]
        )
        # Flags survived the dry-run extraction
        assert "-DOK=1" in result.flags

    def test_all_donors_fail_returns_miss_with_chain(self, tmp_path):
        """When every donor fails, the DiscoveryMiss reason must include
        each donor name and a stderr tail — so the user can see the real
        problem instead of the generic 'no makefile in build_dir'."""
        from flag_sources import DiscoveryMiss
        from flag_sources import gmake_dryrun as gd

        build_dir = tmp_path / "bd"
        build_dir.mkdir()
        (build_dir / "makefile").write_text(
            "CC = gcc\n"
            "OBJECTS = a.obj b.obj\n"
            "a.obj: missing_a.c\n"
            "\t$(CC) -c $< -o $@\n"
            "b.obj: missing_b.c\n"
            "\t$(CC) -c $< -o $@\n"
        )
        src = tmp_path / "ext.c"
        src.write_text("")

        result = gd.discover(src, "armv6-m", tmp_path, {}, build_dir)
        assert isinstance(result, DiscoveryMiss)
        # The reason must mention both donors (so the user can see the
        # full chain that was tried, not just the first failure)
        assert "a.obj" in result.reason
        assert "b.obj" in result.reason
        # And must mention this is a dry-run failure, not a missing-file one
        assert "rc=" in result.reason

    def test_pick_donor_targets_returns_multiple_in_order(self, tmp_path):
        from flag_sources import gmake_dryrun as gd
        mk = tmp_path / "makefile"
        mk.write_text(
            "OBJECTS = a.obj b.obj c.obj $(GENERATED) d.obj\n"
            "a.obj: a.c\n\t$(CC) -c $< -o $@\n"
        )
        donors = gd._pick_donor_targets(mk)
        # Skips $(...) tokens; returns up to limit in declared order.
        assert donors[:4] == ["a.obj", "b.obj", "c.obj", "d.obj"]

    def test_pick_donor_targets_caps_at_limit(self, tmp_path):
        from flag_sources import gmake_dryrun as gd
        mk = tmp_path / "makefile"
        # 10 candidates, but limit=3
        objs = " ".join(f"o{i}.obj" for i in range(10))
        mk.write_text(f"OBJECTS = {objs}\n")
        donors = gd._pick_donor_targets(mk, limit=3)
        assert donors == ["o0.obj", "o1.obj", "o2.obj"]


# ---------------------------------------------------------------------------
# Orchestrator: DiscoveryMiss surfaces precise reasons in attempt trace
# ---------------------------------------------------------------------------

class TestOrchestratorDiscoveryMiss:
    def test_discovery_miss_reason_in_attempt_trace(self, tmp_path):
        """When a discoverer returns DiscoveryMiss(reason=...), the
        orchestrator must put `reason` in the AttemptRecord's `detail`
        — NOT the hardcoded `_reason_for_missing` category string."""
        src = tmp_path / "foo.c"
        src.write_text("")
        # No build artifacts at all → cascade falls through. The
        # gmake-dry-run step will return DiscoveryMiss because there's
        # no build_dir/makefile.
        decision = detect_flags_verbose(src, "armv6-m", tmp_path, {})
        gmake_attempts = [a for a in decision.attempts if a.kind == "gmake-dry-run"]
        assert gmake_attempts, "gmake-dry-run step must be recorded"
        # The detail must carry a specific reason, not the legacy
        # category string
        detail = gmake_attempts[0].detail
        # If a build_dir was discovered upstream (via build_root), the
        # gmake step's reason mentions the makefile path it looked for;
        # otherwise it mentions no build_dir. Either way it's specific.
        assert detail, "missing-attempt detail must not be empty"
        assert detail != "no makefile in build_dir", (
            "regression: orchestrator fell back to the legacy hardcoded "
            "category string instead of using DiscoveryMiss.reason"
        )

    def test_legacy_none_return_still_uses_reason_for_missing(self, tmp_path, monkeypatch):
        """A discoverer returning bare `None` (legacy contract) must
        still get the `_reason_for_missing` category string. This keeps
        the migration low-risk: we don't have to update every discoverer."""
        from flag_sources import gmake_dryrun as gd_mod
        # Force gmake-dry-run to legacy bare-None behavior
        monkeypatch.setattr(gd_mod, "discover", lambda *a, **kw: None)

        src = tmp_path / "foo.c"
        src.write_text("")
        decision = detect_flags_verbose(src, "armv6-m", tmp_path, {})
        gmake_attempts = [a for a in decision.attempts if a.kind == "gmake-dry-run"]
        assert gmake_attempts
        # The orchestrator falls back to _reason_for_missing for
        # bare-None — its current category string contains 'makefile'
        # or 'build_dir'.
        assert any(
            kw in gmake_attempts[0].detail
            for kw in ("makefile", "build_dir")
        )
