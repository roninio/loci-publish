"""End-to-end integration tests for the heap-allocation pipeline.

Exercises the full path: plugin Python entry point → installed
loci-service-asmslicer wheel → real built ELF / .o fixture.

These tests are the canary that catches regressions where the wheel
version pinned in requirements.txt doesn't match the heap.py code we
expect — for example, if the asmslicer 1.0.9 wheel was rebuilt
without the Thumb-mode Capstone fix, the cortexm cases here would
flip to 0 sites and fail loudly.

Fixtures come from loci-service-asmslicer/test/heap_fixtures/. Set
LOCI_TEST_HEAP_FIXTURES_DIR to point elsewhere, or check out the
asmslicer source repo as a sibling of loci-claude/.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from asm_analyze import memmap as memmap_func


def _fixture(heap_fixtures_dir, name: str) -> Path:
    return heap_fixtures_dir / name


# ──────────────────────────────────────────────────────────────────────────
# Python-API path: plugin's memmap() wrapper → asmslicer wheel
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    'fixture',
    ['heap_alloc_aarch64.elf',  'heap_alloc_aarch64.o',
     'heap_alloc_cortexm.elf',  'heap_alloc_cortexm.o'],
)
def test_memmap_with_heap_finds_all_allocators(heap_fixtures_dir, require_asmslicer, fixture):
    """All four allocator calls (malloc, calloc, pvPortMalloc, free) must
    resolve, with malloc/pvPortMalloc carrying their literal sizes."""
    if heap_fixtures_dir is None:
        pytest.skip('heap fixtures not available')
    path = _fixture(heap_fixtures_dir, fixture)
    if not path.is_file():
        pytest.skip(f'fixture missing: {fixture}')

    result = memmap_func(elf_path=str(path), with_heap=True)
    assert 'heap' in result, result
    totals = result['heap']['totals']
    assert totals['alloc_sites'] == 4, totals
    assert totals['static_bytes'] == 640, totals  # 128 (malloc) + 512 (pvPortMalloc)
    assert totals['dynamic_sites'] == 1, totals   # calloc with register-source count

    by_caller = {
        fn: {(s['callee'], s['size']) for s in sites}
        for fn, sites in result['heap']['per_function'].items()
    }
    assert ('malloc', 128) in by_caller.get('parse_packet', set()), by_caller
    assert ('pvPortMalloc', 512) in by_caller.get('taskAlloc', set()), by_caller
    assert ('calloc', None) in by_caller.get('init_buffers', set()), by_caller
    assert ('free', None) in by_caller.get('cleanup', set()), by_caller


def test_memmap_delta_direction_through_plugin_api(heap_fixtures_dir, require_asmslicer):
    """Pass OLD as --elf-path and NEW as --comparing-elf-path; expect
    growth (positive delta, new sites in `added`)."""
    if heap_fixtures_dir is None:
        pytest.skip('heap fixtures not available')
    OLD = _fixture(heap_fixtures_dir, 'heap_alloc_before_aarch64.elf')
    NEW = _fixture(heap_fixtures_dir, 'heap_alloc_aarch64.elf')
    if not (OLD.is_file() and NEW.is_file()):
        pytest.skip('aarch64 delta fixtures missing')

    result = memmap_func(elf_path=str(OLD), comparing_elf_path=str(NEW),
                         with_heap=True)
    rom = result['summary_delta']['rom_total']
    assert rom['delta'] > 0, rom
    assert rom['current'] > rom['base'], rom

    delta = result['heap_delta']
    assert delta['alloc_sites_after'] > delta['alloc_sites_before'], delta
    added = {(x['caller'], x['callee'], x['size']) for x in delta['added']}
    # parse_packet→malloc(128) was added in NEW
    assert ('parse_packet', 'malloc', 128) in added, delta


def test_memmap_with_heap_no_heap_field_when_not_requested(heap_fixtures_dir, require_asmslicer):
    """Default (without --with-heap) must not include a heap field —
    keeps the JSON contract narrow for callers that don't care."""
    if heap_fixtures_dir is None:
        pytest.skip('heap fixtures not available')
    path = _fixture(heap_fixtures_dir, 'heap_alloc_aarch64.elf')
    if not path.is_file():
        pytest.skip('fixture missing')
    result = memmap_func(elf_path=str(path))  # default with_heap=False
    assert 'heap' not in result


# ──────────────────────────────────────────────────────────────────────────
# Subprocess CLI path: invoking lib/asm_analyze.py directly
# ──────────────────────────────────────────────────────────────────────────
# This is the most production-like test. The skill calls the CLI via
# Bash; if argparse plumbing or JSON contract regresses, this catches it.

