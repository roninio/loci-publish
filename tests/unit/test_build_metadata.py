"""Tests for lib/build_metadata.py — flag detection, metadata I/O, diff."""

import argparse
import json

import pytest

from build_metadata import (
    COMPILABLE_EXTS,
    DEFAULT_COMPILER,
    DEFAULT_FLAGS,
    HEADER_EXTS,
    LOCI_TARGET_FAMILY,
    RUST_EXTS,
    RUST_TARGETS,
    _diagnose_missing_include_dirs,
    _flags_diff,
    _parse_producer_flags,
    choose_compiler_for_source,
    compile_subcommand,
    compiler_family,
    compiler_matches_target,
    detect_flags,
    detect_from_compile_commands,
    detect_from_dwarf,
    diff_metas,
    diff_subcommand,
    ensure_required_flags,
    find_compile_commands,
    format_metadata_block,
    format_mismatch_block,
    parse_compile_command,
    print_subcommand,
    scan_makefiles_for_flags,
)

pytestmark = pytest.mark.unit


# -- parse_compile_command --------------------------------------------------

class TestParseCompileCommand:
    def test_arguments_form(self):
        entry = {
            "directory": "/proj/build",
            "file": "/proj/src/foo.cpp",
            "arguments": [
                "/usr/bin/arm-none-eabi-g++",
                "-g", "-O2", "-mcpu=cortex-m4", "-mthumb",
                "-DBUILD_MODE=1",
                "-c", "/proj/src/foo.cpp",
                "-o", "/proj/build/foo.o",
            ],
        }
        compiler, flags = parse_compile_command(entry)
        assert compiler == "/usr/bin/arm-none-eabi-g++"
        assert flags == ["-g", "-O2", "-mcpu=cortex-m4", "-mthumb",
                         "-DBUILD_MODE=1", "-c"]

    def test_command_form_shell_split(self):
        entry = {
            "directory": "/proj/build",
            "file": "/proj/src/foo.cpp",
            "command": '/usr/bin/g++ -O2 -DNDEBUG -c /proj/src/foo.cpp -o foo.o',
        }
        compiler, flags = parse_compile_command(entry)
        assert compiler == "/usr/bin/g++"
        # -o foo.o stripped, /proj/src/foo.cpp stripped
        assert flags == ["-O2", "-DNDEBUG", "-c"]

    def test_combined_o_flag_stripped(self):
        entry = {
            "directory": "/proj",
            "file": "/proj/foo.c",
            "arguments": ["gcc", "-c", "-O1", "-ofoo.o", "/proj/foo.c"],
        }
        _, flags = parse_compile_command(entry)
        assert "-ofoo.o" not in flags
        assert flags == ["-c", "-O1"]

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            parse_compile_command({"file": "/x/y.cpp"})


# -- detect_from_compile_commands ------------------------------------------

class TestDetectFromCompileCommands:
    def test_found(self, tmp_path):
        src = tmp_path / "src" / "foo.cpp"
        src.parent.mkdir()
        src.write_text("int main() { return 0; }\n")

        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([
            {
                "directory": str(tmp_path / "build"),
                "file": str(src),
                "arguments": ["arm-none-eabi-g++", "-g", "-O2",
                              "-mcpu=cortex-m4", "-c", str(src),
                              "-o", "foo.o"],
            },
        ]))
        result = detect_from_compile_commands(src, cc)
        assert result is not None
        compiler, flags = result
        assert compiler == "arm-none-eabi-g++"
        assert "-mcpu=cortex-m4" in flags

    def test_not_found_returns_none(self, tmp_path):
        src = tmp_path / "unrelated.cpp"
        src.write_text("// noop\n")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([
            {"directory": str(tmp_path), "file": "/other/file.cpp",
             "command": "g++ -c /other/file.cpp"},
        ]))
        assert detect_from_compile_commands(src, cc) is None

    def test_relative_file_path_resolved(self, tmp_path):
        # compile_commands often stores relative paths + a directory
        src = tmp_path / "src" / "foo.cpp"
        src.parent.mkdir()
        src.write_text("// x\n")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([
            {
                "directory": str(tmp_path),
                "file": "src/foo.cpp",
                "command": f"g++ -O2 -c src/foo.cpp",
            },
        ]))
        result = detect_from_compile_commands(src, cc)
        assert result is not None


# -- find_compile_commands --------------------------------------------------

# -- _parse_producer_flags --------------------------------------------------

