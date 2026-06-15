"""Tests for the flag_sources cascade — new sources and orchestrator."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from flag_sources import (
    AttemptRecord, DiscoveryResult, FlagDecision,
    compiler_family, has_include, is_arch_flag, is_define, is_include,
    parse_producer,
)
from flag_sources import (
    build_root as fs_build_root,
    compile_commands as fs_compile_commands,
    compiler_match as fs_compiler_match,
    gmake_dryrun as fs_gmake,
    makefile_regex as fs_mk_regex,
    projectspec_xml as fs_projectspec,
    response_file as fs_response,
    stdlib_headers as fs_stdlib,
    user_override as fs_override,
)
from flag_sources.flags_normalize import (
    dedup_preserve_order, ensure_required, keep_includes_and_defines,
    strip_arch_flags, strip_source_and_output,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# parse_producer
# ---------------------------------------------------------------------------

class TestParseProducer:
    def test_tiarmclang_with_flags(self):
        p = parse_producer(
            "tiarmclang version 3.2.2.LTS -mcpu=cortex-m0plus -mthumb "
            "-O2 -g -IC:/sdk -DCC23X0"
        )
        assert p.compiler == "tiarmclang"
        assert "-mcpu=cortex-m0plus" in p.flags
        assert "-DCC23X0" in p.flags
        assert "-IC:/sdk" in p.flags

    def test_gcc_arm_none_eabi(self):
        p = parse_producer(
            "arm-none-eabi-gcc 10.3.1 -mcpu=cortex-m4 -O3"
        )
        assert p.compiler is not None
        assert "arm-none-eabi" in p.compiler
        assert "-mcpu=cortex-m4" in p.flags

    def test_producer_without_flags_still_returns_compiler(self):
        # This is the critical behavior change vs old _parse_producer_flags
        p = parse_producer("tiarmclang version 3.2.2.LTS")
        assert p.compiler == "tiarmclang"
        assert p.flags == []

    def test_unknown_compiler(self):
        p = parse_producer("Some Unknown Compiler 1.0")
        assert p.compiler is None


# ---------------------------------------------------------------------------
# Flag classification helpers
# ---------------------------------------------------------------------------

class TestFlagClassification:
    def test_is_include(self):
        assert is_include("-I/foo")
        assert is_include("-isystem")
        assert not is_include("-DFOO")

    def test_is_arch_flag(self):
        assert is_arch_flag("-mcpu=cortex-m4")
        assert is_arch_flag("-mthumb")
        assert is_arch_flag("-mfloat-abi=soft")
        assert not is_arch_flag("-O2")

    def test_is_define(self):
        assert is_define("-DFOO=1")
        assert is_define("-UBAR")
        assert not is_define("-I/x")

    def test_has_include(self):
        assert has_include(["-DFOO", "-I/x"])
        assert has_include(["-isystem", "/usr/include"])
        assert not has_include(["-DFOO", "-O2"])

    def test_strip_arch(self):
        result = strip_arch_flags(
            ["-I/x", "-mcpu=cortex-m4", "-DFOO", "-mthumb"],
        )
        assert "-mcpu=cortex-m4" not in result
        assert "-mthumb" not in result
        assert "-I/x" in result
        assert "-DFOO" in result

    def test_keep_includes_and_defines(self):
        result = keep_includes_and_defines(
            ["-I/x", "-DFOO", "-O2", "-mcpu=cortex-m4", "-isystem", "/usr"],
        )
        assert "-I/x" in result
        assert "-DFOO" in result
        assert "-O2" not in result
        assert "-isystem" in result
        assert "/usr" in result


# ---------------------------------------------------------------------------
# Response file expansion
# ---------------------------------------------------------------------------

class TestResponseFile:
    def test_expand_opt_file(self, tmp_path):
        opt = tmp_path / "flags.opt"
        opt.write_text("-DFOO=1\n-DBAR=2\n# comment\n-I/x\n")
        expanded, aug = fs_response.expand_response_files(
            ["@flags.opt"], tmp_path,
        )
        assert "-DFOO=1" in expanded
        assert "-DBAR=2" in expanded
        assert "-I/x" in expanded
        assert len(aug) == 1
        assert aug[0]["kind"] == "response_file_expand"

    def test_missing_response_file_warns(self, tmp_path):
        expanded, aug = fs_response.expand_response_files(
            ["@missing.opt"], tmp_path,
        )
        assert "@missing.opt" in expanded
        assert any(a["kind"] == "response_file_missing" for a in aug)

    def test_non_response_token_passes_through(self, tmp_path):
        expanded, aug = fs_response.expand_response_files(
            ["-I/x", "-DFOO"], tmp_path,
        )
        assert expanded == ["-I/x", "-DFOO"]
        assert aug == []


# ---------------------------------------------------------------------------
# compiler_match reconcile_arch
# ---------------------------------------------------------------------------

class TestReconcileArch:
    def test_same_family_same_cpu(self):
        r = fs_compiler_match.reconcile_arch(
            "tiarmclang", ["-mcpu=cortex-m0plus"], "armv6-m",
        )
        assert r.accept
        assert r.effective_target == "armv6-m"

    def test_cpu_downgrade_accepted_within_family(self):
        # session says armv7e-m, discovered is cortex-m0plus (armv6-m)
        r = fs_compiler_match.reconcile_arch(
            "tiarmclang", ["-mcpu=cortex-m0plus"], "armv7e-m",
        )
        assert r.accept
        assert r.effective_target == "armv6-m"
        assert r.cpu_override is not None
        assert r.cpu_override["discovered_target"] == "armv6-m"
        assert any("cpu_downgrade" in w for w in r.warnings)

    def test_cross_family_rejects(self):
        r = fs_compiler_match.reconcile_arch(
            "aarch64-linux-gnu-g++", ["-march=armv8-a"], "armv7e-m",
        )
        assert not r.accept

    def test_native_compiler_rejected(self):
        r = fs_compiler_match.reconcile_arch("g++", ["-O2"], "armv7e-m")
        assert not r.accept

    def test_no_cpu_accepted(self):
        r = fs_compiler_match.reconcile_arch(
            "arm-none-eabi-gcc", ["-O2"], "armv7e-m",
        )
        assert r.accept
        assert r.effective_target == "armv7e-m"


# ---------------------------------------------------------------------------
# build_root scoring
# ---------------------------------------------------------------------------

class TestBuildRoot:
    def test_picks_dir_with_makefile_and_obj(self, tmp_path):
        src = tmp_path / "src" / "foo.c"
        src.parent.mkdir()
        src.write_text("")
        # candidate A: just a ticlang subdir (no artifacts)
        a = tmp_path / "empty" / "ticlang"
        a.mkdir(parents=True)
        # candidate B: full build dir
        b = tmp_path / "build"
        b.mkdir()
        (b / "makefile").write_text("CC=gcc\nfoo.obj: foo.c\n\t$(CC) -c $< -o $@\n")
        (b / "bar.obj").write_bytes(b"\x7fELF" + b"\x00" * 32)

        picked = fs_build_root.find_build_root(src, tmp_path, {})
        # Should prefer b (more signals)
        assert picked is not None
        assert picked == b.resolve()

    def test_returns_none_when_no_signals(self, tmp_path, monkeypatch):
        # Limit candidate set to just this dir — ensures we don't pick up
        # stray Makefiles from sibling test dirs or %TEMP%.
        isolated = tmp_path / "isolated_empty"
        isolated.mkdir()
        src = isolated / "foo.c"
        src.write_text("")

        def _only_self(source, project_root, context):
            return [isolated.resolve()]

        monkeypatch.setattr(fs_build_root, "_iter_candidates", _only_self)
        picked = fs_build_root.find_build_root(src, isolated, {})
        assert picked is None


# ---------------------------------------------------------------------------
# projectspec_xml discovery
# ---------------------------------------------------------------------------

class TestProjectspec:
    def test_extracts_includes_and_defines_strips_arch(self, tmp_path):
        ps = tmp_path / "proj.projectspec"
        ps.write_text('<?xml version="1.0"?>\n'
                      '<projectSpec>\n'
                      '  <project toolChain="TICLANG" '
                      'compilerBuildOptions="-I/usr/include -DFOO=1 '
                      '-mcpu=cortex-m4 -mthumb -O2 -std=c99"/>\n'
                      '</projectSpec>\n')
        src = tmp_path / "bar.c"
        src.write_text("")
        result = fs_projectspec.discover(
            src, "armv7e-m", tmp_path, {}, None,
        )
        assert result is not None
        assert "-I/usr/include" in result.flags
        assert "-DFOO=1" in result.flags
        # Arch flags stripped
        assert "-mcpu=cortex-m4" not in result.flags
        assert "-mthumb" not in result.flags
        assert "-std=c99" not in result.flags
        assert result.partial is True
        assert result.confidence == "medium"


# ---------------------------------------------------------------------------
# gmake_dryrun parsing
# ---------------------------------------------------------------------------

class TestGmakeDryrunParse:
    def test_extract_compile_line(self):
        stdout = (
            "echo Building foo.obj\n"
            '"C:/ti/ticlang/bin/tiarmclang" -I../.. -I. -DFOO '
            "-std=gnu9x -mcpu=cortex-m0plus -mthumb "
            "-c foo.c -o foo.obj\n"
        )
        # The internal parser tokens:
        result = fs_gmake._extract_compile_line(stdout)
        assert result is not None
        compiler, tokens = result
        assert "tiarmclang" in compiler.lower()
        assert "-DFOO" in tokens
        assert "-I../.." in tokens or any(t == "-I../.." for t in tokens)

    def test_pick_donor_target_from_objects_var(self, tmp_path):
        mk = tmp_path / "makefile"
        mk.write_text(
            "OBJECTS = app_main.obj app_menu.obj $(SYSCFG)\n"
            "app_main.obj: app_main.c\n"
            "\t$(CC) -c $< -o $@\n"
        )
        donor = fs_gmake._pick_donor_target(mk)
        assert donor == "app_main.obj"

    def test_absolutize_include_paths(self, tmp_path):
        flags = ["-I../..", "-I.", "-IC:/abs/path", "-isystem", "include"]
        out = fs_gmake._absolutize_include_paths(flags, tmp_path)
        # Relative paths become absolute; absolute preserved
        assert any(f == "-IC:/abs/path" for f in out)
        # -I../.. became an absolute path
        has_rel = any(f == "-I../.." for f in out)
        assert not has_rel


# ---------------------------------------------------------------------------
# user_override
# ---------------------------------------------------------------------------

class TestUserOverride:
    def test_augment_mode_merges(self, tmp_path):
        (tmp_path / ".loci-build").mkdir()
        (tmp_path / ".loci-build" / "flags.json").write_text(json.dumps({
            "mode": "augment",
            "flags": ["-DADDED=1"],
        }))
        source = tmp_path / "foo.c"
        source.write_text("")

        base = DiscoveryResult(
            compiler="gcc", flags=["-I/x"],
            kind="compile_commands", confidence="exact",
        )
        new, applied = fs_override.apply_augment(base, source, tmp_path)
        assert applied
        assert "-DADDED=1" in new.flags
        assert "-I/x" in new.flags

    def test_replace_mode_emits_result(self, tmp_path):
        (tmp_path / ".loci-build").mkdir()
        (tmp_path / ".loci-build" / "flags.json").write_text(json.dumps({
            "mode": "replace",
            "compiler": "arm-none-eabi-gcc",
            "flags": ["-g", "-O2", "-mcpu=cortex-m0plus", "-mthumb", "-c"],
        }))
        source = tmp_path / "foo.c"
        source.write_text("")

        result = fs_override.discover(
            source, "armv6-m", tmp_path, {}, None,
        )
        assert result is not None
        assert result.compiler == "arm-none-eabi-gcc"
        assert result.kind == "user-override-replace"
        assert "-mcpu=cortex-m0plus" in result.flags

    def test_per_source_glob_match(self, tmp_path):
        (tmp_path / ".loci-build").mkdir()
        (tmp_path / ".loci-build" / "flags.json").write_text(json.dumps({
            "mode": "augment",
            "per_source": {
                "**/foo.c": {"mode": "augment", "flags": ["-DHCI_TL_NONE"]},
            },
        }))
        source = tmp_path / "sub" / "foo.c"
        source.parent.mkdir()
        source.write_text("")

        ov = fs_override.load_override(tmp_path)
        matched = ov.per_source_for(source, tmp_path)
        assert matched is not None
        assert "-DHCI_TL_NONE" in matched["flags"]

    def test_env_var_extras_are_appended(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCI_EXTRA_CFLAGS", "-DENV_EXTRA=1 -Ienv_path")
        ov = fs_override.load_override(tmp_path)
        assert "-DENV_EXTRA=1" in ov.extra_cflags
        assert "-Ienv_path" in ov.extra_cflags

    def test_expand_vars_resolves_variable_referencing_project_root(self, tmp_path):
        # Variable value contains ${PROJECT_ROOT} — single-pass substitution
        # used to leave the placeholder in the output. Iterative expansion fixes it.
        out = fs_override._expand_vars(
            "-I${SDK_SRC}/ti", tmp_path,
            {"SDK_SRC": "${PROJECT_ROOT}/sdk/src"},
        )
        assert out == f"-I{tmp_path}/sdk/src/ti"

    def test_expand_vars_resolves_home_in_variable_value(self, tmp_path):
        # ${HOME} was previously not handled at all.
        out = fs_override._expand_vars(
            "-I${FREERTOS_DIR}/include", tmp_path,
            {"FREERTOS_DIR": "${HOME}/FreeRTOS"},
        )
        home = os.path.expanduser("~")
        assert out == f"-I{home}/FreeRTOS/include"

    def test_expand_vars_handles_cyclic_definitions(self, tmp_path):
        # A=${B}, B=${A} must not loop forever; cap exits with placeholder
        # left in place (downstream compile error is acceptable).
        out = fs_override._expand_vars(
            "-D${A}", tmp_path,
            {"A": "${B}", "B": "${A}"},
        )
        assert isinstance(out, str)
        assert out.startswith("-D")

    def test_expand_vars_well_known_wins_over_user_collision(self, tmp_path):
        # User-supplied PROJECT_ROOT must not shadow the real one.
        out = fs_override._expand_vars(
            "-I${PROJECT_ROOT}/x", tmp_path,
            {"PROJECT_ROOT": "/should/not/win"},
        )
        assert out == f"-I{tmp_path}/x"


# ---------------------------------------------------------------------------
# stdlib_headers
# ---------------------------------------------------------------------------

class TestStdlibHeaders:
    def test_c_stdlib(self):
        assert fs_stdlib.is_stdlib("stdio.h")
        assert fs_stdlib.is_stdlib("stdint.h")
        assert fs_stdlib.is_stdlib("string.h")

    def test_cxx_stdlib(self):
        assert fs_stdlib.is_stdlib("vector")
        assert fs_stdlib.is_stdlib("algorithm")

    def test_non_stdlib(self):
        assert not fs_stdlib.is_stdlib("my_header.h")
        assert not fs_stdlib.is_stdlib("project/foo.h")

    def test_generated_header_pattern(self):
        assert fs_stdlib.is_generated("ti_drivers_config.h")
        assert fs_stdlib.is_generated("FreeRTOSConfig.h")
        assert not fs_stdlib.is_generated("user.h")


# ---------------------------------------------------------------------------
# Meta v1/v2 compatibility
# ---------------------------------------------------------------------------

class TestMetaCompat:
    def test_diff_v1_meta_still_works(self, tmp_path):
        from build_metadata import diff_metas

        v1_prev = {
            "compiler": "gcc", "compiler_version": "10",
            "loci_target": "aarch64", "architecture": "aarch64",
            "flags": ["-g", "-O2", "-c"],
            "flag_source": "defaults",
        }
        v1_curr = dict(v1_prev)  # identical
        assert diff_metas(v1_prev, v1_curr) == []

    def test_diff_flag_source_v2_kind_regression(self, tmp_path):
        from build_metadata import diff_metas

        v2_prev = {
            "compiler": "tiarmclang", "compiler_version": "3.2",
            "loci_target": "armv6-m", "architecture": "armv6-m",
            "flags": ["-g", "-c"],
            "flag_source": "gmake-dry-run",
            "flag_source_v2": {"kind": "gmake-dry-run", "confidence": "exact"},
        }
        v2_curr = {
            "compiler": "tiarmclang", "compiler_version": "3.2",
            "loci_target": "armv6-m", "architecture": "armv6-m",
            "flags": ["-g", "-c"],
            "flag_source": "defaults",
            "flag_source_v2": {"kind": "defaults", "confidence": "low"},
        }
        lines = diff_metas(v2_prev, v2_curr)
        assert any("flag_source" in l and "regressed" in l for l in lines)

    def test_v1_preserves_flags_key(self):
        from build_metadata import _flags_diff
        removed, added = _flags_diff(["-O2"], ["-O3"])
        assert removed == ["-O2"]
        assert added == ["-O3"]
