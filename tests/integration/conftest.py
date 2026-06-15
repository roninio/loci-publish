"""Integration conftest — asmslicer availability check, auto-markers."""

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-add 'integration' marker to all tests in this directory."""
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


# require_asmslicer fixture is defined in root conftest.py
