"""Tests for parse_functions_from_asm(), parse_symbols(), match_function()."""

import pytest

from asm_analyze import match_function, parse_functions_from_asm, parse_symbols
from tests.fixtures.asm_samples import (
    COMPLEX_NAMES_ASM,
    EMPTY_BODY_ASM,
    MULTI_FUNCTION_ASM,
    SINGLE_FUNCTION_ASM,
)
from tests.fixtures.csv_samples import SYMMAP_CSV, SYMMAP_CSV_BAD_SIZE, SYMMAP_CSV_EMPTY

pytestmark = pytest.mark.unit


# -- parse_functions_from_asm ---------------------------------------------

class TestParseFunctionsFromAsm:
    def test_single_function(self):
        funcs = parse_functions_from_asm(SINGLE_FUNCTION_ASM)
        assert "main" in funcs
        assert funcs["main"]["start_address"] == "0x00008000"
        assert len(funcs["main"]["instructions"]) == 5

    def test_multiple_functions(self):
        funcs = parse_functions_from_asm(MULTI_FUNCTION_ASM)
        assert len(funcs) == 3
        assert set(funcs.keys()) == {"init_hardware", "process_data", "cleanup"}

    def test_empty_body_skipped(self):
        funcs = parse_functions_from_asm(EMPTY_BODY_ASM)
        assert "empty_func" not in funcs
        assert "real_func" in funcs

    def test_hex_address(self):
        funcs = parse_functions_from_asm(SINGLE_FUNCTION_ASM)
        assert funcs["main"]["start_address"] == "0x00008000"

    def test_cpp_mangled_names(self):
        funcs = parse_functions_from_asm(COMPLEX_NAMES_ASM)
        assert "std::vector<int>::push_back(int const&)" in funcs
        assert "ns::MyClass<T>::~MyClass()" in funcs


# -- parse_symbols --------------------------------------------------------

class TestParseSymbols:
    def test_basic(self):
        symbols = parse_symbols(SYMMAP_CSV)
        assert len(symbols) == 3
        assert symbols[0]["name"] == "main"
        assert symbols[0]["long_name"] == "main()"
        assert symbols[0]["start_address"] == "0x8000"
        assert symbols[0]["size"] == 64

    def test_empty(self):
        symbols = parse_symbols(SYMMAP_CSV_EMPTY)
        assert symbols == []

    def test_non_numeric_size(self):
        symbols = parse_symbols(SYMMAP_CSV_BAD_SIZE)
        assert symbols[0]["size"] == 0


# -- match_function -------------------------------------------------------

class TestMatchFunction:
    def test_exact_name(self):
        assert match_function("main", "main", "main()") is True

    def test_exact_long_name(self):
        assert match_function("ns::init(int)", "init", "ns::init(int)") is True

    def test_prefix_with_params(self):
        assert match_function("helper", "helper", "helper(void)") is True

    def test_no_match(self):
        assert match_function("bar", "foo", "foo(int)") is False
