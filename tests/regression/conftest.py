"""Regression conftest — baseline load/save helpers."""

import json
from pathlib import Path

import pytest

BASELINES_DIR = Path(__file__).resolve().parent / "baselines"


def pytest_collection_modifyitems(config, items):
    """Auto-add 'regression' marker to all tests in this directory."""
    for item in items:
        if "regression" in str(item.fspath):
            item.add_marker(pytest.mark.regression)


@pytest.fixture(scope="session")
def load_baseline():
    """Return a callable that loads a baseline JSON file.

    Usage: data = load_baseline("ble_basic_ble", "slice_elf_structure")
    Skips test if baseline doesn't exist and --update-baselines not set.
    """
    def _load(project: str, name: str) -> dict | None:
        path = BASELINES_DIR / project / f"{name}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    return _load


@pytest.fixture(scope="session")
def save_baseline():
    """Return a callable that saves a baseline JSON file.

    Usage: save_baseline("ble_basic_ble", "slice_elf_structure", data)
    """
    def _save(project: str, name: str, data: dict):
        project_dir = BASELINES_DIR / project
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{name}.json"
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return _save
