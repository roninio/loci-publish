"""BLE regression conftest — project-specific baseline config."""

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-add 'ble' marker to all tests in this directory."""
    for item in items:
        if "regression/ble" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.ble)


BLE_BASELINE_PROJECT = "ble_basic_ble"