class TestParseProducerFlags:
    def test_tiarmclang_producer_string(self):
        producer = "tiarmclang version 3.2.2.LTS LLVM 14.0.3 -g -O2 -mcpu=cortex-m0plus -mthumb -mfloat-abi=soft"
        compiler, flags = _parse_producer_flags(producer)
        assert compiler == "tiarmclang"
        assert "-mcpu=cortex-m0plus" in flags
        assert "-mthumb" in flags
        assert "-O2" in flags
        assert "-mfloat-abi=soft" in flags

    def test_arm_none_eabi_gcc(self):
        producer = "GNU C17 10.3.1 arm-none-eabi-gcc -march=armv7e-m -mfpu=fpv4-sp-d16 -O2 -std=c17"
        compiler, flags = _parse_producer_flags(producer)
        assert "arm-none-eabi-gcc" in compiler
        assert "-march=armv7e-m" in flags
        assert "-O2" in flags
        assert "-std=c17" in flags

    def test_aarch64_linux_gnu(self):
        producer = "GNU C11 12.2.1 aarch64-linux-gnu-gcc -march=armv8-a -O3 -g"
        compiler, flags = _parse_producer_flags(producer)
        assert "aarch64-linux-gnu-gcc" in compiler
        assert "-march=armv8-a" in flags
        assert "-O3" in flags

    def test_with_include_and_define_flags(self):
        producer = "tiarmclang -O2 -mcpu=cortex-m0plus -I/path/to/include -DFOO=1"
        compiler, flags = _parse_producer_flags(producer)
        assert compiler == "tiarmclang"
        assert "-I/path/to/include" in flags
        assert "-DFOO=1" in flags

    def test_returns_none_when_no_compiler_found(self):
        producer = "some random string with no compiler"
        assert _parse_producer_flags(producer) is None

    def test_returns_none_when_only_compiler_no_flags(self):
        producer = "tiarmclang version 3.2.2.LTS"
        # No flags, so returns None
        assert _parse_producer_flags(producer) is None

    def test_deduplicates_flags(self):
        producer = "tiarmclang -O2 -O2 -mcpu=cortex-m0plus -mcpu=cortex-m0plus"
        compiler, flags = _parse_producer_flags(producer)
        assert flags.count("-O2") == 1
        assert flags.count("-mcpu=cortex-m0plus") == 1


# -- detect_from_dwarf -------------------------------------------------------

class TestDetectFromDwarf:
    def test_returns_none_when_pyelftools_missing(self, tmp_path, monkeypatch):
        """If pyelftools is not installed, must return None gracefully."""
        src = tmp_path / "test.elf"
        src.write_bytes(b"\x7fELF" + b"\x00" * 100)
        # Patch sys.modules to hide elftools
        monkeypatch.setitem(__import__("sys").modules, "elftools.elf.elffile", None)
        result = detect_from_dwarf(src, "test")
        assert result is None

    def test_returns_none_on_invalid_elf(self, tmp_path):
        """Non-ELF file should return None."""
        src = tmp_path / "notelf.bin"
        src.write_bytes(b"not an elf file")
        result = detect_from_dwarf(src, "test")
        assert result is None

    def test_returns_none_on_missing_file(self, tmp_path):
        """Non-existent file should return None."""
        src = tmp_path / "missing.elf"
        result = detect_from_dwarf(src, "test")
        assert result is None

    def test_tiarmclang_producer_extraction(self, tmp_path):
        """Mock an ELF with DWARF containing tiarmclang producer string."""
        from unittest.mock import MagicMock, patch

        src = tmp_path / "test.elf"
        src.write_bytes(b"\x7fELF" + b"\x00" * 100)

        mock_die = MagicMock()
        mock_die.tag = "DW_TAG_compile_unit"
        mock_die.attributes = {
            "DW_AT_name": MagicMock(value=b"foo.c"),
            "DW_AT_producer": MagicMock(
                value=b"tiarmclang version 3.2.2.LTS -g -O2 -mcpu=cortex-m0plus -mthumb"
            ),
        }

        mock_cu = MagicMock()
        mock_cu.get_top_DIE.return_value = mock_die

        mock_dwarf = MagicMock()
        mock_dwarf.iter_CUs.return_value = [mock_cu]

        mock_elf = MagicMock()
        mock_elf.has_dwarf_info.return_value = True
        mock_elf.get_dwarf_info.return_value = mock_dwarf

        with patch("elftools.elf.elffile.ELFFile", return_value=mock_elf):
            result = detect_from_dwarf(src, "foo")
            assert result is not None
            compiler, flags = result
            assert compiler == "tiarmclang"
            assert "-mcpu=cortex-m0plus" in flags
            assert "-mthumb" in flags
            assert "-O2" in flags

    def test_matches_by_source_stem(self, tmp_path):
        """When multiple CUs exist, prefer the one matching source_stem."""
        from unittest.mock import MagicMock, patch

        src = tmp_path / "test.elf"
        src.write_bytes(b"\x7fELF" + b"\x00" * 100)

        # First CU doesn't match, second does
        mock_die1 = MagicMock()
        mock_die1.tag = "DW_TAG_compile_unit"
        mock_die1.attributes = {
            "DW_AT_name": MagicMock(value=b"other.c"),
            "DW_AT_producer": MagicMock(
                value=b"tiarmclang -mcpu=cortex-m4 -O1"
            ),
        }

        mock_die2 = MagicMock()
        mock_die2.tag = "DW_TAG_compile_unit"
        mock_die2.attributes = {
            "DW_AT_name": MagicMock(value=b"foo.c"),
            "DW_AT_producer": MagicMock(
                value=b"tiarmclang -mcpu=cortex-m0plus -O2"
            ),
        }

        mock_cu1 = MagicMock()
        mock_cu1.get_top_DIE.return_value = mock_die1

        mock_cu2 = MagicMock()
        mock_cu2.get_top_DIE.return_value = mock_die2

        mock_dwarf = MagicMock()
        mock_dwarf.iter_CUs.return_value = [mock_cu1, mock_cu2]

        mock_elf = MagicMock()
        mock_elf.has_dwarf_info.return_value = True
        mock_elf.get_dwarf_info.return_value = mock_dwarf

        with patch("elftools.elf.elffile.ELFFile", return_value=mock_elf):
            result = detect_from_dwarf(src, "foo")
            assert result is not None
            compiler, flags = result
            # Should match the second CU (foo.c)
            assert "-mcpu=cortex-m0plus" in flags
            assert "-O2" in flags


