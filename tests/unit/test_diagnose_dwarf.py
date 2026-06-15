"""Tests for _diagnose_elf() ELF diagnostic helper."""

from unittest.mock import MagicMock, patch

from asm_analyze import _diagnose_elf

ELFFILE_PATH = "elftools.elf.elffile.ELFFile"


def _make_section(name, data_size, is_symtab=False, symbols=None):
    """Build a mock ELF section."""
    from elftools.elf.sections import SymbolTableSection

    section = MagicMock()
    section.name = name
    section.data_size = data_size
    if is_symtab:
        section.__class__ = SymbolTableSection
        section.iter_symbols.return_value = symbols or []
    return section


def _make_symbol(sym_type="STT_FUNC"):
    sym = MagicMock()
    sym.entry.st_info.type = sym_type
    return sym


def _mock_elf(text_size=0, func_count=0, has_dwarf=False, cu_versions=None):
    """Build a mock ELFFile."""
    from elftools.elf.sections import SymbolTableSection

    symbols = [_make_symbol("STT_FUNC") for _ in range(func_count)]
    sections = [
        _make_section(".text", text_size),
        _make_section(".symtab", 0, is_symtab=True, symbols=symbols),
    ]
    elf = MagicMock()
    elf.iter_sections.return_value = sections
    elf.has_dwarf_info.return_value = has_dwarf

    if cu_versions is not None:
        cus = []
        for ver in cu_versions:
            cu = MagicMock()
            cu.header = {"version": ver}
            cus.append(cu)
        dwarf_info = MagicMock()
        dwarf_info.iter_CUs.return_value = iter(cus)
        elf.get_dwarf_info.return_value = dwarf_info

    return elf


class TestDiagnoseElf:
    def test_empty_object_file(self, tmp_path):
        """Empty .text + 0 functions → preprocessor conditional message."""
        fake = tmp_path / "test.o"
        fake.write_bytes(b"\x7fELF" + b"\x00" * 100)
        with patch(ELFFILE_PATH, return_value=_mock_elf(text_size=0, func_count=0)):
            result = _diagnose_elf(str(fake))
        assert "contains no code" in result
        assert "preprocessor" in result
        assert "-D" in result

    def test_code_present_no_dwarf(self, tmp_path):
        """Code present but no DWARF → suggest -g."""
        fake = tmp_path / "test.o"
        fake.write_bytes(b"\x7fELF" + b"\x00" * 100)
        with patch(ELFFILE_PATH, return_value=_mock_elf(
            text_size=256, func_count=3, has_dwarf=False
        )):
            result = _diagnose_elf(str(fake))
        assert "3 function(s)" in result
        assert "no DWARF" in result
        assert "-g" in result

    def test_dwarf_present_with_version(self, tmp_path):
        """DWARF present + functions → report version and possible format issue."""
        fake = tmp_path / "test.o"
        fake.write_bytes(b"\x7fELF" + b"\x00" * 100)
        with patch(ELFFILE_PATH, return_value=_mock_elf(
            text_size=256, func_count=5, has_dwarf=True, cu_versions=[4]
        )):
            result = _diagnose_elf(str(fake))
        assert "DWARF version 4" in result
        assert "5 function(s)" in result

    def test_file_not_found(self):
        result = _diagnose_elf("/nonexistent/path/to/file.o")
        assert "Could not inspect" in result

    def test_non_elf_file(self, tmp_path):
        fake = tmp_path / "not_elf.txt"
        fake.write_text("this is not an ELF file")
        result = _diagnose_elf(str(fake))
        assert "Could not inspect" in result

    def test_pyelftools_import_error(self, tmp_path):
        fake = tmp_path / "test.o"
        fake.write_bytes(b"\x7fELF" + b"\x00" * 100)
        with patch.dict("sys.modules", {
            "elftools": None,
            "elftools.elf": None,
            "elftools.elf.elffile": None,
            "elftools.elf.sections": None,
        }):
            result = _diagnose_elf(str(fake))
        assert "pyelftools" in result.lower() or "Could not inspect" in result
