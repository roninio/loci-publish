"""BLE integration conftest — cached analysis fixtures."""

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-add 'ble' marker to all tests in this directory."""
    for item in items:
        if "integration/ble" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.ble)


@pytest.fixture(scope="module")
def ble_analysis_result(ble_basic_ble_elf, require_asmslicer):
    """Cached run_analysis() result for the BLE ELF.

    Module-scoped to avoid re-running the 30-60s analysis per test file.
    """
    from asm_analyze import run_analysis

    result = run_analysis(str(ble_basic_ble_elf))
    assert "files" in result, "run_analysis() returned no files"
    return result