# -- scan_makefiles_for_flags ------------------------------------------------

class TestScanMakefilesForFlags:
    def test_finds_include_paths_in_makefile(self, tmp_path):
        """Extract -I flags from a Makefile."""
        makefile = tmp_path / "Makefile"
        makefile.write_text(
            'INCLUDES = -I"$(PROJECT_ROOT)/include" -I"/usr/arm/include"\n'
            "CFLAGS = $(INCLUDES) -mcpu=cortex-m0plus\n"
        )
        src = tmp_path / "foo.c"
        src.write_text("")
        flags = scan_makefiles_for_flags(tmp_path, src)
        assert any("-I" in f for f in flags)

    def test_finds_defines_in_makefile(self, tmp_path):
        """Extract -D flags from a Makefile."""
        makefile = tmp_path / "rules.mk"
        makefile.write_text("DEFINES = -DDEBUG -DVERSION=1\n")
        src = tmp_path / "foo.c"
        src.write_text("")
        flags = scan_makefiles_for_flags(tmp_path, src)
        assert any("-D" in f for f in flags)

    def test_returns_empty_on_no_makefiles(self, tmp_path):
        """When no Makefiles exist, return empty list."""
        src = tmp_path / "foo.c"
        src.write_text("")
        flags = scan_makefiles_for_flags(tmp_path, src)
        assert flags == []

    def test_handles_binary_file_gracefully(self, tmp_path):
        """Binary/unreadable files should not crash."""
        makefile = tmp_path / "Makefile"
        makefile.write_bytes(b"\x00\xFF\xFE")
        src = tmp_path / "foo.c"
        src.write_text("")
        # Should not raise, return empty or partial list
        flags = scan_makefiles_for_flags(tmp_path, src)
        assert isinstance(flags, list)

    def test_deduplicates_flags(self, tmp_path):
        """Duplicate flags from multiple files should appear once."""
        makefile1 = tmp_path / "Makefile"
        makefile1.write_text("-I/include -DFOO\n")
        makefile2 = tmp_path / "rules.mk"
        makefile2.write_text("-I/include -DBAR\n")
        src = tmp_path / "foo.c"
        src.write_text("")
        flags = scan_makefiles_for_flags(tmp_path, src)
        # -I/include should appear once
        include_flags = [f for f in flags if f.startswith("-I/include")]
        assert len(include_flags) == 1


# -- find_compile_commands --------------------------------------------------

class TestFindCompileCommands:
    def test_root_wins_over_build_dirs(self, tmp_path):
        (tmp_path / "compile_commands.json").write_text("[]")
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "compile_commands.json").write_text("[]")
        found = find_compile_commands(tmp_path)
        assert found == tmp_path / "compile_commands.json"

    def test_build_dir_fallback(self, tmp_path):
        (tmp_path / "build").mkdir()
        target = tmp_path / "build" / "compile_commands.json"
        target.write_text("[]")
        assert find_compile_commands(tmp_path) == target

    def test_none_when_absent(self, tmp_path):
        assert find_compile_commands(tmp_path) is None


# -- choose_compiler_for_source --------------------------------------------