def _run_cli(*args, plugin_root):
    """Invoke lib/asm_analyze.py via a subprocess, return parsed JSON."""
    cmd = [sys.executable, str(plugin_root / 'lib' / 'asm_analyze.py'), *args]
    env = {**os.environ, '_LOCI_BOOTSTRAP': '1', 'PYTHONIOENCODING': 'utf-8'}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, (
        f'CLI exited with {proc.returncode}\nstdout: {proc.stdout[-2000:]}'
        f'\nstderr: {proc.stderr[-2000:]}'
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize('fixture', ['heap_alloc_aarch64.elf', 'heap_alloc_cortexm.elf'])
def test_cli_memmap_with_heap_subprocess(plugin_root, heap_fixtures_dir,
                                          require_asmslicer, fixture):
    """End-to-end through the actual CLI subprocess that the skill calls."""
    if heap_fixtures_dir is None:
        pytest.skip('heap fixtures not available')
    path = _fixture(heap_fixtures_dir, fixture)
    if not path.is_file():
        pytest.skip(f'fixture missing: {fixture}')

    result = _run_cli('memmap', '--elf-path', str(path), '--with-heap',
                       plugin_root=plugin_root)
    assert result['mode'] == 'report'
    assert 'heap' in result
    assert result['heap']['totals']['alloc_sites'] == 4
    assert result['heap']['totals']['static_bytes'] == 640
    assert result['heap']['totals']['dynamic_sites'] == 1


def test_cli_memmap_with_heap_delta_subprocess(plugin_root, heap_fixtures_dir,
                                                 require_asmslicer):
    """Delta mode through the CLI: positive ROM delta when OLD→NEW grows."""
    if heap_fixtures_dir is None:
        pytest.skip('heap fixtures not available')
    OLD = _fixture(heap_fixtures_dir, 'heap_alloc_before_aarch64.elf')
    NEW = _fixture(heap_fixtures_dir, 'heap_alloc_aarch64.elf')
    if not (OLD.is_file() and NEW.is_file()):
        pytest.skip('aarch64 delta fixtures missing')
    result = _run_cli(
        'memmap', '--elf-path', str(OLD), '--comparing-elf-path', str(NEW),
        '--with-heap', plugin_root=plugin_root,
    )
    assert result['mode'] == 'delta'
    rom = result['summary_delta']['rom_total']
    assert rom['delta'] > 0, rom
    assert result['heap_delta']['alloc_sites_after'] > result['heap_delta']['alloc_sites_before']


def test_cli_allocators_file_overrides_default_catalog(plugin_root, heap_fixtures_dir,
                                                         require_asmslicer, tmp_path):
    """An --allocators-file containing only `malloc` should detect 1 site
    (parse_packet→malloc) and miss the rest. Verifies that the CLI
    actually plumbs --allocators-file through to the wheel's catalog."""
    if heap_fixtures_dir is None:
        pytest.skip('heap fixtures not available')
    path = _fixture(heap_fixtures_dir, 'heap_alloc_aarch64.elf')
    if not path.is_file():
        pytest.skip('fixture missing')

    cat = tmp_path / 'cat.txt'
    cat.write_text('# only malloc\nmalloc\n', encoding='utf-8')

    result = _run_cli(
        'memmap', '--elf-path', str(path), '--with-heap',
        '--allocators-file', str(cat), plugin_root=plugin_root,
    )
    totals = result['heap']['totals']
    assert totals['alloc_sites'] == 1, totals
    assert totals['by_callee'] == {'malloc': 1}, totals
    # static_bytes = 128, the literal in malloc(128).
    assert totals['static_bytes'] == 128


def test_cli_allocators_file_empty_means_zero_sites(plugin_root, heap_fixtures_dir,
                                                      require_asmslicer, tmp_path):
    """An explicit empty allocators file means 'detect nothing' — must
    NOT silently fall back to DEFAULT_ALLOCATORS."""
    if heap_fixtures_dir is None:
        pytest.skip('heap fixtures not available')
    path = _fixture(heap_fixtures_dir, 'heap_alloc_aarch64.elf')
    if not path.is_file():
        pytest.skip('fixture missing')

    cat = tmp_path / 'empty.txt'
    cat.write_text('# nothing here, on purpose\n\n', encoding='utf-8')

    result = _run_cli(
        'memmap', '--elf-path', str(path), '--with-heap',
        '--allocators-file', str(cat), plugin_root=plugin_root,
    )
    assert result['heap']['totals']['alloc_sites'] == 0
    assert result['heap']['totals']['by_callee'] == {}
