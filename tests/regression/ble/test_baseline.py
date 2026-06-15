"""Regression tests: compare current BLE output against stored baselines."""

import csv
import io

import pytest

from asm_analyze import extract_assembly, extract_symbols, slice_elf

from tests.regression.ble.conftest import BLE_BASELINE_PROJECT

pytestmark = [pytest.mark.regression, pytest.mark.ble, pytest.mark.slow]


def _approx_equal(actual, expected, tolerance=0.05):
    """Check if actual is within ±tolerance of expected."""
    if expected == 0:
        return actual == 0
    return abs(actual - expected) / expected <= tolerance


class TestBleBaseline:
    def test_baseline_slice_elf_structure(
        self, ble_basic_ble_elf, require_asmslicer,
        load_baseline, save_baseline, update_baselines,
    ):
        result = slice_elf(str(ble_basic_ble_elf))

        snapshot = {
            "architecture": result.get("architecture"),
            "timing_architecture": result.get("timing_architecture"),
            "asm_function_count": len(result.get("asm", {})) if isinstance(result.get("asm"), dict) else 0,
            "symbol_count": len(result.get("symbols", [])) if isinstance(result.get("symbols"), list) else 0,
        }

        if update_baselines:
            save_baseline(BLE_BASELINE_PROJECT, "slice_elf_structure", snapshot)
            return

        baseline = load_baseline(BLE_BASELINE_PROJECT, "slice_elf_structure")
        if baseline is None:
            pytest.skip("No baseline found — run with --update-baselines first")

        assert snapshot["architecture"] == baseline["architecture"]
        assert snapshot["timing_architecture"] == baseline["timing_architecture"]
        assert _approx_equal(snapshot["asm_function_count"], baseline["asm_function_count"])
        assert _approx_equal(snapshot["symbol_count"], baseline["symbol_count"])

    def test_baseline_extract_symbols_count(
        self, ble_basic_ble_elf, require_asmslicer,
        load_baseline, save_baseline, update_baselines,
    ):
        result = extract_symbols(str(ble_basic_ble_elf))
        symbols = result.get("symbols", [])

        snapshot = {
            "total_count": len(symbols),
            "sample_names": [s["name"] for s in symbols[:10]],
        }

        if update_baselines:
            save_baseline(BLE_BASELINE_PROJECT, "extract_symbols_count", snapshot)
            return

        baseline = load_baseline(BLE_BASELINE_PROJECT, "extract_symbols_count")
        if baseline is None:
            pytest.skip("No baseline found — run with --update-baselines first")

        assert _approx_equal(snapshot["total_count"], baseline["total_count"])
        # At least some known symbols should still be present
        baseline_names = set(baseline["sample_names"])
        current_names = {s["name"] for s in symbols}
        overlap = baseline_names & current_names
        assert len(overlap) > 0, "No overlap between baseline and current symbols"

    def test_baseline_extract_assembly_known_func(
        self, ble_basic_ble_elf, require_asmslicer,
        load_baseline, save_baseline, update_baselines,
    ):
        result = extract_assembly(str(ble_basic_ble_elf))
        funcs = result.get("functions", {})

        # Pick first function as the known reference
        if not funcs:
            pytest.skip("No functions extracted")
        first_name = sorted(funcs.keys())[0]
        func_data = funcs[first_name]

        snapshot = {
            "function_name": first_name,
            "instruction_count": len(func_data.get("instructions", [])),
            "start_address": func_data.get("start_address", ""),
        }

        if update_baselines:
            save_baseline(BLE_BASELINE_PROJECT, "extract_assembly_known_func", snapshot)
            return

        baseline = load_baseline(BLE_BASELINE_PROJECT, "extract_assembly_known_func")
        if baseline is None:
            pytest.skip("No baseline found — run with --update-baselines first")

        assert snapshot["function_name"] == baseline["function_name"]
        assert _approx_equal(
            snapshot["instruction_count"], baseline["instruction_count"]
        )

    def test_baseline_timing_csv_row_count(
        self, ble_basic_ble_elf, require_asmslicer,
        load_baseline, save_baseline, update_baselines,
    ):
        result = extract_assembly(str(ble_basic_ble_elf))
        timing_csv = result.get("timing_csv", "")
        rows = list(csv.reader(io.StringIO(timing_csv)))
        row_count = len(rows) - 1  # exclude header

        snapshot = {"row_count": row_count}

        if update_baselines:
            save_baseline(BLE_BASELINE_PROJECT, "timing_csv_row_count", snapshot)
            return

        baseline = load_baseline(BLE_BASELINE_PROJECT, "timing_csv_row_count")
        if baseline is None:
            pytest.skip("No baseline found — run with --update-baselines first")

        assert _approx_equal(row_count, baseline["row_count"])