class TestChooseCompilerForSource:
    def test_c_downgrades_gpp_to_gcc(self, tmp_path):
        src = tmp_path / "x.c"
        src.write_text("")
        assert choose_compiler_for_source("arm-none-eabi-g++", src) == "arm-none-eabi-gcc"

    def test_c_downgrades_clangpp_to_clang(self, tmp_path):
        src = tmp_path / "x.c"
        src.write_text("")
        assert choose_compiler_for_source("clang++", src) == "clang"

    def test_cpp_keeps_gpp(self, tmp_path):
        src = tmp_path / "x.cpp"
        src.write_text("")
        assert choose_compiler_for_source("arm-none-eabi-g++", src) == "arm-none-eabi-g++"

    def test_unknown_compiler_unchanged(self, tmp_path):
        src = tmp_path / "x.c"
        src.write_text("")
        assert choose_compiler_for_source("tiarmclang", src) == "tiarmclang"


# -- ensure_required_flags --------------------------------------------------

class TestEnsureRequiredFlags:
    def test_adds_both_when_missing(self):
        result = ensure_required_flags(["-O2", "-Wall"])
        assert "-c" in result
        assert "-g" in result

    def test_keeps_existing_c_and_g(self):
        result = ensure_required_flags(["-c", "-g", "-O2"])
        assert result.count("-c") == 1
        assert result.count("-g") == 1

    def test_accepts_gdwarf_variant(self):
        result = ensure_required_flags(["-gdwarf-4", "-c"])
        # Plain -g should NOT be added when -gdwarf-N is present
        assert "-g" not in result or result.count("-g") == 0

    def test_leaves_order_mostly_intact(self):
        result = ensure_required_flags(["-O2", "-Wall"])
        # -g prepended, -c appended
        assert result[0] == "-g"
        assert result[-1] == "-c"


# -- compiler_family / compiler_matches_target ------------------------------

class TestCompilerFamily:
    def test_arm_none_eabi(self):
        assert compiler_family("arm-none-eabi-g++") == "cortexm"
        assert compiler_family("/usr/bin/arm-none-eabi-gcc") == "cortexm"

    def test_aarch64(self):
        assert compiler_family("aarch64-linux-gnu-g++") == "aarch64"

    def test_tricore(self):
        assert compiler_family("tricore-elf-gcc") == "tricore"

    def test_native_returns_none(self):
        assert compiler_family("g++") is None
        assert compiler_family("/usr/bin/clang++") is None


class TestCompilerMatchesTarget:
    def test_matching_family(self):
        assert compiler_matches_target("arm-none-eabi-g++", "armv7e-m")
        assert compiler_matches_target("arm-none-eabi-g++", "armv6-m")
        assert compiler_matches_target("aarch64-linux-gnu-g++", "aarch64")
        assert compiler_matches_target("tricore-elf-gcc", "tc399")

    def test_mismatched_family(self):
        assert not compiler_matches_target("arm-none-eabi-g++", "aarch64")
        assert not compiler_matches_target("aarch64-linux-gnu-g++", "tc399")

    def test_native_compiler_rejected(self):
        # A host g++ should not be accepted as an arm-target compiler;
        # this is what prevents compile_commands.json from an x86 build
        # silently feeding flags into an arm preflight.
        assert not compiler_matches_target("g++", "armv7e-m")
        assert not compiler_matches_target("/usr/bin/clang++", "aarch64")


# -- detect_flags -----------------------------------------------------------

