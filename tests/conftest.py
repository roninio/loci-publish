"""Root conftest — sys.path, CLI options, markers, shared fixtures."""

import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: prevent asm_analyze.py venv re-exec guard
# ---------------------------------------------------------------------------
os.environ["_LOCI_BOOTSTRAP"] = "1"

# ---------------------------------------------------------------------------
# sys.path: make lib/ and hooks/ importable
# ---------------------------------------------------------------------------
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_LIB_DIR = _PLUGIN_ROOT / "lib"
_HOOKS_DIR = _PLUGIN_ROOT / "hooks"

for p in (_LIB_DIR, _HOOKS_DIR):
    ps = str(p)
    if ps not in sys.path:
        sys.path.insert(0, ps)


# ---------------------------------------------------------------------------
# CLI options
# ---------------------------------------------------------------------------
def pytest_addoption(parser):
    parser.addoption(
        "--ble-root",
        default=None,
        help="Path to BLE project root (overrides LOCI_TEST_BLE_ROOT env var)",
    )
    parser.addoption(
        "--update-baselines",
        action="store_true",
        default=False,
        help="Regenerate regression baseline files instead of comparing",
    )


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def plugin_root():
    """Absolute path to the loci root directory."""
    return _PLUGIN_ROOT


@pytest.fixture(scope="session")
def ble_root(request):
    """Path to BLE project root, or None if not configured.

    CLI --ble-root overrides env LOCI_TEST_BLE_ROOT.
    """
    cli = request.config.getoption("--ble-root")
    env = os.environ.get("LOCI_TEST_BLE_ROOT")
    raw = cli or env
    if raw:
        p = Path(raw)
        if p.is_dir():
            return p
    return None


_BLE_BASIC_BLE_ELF = Path(
    "examples/rtos/LP_EM_CC2340R5/ble5stack/basic_ble/freertos/ticlang/basic_ble.out"
)


@pytest.fixture(scope="session")
def ble_basic_ble_elf(ble_root):
    """Resolved path to the basic_ble ELF, or skip if unavailable."""
    if ble_root is None:
        pytest.skip("BLE project root not configured (set LOCI_TEST_BLE_ROOT or --ble-root)")
    elf = ble_root / _BLE_BASIC_BLE_ELF
    if not elf.is_file():
        pytest.skip(f"BLE ELF not found: {elf}")
    return elf


@pytest.fixture(scope="session")
def update_baselines(request):
    """Whether to regenerate baseline files."""
    return request.config.getoption("--update-baselines")


@pytest.fixture(scope="session")
def require_asmslicer():
    """Skip if loci.service.asmslicer is not importable."""
    try:
        from loci.service.asmslicer import asmslicer  # noqa: F401
    except ImportError:
        pytest.skip(
            "loci.service.asmslicer not available — "
            "run 'python tests/bootstrap_venv.py' to create the plugin venv, "
            "then run pytest from .venv"
        )


@pytest.fixture(scope="session")
def heap_fixtures_dir():
    """Directory containing heap_alloc_*.elf / .o fixtures, or None.

    Lookup order:
      1. LOCI_TEST_HEAP_FIXTURES_DIR env var
      2. ../loci-service-asmslicer/test/heap_fixtures/ (sibling checkout
         of the asmslicer source repo — common dev layout)
      3. None → tests skip

    Fixtures are built from heap_alloc.c (linked + relocatable, aarch64
    + Cortex-M Thumb) and committed to the asmslicer repo.
    """
    env_dir = os.environ.get("LOCI_TEST_HEAP_FIXTURES_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            return p
    sibling = _PLUGIN_ROOT.parent / "loci-service-asmslicer" / "test" / "heap_fixtures"
    if sibling.is_dir():
        return sibling
    return None
