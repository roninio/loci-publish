"""Tests for asm_analyze.memmap() heap-allocation plumbing.

Covers the --allocators-file parser only; end-to-end heap detection is
exercised by loci-service-asmslicer's test_heap.py against pre-built
fixtures. Here we verify the file-parsing edge cases (comments, blanks,
empty file) and the contract that an explicit empty file means "no
allocators" (not "fall back to defaults").
"""

import pytest

from asm_analyze import memmap as memmap_wrapper

pytestmark = pytest.mark.unit


def _capture_allocators(monkeypatch, **kwargs):
    """Stub the inner memmap() and return whatever 'allocators' value the
    wrapper passes through. Avoids running the real ELF analysis."""
    captured = {}

    def fake_memmap(elf_path, comparing_elf_path, map_file, top_n,
                     with_heap, allocators):
        captured['allocators'] = allocators
        return {'mode': 'report'}

    # Patch at the import-site inside memmap_wrapper.
    import loci.service.asmslicer.memmap as inner_mod
    monkeypatch.setattr(inner_mod, 'memmap', fake_memmap)
    result = memmap_wrapper(elf_path='dummy.elf', **kwargs)
    return captured.get('allocators'), result


class TestAllocatorsFileParser:
    def test_no_file_passes_none(self, monkeypatch, tmp_path):
        allocators, _ = _capture_allocators(monkeypatch, with_heap=True)
        assert allocators is None  # falls through to DEFAULT_ALLOCATORS

    def test_simple_list(self, monkeypatch, tmp_path):
        f = tmp_path / 'allocs.txt'
        f.write_text('malloc\ncalloc\nfree\n', encoding='utf-8')
        allocators, _ = _capture_allocators(
            monkeypatch, with_heap=True, allocators_file=str(f),
        )
        assert allocators == frozenset({'malloc', 'calloc', 'free'})

    def test_comments_skipped(self, monkeypatch, tmp_path):
        f = tmp_path / 'allocs.txt'
        f.write_text(
            '# project allocators\nmy_malloc\n# this is a comment\nmy_free\n',
            encoding='utf-8',
        )
        allocators, _ = _capture_allocators(
            monkeypatch, with_heap=True, allocators_file=str(f),
        )
        assert allocators == frozenset({'my_malloc', 'my_free'})

    def test_blank_lines_skipped(self, monkeypatch, tmp_path):
        f = tmp_path / 'allocs.txt'
        f.write_text('\n\nmalloc\n\n\ncalloc\n\n', encoding='utf-8')
        allocators, _ = _capture_allocators(
            monkeypatch, with_heap=True, allocators_file=str(f),
        )
        assert allocators == frozenset({'malloc', 'calloc'})

    def test_indented_comments_also_skipped(self, monkeypatch, tmp_path):
        """A line whose only non-whitespace is `#...` should be a comment
        — even with leading whitespace."""
        f = tmp_path / 'allocs.txt'
        f.write_text('   # indented comment\nmalloc\n\t# tabbed comment\ncalloc\n',
                     encoding='utf-8')
        allocators, _ = _capture_allocators(
            monkeypatch, with_heap=True, allocators_file=str(f),
        )
        assert allocators == frozenset({'malloc', 'calloc'})

    def test_whitespace_around_names_stripped(self, monkeypatch, tmp_path):
        f = tmp_path / 'allocs.txt'
        f.write_text('  malloc  \n\tcalloc\t\n', encoding='utf-8')
        allocators, _ = _capture_allocators(
            monkeypatch, with_heap=True, allocators_file=str(f),
        )
        assert allocators == frozenset({'malloc', 'calloc'})

    def test_empty_file_means_empty_set_not_defaults(self, monkeypatch, tmp_path):
        """An explicit empty allocators file means 'detect nothing' — it
        must NOT silently fall back to DEFAULT_ALLOCATORS."""
        f = tmp_path / 'empty.txt'
        f.write_text('', encoding='utf-8')
        allocators, _ = _capture_allocators(
            monkeypatch, with_heap=True, allocators_file=str(f),
        )
        assert allocators == frozenset()
        assert allocators is not None

    def test_file_only_comments_means_empty_set(self, monkeypatch, tmp_path):
        """A file containing only comments / blank lines is the same as
        empty: explicit override to empty, not fallback to defaults."""
        f = tmp_path / 'only_comments.txt'
        f.write_text('# nothing here\n\n  # also a comment\n', encoding='utf-8')
        allocators, _ = _capture_allocators(
            monkeypatch, with_heap=True, allocators_file=str(f),
        )
        assert allocators == frozenset()

    def test_duplicates_deduped(self, monkeypatch, tmp_path):
        f = tmp_path / 'allocs.txt'
        f.write_text('malloc\nmalloc\nfree\nmalloc\nfree\n', encoding='utf-8')
        allocators, _ = _capture_allocators(
            monkeypatch, with_heap=True, allocators_file=str(f),
        )
        assert allocators == frozenset({'malloc', 'free'})

    def test_missing_file_returns_error_dict(self, tmp_path):
        result = memmap_wrapper(
            elf_path='dummy.elf',
            with_heap=True,
            allocators_file=str(tmp_path / 'does_not_exist.txt'),
        )
        assert 'error' in result
        assert 'allocators file' in result['error']