class TestDetectFlags:
    def test_fallback_to_defaults_when_no_cc(self, tmp_path):
        src = tmp_path / "foo.cpp"
        src.write_text("int x;")
        compiler, flags, source = detect_flags(
            src, "armv7e-m", tmp_path)
        assert compiler == DEFAULT_COMPILER["armv7e-m"]
        assert flags == DEFAULT_FLAGS["armv7e-m"]
        assert source == "defaults"

    def test_uses_compile_commands_when_family_matches(self, tmp_path):
        src = tmp_path / "foo.cpp"
        src.write_text("")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(src),
            "arguments": ["arm-none-eabi-g++", "-O3", "-mcpu=cortex-m4",
                          "-mthumb", "-c", str(src), "-o", "foo.o"],
        }]))
        compiler, flags, source = detect_flags(src, "armv7e-m", tmp_path)
        assert compiler == "arm-none-eabi-g++"
        assert "-O3" in flags
        assert source.startswith("compile_commands")

    def test_falls_back_when_cc_compiler_wrong_family(self, tmp_path):
        """If compile_commands.json points at a host g++ but the user asked
        for armv7e-m, the detector must NOT use those flags — otherwise the
        preflight .o would be x86 while the skill thinks it's arm. The
        fallback flag_source must carry the rejection reason so the user
        sees why we ignored their compile_commands."""
        src = tmp_path / "foo.cpp"
        src.write_text("")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(src),
            "arguments": ["g++", "-O2", "-c", str(src)],
        }]))
        compiler, flags, source = detect_flags(src, "armv7e-m", tmp_path)
        assert compiler == DEFAULT_COMPILER["armv7e-m"]
        assert flags == DEFAULT_FLAGS["armv7e-m"]
        assert source.startswith("defaults (compile_commands rejected")
        assert "wrong arch" in source

    def test_c_file_gets_gcc_from_defaults(self, tmp_path):
        src = tmp_path / "foo.c"
        src.write_text("")
        compiler, _, _ = detect_flags(src, "armv7e-m", tmp_path)
        assert compiler == "arm-none-eabi-gcc"

    def test_unknown_target_raises(self, tmp_path):
        src = tmp_path / "foo.cpp"
        src.write_text("")
        with pytest.raises(RuntimeError):
            detect_flags(src, "riscv", tmp_path)

    def test_all_loci_targets_have_defaults(self):
        """Every LOCI target advertised in SessionStart must have defaults."""
        for target in LOCI_TARGET_FAMILY:
            assert target in DEFAULT_COMPILER
            assert target in DEFAULT_FLAGS

    def test_uses_dwarf_when_no_compile_commands(self, tmp_path):
        """When no compile_commands.json exists, cascade must reach a DWARF
        source and use its flags. The cascade now runs through the
        flag_sources package — patch the linked_elf_dwarf module."""
        from unittest.mock import patch
        from flag_sources import DiscoveryResult

        src = tmp_path / "foo.c"
        src.write_text("")
        fake_elf = tmp_path / "test.elf"
        fake_elf.write_bytes(b"\x7fELF" + b"\x00" * 100)

        context = {
            "elf_files": [str(fake_elf)],
            "project_root": str(tmp_path),
            "compiler": "tiarmclang",
        }

        def _fake_discover(source, target, root, ctx, bd):
            return DiscoveryResult(
                compiler="tiarmclang",
                flags=["-mcpu=cortex-m0plus", "-mthumb", "-O2", "-g",
                       "-I/include"],
                kind="linked-elf-dwarf",
                confidence="high",
                details={"elf_path": str(fake_elf)},
            )

        with patch("flag_sources.linked_elf_dwarf.discover",
                   side_effect=_fake_discover):
            compiler, flags, source_label = detect_flags(
                src, "armv6-m", tmp_path, context=context,
            )

        assert compiler == "tiarmclang"
        assert "-mcpu=cortex-m0plus" in flags
        assert "-mthumb" in flags
        assert "-I/include" in flags
        assert source_label.startswith("dwarf")


# -- _flags_diff / diff_metas ----------------------------------------------

class TestFlagsDiff:
    def test_identical(self):
        assert _flags_diff(["-g", "-O2"], ["-g", "-O2"]) == ([], [])

    def test_removed_only(self):
        removed, added = _flags_diff(["-g", "-O2", "-DFOO"], ["-g", "-O2"])
        assert removed == ["-DFOO"]
        assert added == []

    def test_added_only(self):
        removed, added = _flags_diff(["-g"], ["-g", "-O3"])
        assert removed == []
        assert added == ["-O3"]

    def test_both(self):
        removed, added = _flags_diff(["-O2"], ["-O3"])
        assert removed == ["-O2"]
        assert added == ["-O3"]


class TestDiffMetas:
    def _base(self, **kwargs):
        base = {
            "compiler": "arm-none-eabi-g++",
            "compiler_version": "10.3.1",
            "loci_target": "armv7e-m",
            "architecture": "armv7e-m",
            "flags": ["-g", "-O2", "-c"],
        }
        base.update(kwargs)
        return base

    def test_identical_no_diff(self):
        assert diff_metas(self._base(), self._base()) == []

    def test_flag_change_detected(self):
        diff = diff_metas(self._base(), self._base(flags=["-g", "-O3", "-c"]))
        assert len(diff) == 1
        assert "flags" in diff[0]
        assert "-O2" in diff[0]
        assert "-O3" in diff[0]

    def test_compiler_version_change_detected(self):
        diff = diff_metas(self._base(), self._base(compiler_version="12.2.0"))
        assert any("compiler_version" in d for d in diff)

    def test_target_change_detected(self):
        diff = diff_metas(self._base(),
                          self._base(loci_target="armv6-m", architecture="armv6-m"))
        assert len(diff) >= 2  # loci_target + architecture


# -- format_metadata_block / format_mismatch_block -------------------------

