"""Tests for parse_blocks_to_timing_csv() and chunk_timing_csv()."""

import csv
import io

import pandas as pd
import pytest

from asm_analyze import chunk_timing_csv, parse_blocks_to_timing_csv
from tests.fixtures.csv_samples import BLOCKS_CSV, BLOCKS_CSV_EMPTY_ASM

pytestmark = pytest.mark.unit


class TestParseBlocksToTimingCsv:
    def test_basic(self):
        result = parse_blocks_to_timing_csv(BLOCKS_CSV)
        reader = list(csv.reader(io.StringIO(result)))
        # 2 main rows + 1 init row + header = 4
        assert len(reader) == 4
        # function_name = long_name_from_addr
        assert reader[1][0] == "main()_0x8000"

    def test_filter_by_functions(self):
        result = parse_blocks_to_timing_csv(BLOCKS_CSV, functions=["init"])
        reader = list(csv.reader(io.StringIO(result)))
        # header + 1 init row
        assert len(reader) == 2
        assert "ns::init(int)" in reader[1][0]

    def test_empty_asm_skipped(self):
        result = parse_blocks_to_timing_csv(BLOCKS_CSV_EMPTY_ASM)
        reader = list(csv.reader(io.StringIO(result)))
        # header + 1 non-empty row only
        assert len(reader) == 2

    def test_no_filter(self):
        result = parse_blocks_to_timing_csv(BLOCKS_CSV, functions=None)
        reader = list(csv.reader(io.StringIO(result)))
        assert len(reader) == 4  # header + 3 data rows

    def test_csv_header(self):
        result = parse_blocks_to_timing_csv(BLOCKS_CSV)
        first_line = result.split("\n")[0]
        assert first_line.strip() == "function_name,assembly_code"


class TestChunkTimingCsv:
    """Regression: the server's pandas.read_csv parses each chunk back into
    a DataFrame. Chunking must split only on CSV record boundaries — never
    inside a quoted assembly_code field — or pandas raises ParserError and
    the whole MCP call fails."""

    @staticmethod
    def _build_csv(rows: list[tuple[str, str]]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["function_name", "assembly_code"])
        for fn, asm in rows:
            w.writerow([fn, asm])
        return buf.getvalue()

    def test_single_chunk_preserves_header(self):
        csv_text = self._build_csv([("foo_0x10", "mov r0, #1\nbx lr")])
        chunks = chunk_timing_csv(csv_text)
        assert len(chunks) == 1
        assert chunks[0].startswith("function_name,assembly_code")

    def test_each_chunk_is_valid_csv(self):
        """With multi-line assembly, every chunk must parse cleanly via
        pandas.read_csv — the exact round-trip the server performs."""
        long_asm = "\n".join(f"op_{i} r{i % 8}, #{i}" for i in range(200))
        rows = [(f"fn_{i}_0x{i:04x}", long_asm) for i in range(10)]
        csv_text = self._build_csv(rows)

        # Force multiple chunks by shrinking the cap well below total size.
        chunks = chunk_timing_csv(csv_text, max_chars=5000)
        assert len(chunks) > 1

        for idx, chunk in enumerate(chunks):
            df = pd.read_csv(io.StringIO(chunk))
            assert list(df.columns) == ["function_name", "assembly_code"], (
                f"chunk {idx} columns mismatch"
            )
            # Every row must have both fields non-null.
            assert df["function_name"].notna().all()
            assert df["assembly_code"].notna().all()

    def test_row_count_preserved_across_chunks(self):
        """Sum of rows across chunks equals original row count."""
        long_asm = "\n".join(f"op r{i}, #{i}" for i in range(50))
        original_rows = [(f"fn_{i}", long_asm) for i in range(20)]
        csv_text = self._build_csv(original_rows)

        chunks = chunk_timing_csv(csv_text, max_chars=3000)
        total = 0
        for chunk in chunks:
            df = pd.read_csv(io.StringIO(chunk))
            total += len(df)
        assert total == len(original_rows)

    def test_embedded_newlines_and_commas_survive(self):
        """Assembly with commas AND newlines must round-trip intact."""
        tricky = "mov r0, #1, lsl #2\nldr r1, [r2, #4]\nbx lr"
        csv_text = self._build_csv([("tricky_0x0", tricky)])
        chunks = chunk_timing_csv(csv_text)
        assert len(chunks) == 1
        df = pd.read_csv(io.StringIO(chunks[0]))
        assert df.iloc[0]["assembly_code"] == tricky

    def test_empty_input_returns_no_chunks(self):
        """Skills iterate chunks; empty payloads must never reach the MCP."""
        assert chunk_timing_csv("") == []

    def test_header_only_input_returns_no_chunks(self):
        """A CSV with just the header row means zero functions to analyze.
        Sending it to the MCP would trigger the server's skip-message path;
        skills must skip the call entirely instead."""
        assert chunk_timing_csv("function_name,assembly_code\n") == []

    def test_oversize_single_row_kept(self):
        """A row larger than max_chars goes in its own chunk rather than
        silently vanishing. The server's per-row token-limit check decides
        whether to skip it."""
        big_asm = "mov r0, #0\n" * 10000
        csv_text = self._build_csv([("big_0x0", big_asm)])
        chunks = chunk_timing_csv(csv_text, max_chars=1000)
        assert len(chunks) == 1
        df = pd.read_csv(io.StringIO(chunks[0]))
        assert len(df) == 1