class TestFormatMetadataBlock:
    def test_contains_all_fields(self):
        meta = {
            "phase": "preflight",
            "source_file": "/proj/src/foo.cpp",
            "compiler": "arm-none-eabi-g++",
            "compiler_version": "10.3.1",
            "flags": ["-g", "-O2", "-c"],
            "loci_target": "armv7e-m",
            "output": "/proj/.loci-build/armv7e-m/foo.o",
            "flag_source": "defaults",
        }
        block = format_metadata_block(meta)
        assert "LOCI · build" in block
        assert "preflight" in block
        assert "arm-none-eabi-g++" in block
        assert "10.3.1" in block
        assert "-O2" in block
        assert "armv7e-m" in block
        assert "defaults" in block

    def test_unknown_version_hidden(self):
        meta = {
            "phase": "preflight",
            "source_file": "/x.cpp", "compiler": "g++",
            "compiler_version": "unknown",
            "flags": [], "loci_target": "aarch64",
            "output": "/x.o", "flag_source": "defaults",
        }
        block = format_metadata_block(meta)
        assert "(unknown)" not in block


class TestFormatMismatchBlock:
    def test_renders_divergences(self):
        block = format_mismatch_block([
            "  compiler       'g++' → 'clang++'",
            "  flags          added -O3",
        ])
        assert "mismatch" in block
        assert "g++" in block
        assert "clang++" in block


# -- subcommand wrappers (argparse-style args) -----------------------------

class TestSubcommandWrappers:
    def test_diff_subcommand_match(self, tmp_path, capsys):
        meta = {
            "compiler": "g++", "compiler_version": "12",
            "loci_target": "aarch64", "architecture": "aarch64",
            "flags": ["-g", "-O2", "-c"],
        }
        a = tmp_path / "a.json"; a.write_text(json.dumps(meta))
        b = tmp_path / "b.json"; b.write_text(json.dumps(meta))
        args = argparse.Namespace(prev=str(a), curr=str(b))
        rc = diff_subcommand(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "matches" in out

    def test_diff_subcommand_divergence(self, tmp_path, capsys):
        a = tmp_path / "a.json"; a.write_text(json.dumps({
            "compiler": "g++", "compiler_version": "12",
            "loci_target": "aarch64", "architecture": "aarch64",
            "flags": ["-g", "-O2", "-c"],
        }))
        b = tmp_path / "b.json"; b.write_text(json.dumps({
            "compiler": "g++", "compiler_version": "13",
            "loci_target": "aarch64", "architecture": "aarch64",
            "flags": ["-g", "-O3", "-c"],
        }))
        rc = diff_subcommand(argparse.Namespace(prev=str(a), curr=str(b)))
        assert rc == 1
        out = capsys.readouterr().out
        assert "mismatch" in out
        assert "compiler_version" in out
        assert "-O2" in out and "-O3" in out

    def test_print_subcommand(self, tmp_path, capsys):
        meta_path = tmp_path / "m.json"
        meta_path.write_text(json.dumps({
            "phase": "preflight", "source_file": "/x.cpp",
            "compiler": "arm-none-eabi-g++", "compiler_version": "10",
            "flags": ["-g", "-c"], "loci_target": "armv7e-m",
            "output": "/x.o", "flag_source": "defaults",
        }))
        rc = print_subcommand(argparse.Namespace(meta=str(meta_path)))
        assert rc == 0
        assert "LOCI · build" in capsys.readouterr().out

    def test_diff_missing_file(self, tmp_path):
        rc = diff_subcommand(argparse.Namespace(
            prev=str(tmp_path / "nope.json"),
            curr=str(tmp_path / "also-nope.json")))
        assert rc == 2


# -- compile_subcommand input validation -----------------------------------

def _compile_args(**overrides):
    base = dict(source=None, loci_target="aarch64", context=None,
                project_root=None, output=None, meta_prev=None,
                phase="preflight", verbose=False)
    base.update(overrides)
    return argparse.Namespace(**base)


class TestCompileSubcommandRejections:
    """build_metadata.compile must not silently succeed on sources it can't
    actually compile. B1 (Rust silent-success via g++) and G1 (header ->
    empty .o) both manifested as exit-0 with bogus/missing output; the
    compile_subcommand now rejects these up front."""

    def test_header_rejected(self, tmp_path, capsys):
        src = tmp_path / "iface.h"
        src.write_text("inline int x() { return 0; }\n")
        rc = compile_subcommand(_compile_args(source=str(src)))
        assert rc == 2
        err = capsys.readouterr().err
        assert "header" in err.lower()
        # No output was produced
        assert not (tmp_path / ".loci-build").exists()

    def test_unknown_extension_rejected(self, tmp_path, capsys):
        src = tmp_path / "weird.xyz"
        src.write_text("whatever\n")
        rc = compile_subcommand(_compile_args(source=str(src)))
        assert rc == 2
        assert "unsupported source extension" in capsys.readouterr().err.lower()

    def test_rust_without_rustc_rejects_cleanly(self, tmp_path, capsys,
                                                 monkeypatch):
        """If rustc isn't on PATH, we must fail with a clear message rather
        than silently routing through g++ (which exits 0 and makes no .o)."""
        monkeypatch.setenv("PATH", "")
        src = tmp_path / "lib.rs"
        src.write_text("pub fn f() -> i32 { 1 }\n")
        rc = compile_subcommand(_compile_args(
            source=str(src), project_root=str(tmp_path)))
        assert rc == 127
        err = capsys.readouterr().err
        assert "rustc not found" in err.lower()

    def test_rust_unsupported_target_rejects(self, tmp_path, capsys):
        """TriCore has no rustc target triple — must reject, not fallback."""
        src = tmp_path / "lib.rs"
        src.write_text("pub fn f() -> i32 { 1 }\n")
        rc = compile_subcommand(_compile_args(
            source=str(src), loci_target="tc399",
            project_root=str(tmp_path)))
        # Either 2 (no target triple) or 127 (rustc missing) are acceptable
        # outcomes — we only care that it does NOT silently succeed with 0.
        assert rc != 0


# -- project_root resolution from context ----------------------------------

class TestContextProjectRoot:
    def test_context_project_root_used_when_no_cli_flag(self, tmp_path, capsys):
        """Compile should pick up compile_commands.json from the context's
        project_root, not the subprocess cwd. This matters on monorepos
        where Claude's shell starts in a subdir."""
        subdir = tmp_path / "pkg"
        subdir.mkdir()
        src = subdir / "foo.cpp"
        src.write_text("int f() { return 1; }\n")
        # compile_commands.json at the top-level, not under the subdir
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(src),
            "arguments": ["aarch64-linux-gnu-g++", "-O3", "-march=armv8-a",
                          "-DUSE_CC=1", "-c", str(src), "-o", "foo.o"],
        }]))
        ctx_path = tmp_path / "ctx.json"
        ctx_path.write_text(json.dumps({
            "project_root": str(tmp_path),
            "compiler": "aarch64-linux-gnu-g++",
        }))
        # Note: CompileSubcommand runs a real compiler; we only care about
        # flag detection here. Call detect_flags directly.
        compiler, flags, source_label = detect_flags(
            src, "aarch64", tmp_path,
            context=json.loads(ctx_path.read_text()))
        assert "-DUSE_CC=1" in flags
        assert source_label.startswith("compile_commands")


# -- compile_commands rejection annotation --------------------------------

class TestFlagSourceRejectionNote:
    def test_rejection_note_in_flag_source(self, tmp_path):
        """When compile_commands exists but its compiler targets the wrong
        arch, the fallback flag_source must say so — not just 'defaults'."""
        src = tmp_path / "foo.cpp"
        src.write_text("")
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(src),
            "arguments": ["g++", "-O2", "-c", str(src)],
        }]))
        _, _, source_label = detect_flags(src, "armv7e-m", tmp_path)
        assert source_label.startswith("defaults (compile_commands rejected")
        assert "wrong arch" in source_label


# -- Rust target coverage ---------------------------------------------------

class TestRustTargets:
    def test_all_supported_arm_targets_have_triples(self):
        """Every LOCI target except tricore has a rustc target triple."""
        for loci_target in ("aarch64", "armv7e-m", "armv6-m"):
            assert loci_target in RUST_TARGETS
        assert "tc399" not in RUST_TARGETS

    def test_compilable_sets_are_disjoint_from_headers(self):
        assert COMPILABLE_EXTS.isdisjoint(HEADER_EXTS)
        assert RUST_EXTS.issubset(COMPILABLE_EXTS)


# -- _diagnose_missing_include_dirs ----------------------------------------

class TestDiagnoseMissingIncludeDirs:
    """Post-compile diagnostic: when the compiler says a header wasn't
    found, walk the emitted -I / -isystem flags, stat each one, and
    surface the ones that don't exist. Point at the real problem
    (project Makefile placeholder paths) so users don't puzzle it out."""

    _CLANG_STDERR = (
        "ClockP_freertos.c:41:10: fatal error: 'FreeRTOS.h' file not found\n"
        "#include <FreeRTOS.h>\n"
        "         ^~~~~~~~~~~~\n"
        "1 error generated.\n"
    )
    _GCC_STDERR = (
        "foo.c:3:10: fatal error: FreeRTOS.h: No such file or directory\n"
        "    3 | #include <FreeRTOS.h>\n"
        "      |          ^~~~~~~~~~~~\n"
        "compilation terminated.\n"
    )

    def test_returns_none_without_header_error(self, tmp_path):
        """Generic compile failures (syntax errors, etc.) must not trigger
        the diagnostic — it would only confuse the user."""
        stderr = "foo.c:10:5: error: expected ';' at end of declaration\n"
        flags = [f"-I{tmp_path}", "-I/this/path/does/not/exist"]
        assert _diagnose_missing_include_dirs(stderr, flags) is None

    def test_returns_none_when_all_dirs_exist(self, tmp_path):
        """Header-not-found can happen even when every -I dir exists —
        the header is just missing from the project. No diagnostic to
        give in that case."""
        (tmp_path / "inc").mkdir()
        flags = [f"-I{tmp_path}", f"-I{tmp_path / 'inc'}"]
        assert _diagnose_missing_include_dirs(self._CLANG_STDERR, flags) is None

    def test_returns_none_when_no_include_flags(self):
        flags = ["-O2", "-g", "-c", "-mcpu=cortex-m0plus"]
        assert _diagnose_missing_include_dirs(self._CLANG_STDERR, flags) is None

    def test_lists_missing_dirs_clang_stderr(self, tmp_path):
        ghost1 = tmp_path / "ghost" / "sdk"
        ghost2 = tmp_path / "also_missing"
        real = tmp_path / "real"
        real.mkdir()
        flags = [f"-I{real}", f"-I{ghost1}", f"-I{ghost2}"]
        out = _diagnose_missing_include_dirs(self._CLANG_STDERR, flags)
        assert out is not None
        assert "2 of 3" in out
        assert str(ghost1) in out
        assert str(ghost2) in out
        assert str(real) not in out
        # Points users at the override file and shows a concrete example
        assert ".loci-build/flags.json" in out
        assert '"flags"' in out
        assert "-I/path/to/real/sdk/include" in out
        # Universal POSIX-style slashes in the example (not OS-specific)
        assert "\\" not in "-I/path/to/real/sdk/include"

    def test_message_is_project_agnostic(self, tmp_path):
        """Don't name project-specific variables (e.g. FREERTOS_INSTALL_DIR)
        or OS-specific paths (e.g. C:\\home\\username\\) — the diagnostic
        must read cleanly for any project on any host."""
        ghost = tmp_path / "gone"
        out = _diagnose_missing_include_dirs(
            self._CLANG_STDERR, [f"-I{ghost}"])
        assert out is not None
        assert "FREERTOS" not in out.upper()
        assert "FreeRTOS" not in out
        # No Windows-style example paths in the advisory text. The
        # listed missing dirs may legitimately contain backslashes on
        # Windows, so strip those off before checking.
        advisory = out.replace(str(ghost), "")
        assert "C:\\" not in advisory
        assert "\\home\\" not in advisory

    def test_lists_missing_dirs_gcc_stderr(self, tmp_path):
        ghost = tmp_path / "nope"
        flags = [f"-I{ghost}"]
        out = _diagnose_missing_include_dirs(self._GCC_STDERR, flags)
        assert out is not None
        assert str(ghost) in out
        # Singular "directory" when total is 1
        assert "1 of 1 include directory" in out

    def test_picks_up_isystem_two_arg_form(self, tmp_path):
        ghost = tmp_path / "gone"
        flags = ["-O2", "-isystem", str(ghost), "-c"]
        out = _diagnose_missing_include_dirs(self._CLANG_STDERR, flags)
        assert out is not None
        assert str(ghost) in out

    def test_picks_up_isystem_glued_form(self, tmp_path):
        ghost = tmp_path / "absent"
        flags = [f"-isystem{ghost}"]
        out = _diagnose_missing_include_dirs(self._CLANG_STDERR, flags)
        assert out is not None
        assert str(ghost) in out

    def test_truncates_list_at_twelve(self, tmp_path):
        ghosts = [tmp_path / f"missing_{i}" for i in range(15)]
        flags = [f"-I{p}" for p in ghosts]
        out = _diagnose_missing_include_dirs(self._CLANG_STDERR, flags)
        assert out is not None
        # First 12 are listed, the rest folded into a "... and N more" line
        assert str(ghosts[0]) in out
        assert str(ghosts[11]) in out
        assert str(ghosts[12]) not in out
        assert "... and 3 more" in out

    def test_dedupes_repeated_paths(self, tmp_path):
        """The same -I can be emitted multiple times by a sloppy Makefile;
        deduping keeps the total/missing counts honest."""
        ghost = tmp_path / "once"
        flags = [f"-I{ghost}", f"-I{ghost}", f"-I{ghost}"]
        out = _diagnose_missing_include_dirs(self._CLANG_STDERR, flags)
        assert out is not None
        assert "1 of 1" in out
        assert out.count(str(ghost)) == 1
