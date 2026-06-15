#!/usr/bin/env python3
"""
extract_and_time.py — fuse loci-post-edit Steps 3 + 4 into one call.

Step 3 (extract-assembly) and Step 4 (timing via the LOCI HTTP API) are a
natural pipeline: extract assembly from the pre/post .o, then send every
timing CSV chunk to get_assembly_block_exec_behavior. This helper does the
*mechanical* part of that pipeline only:

  1. Run `asm-analyze extract-assembly` on the post-edit .o (and the pre-edit
     .o.prev unless --added), saving each JSON under <out-dir>.
  2. Fan out ALL timing CSV chunks — pre AND post — to the LOCI API
      concurrently with a thread pool over chunks.
  3. Concatenate each side's CSV responses (dedup the repeated header) into
     <out-dir>/<basename>.<fn-tag>.timing_pre.csv / .timing_post.csv.
  4. Print a manifest JSON to stdout: paths, chunk counts, per-side row
     counts, the CFG text paths, and the timing_architecture used.

It deliberately does NOT compute the headline % diff or expand bl/blx
call sites — that requires CFG judgment and hot-path selection, which the
SKILL.md reasoning pass (Steps 4-expansion + 5) owns. This helper hands the
skill the two concatenated CSVs (the source of truth) plus the CFG text so
that reasoning can proceed without any further asm-analyze / API calls.

Exit codes mirror api_client.py so the skill's Step-4 degradation table
still applies:
  0  success — manifest JSON on stdout
  2  bad invocation (missing flag, no chunks, asm-analyze failure)
  3  no LOCI_API_KEY found            (stop the skill)
  4  HTTP non-2xx from the API
  5  quota / rate-limit (HTTP 429)    (stop the skill)
  6  network/transport error

CLI
---
  python extract_and_time.py \
      --asm-analyze "<venv-python> <plugin>/lib/asm_analyze.py" \
      --arch aarch64 \
      --build-dir .loci-build/aarch64 \
      --basename random_counter \
      --functions isPrime,main \
      [--added]                # post-only: no .o.prev, no pre side
      [--api-arch A53]         # override the arch string sent to the API
      [--out-dir .loci-build]  # default: same dir as the .o
      [--jobs 8]               # max concurrent API calls (default 8)
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# api_client.py lives next to this file; import its request helpers directly so
# all concurrent chunk calls share the same initialized API context.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_client  # noqa: E402


def _run_extract(asm_cmd: list[str], elf: Path, functions: str, arch: str) -> dict:
    """Run asm-analyze extract-assembly and return the parsed JSON."""
    cmd = asm_cmd + [
        "extract-assembly",
        "--elf-path", str(elf),
        "--arch", arch,
    ]
    if functions:
        cmd += ["--functions", functions]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(
            f"extract_and_time: extract-assembly failed on {elf} "
            f"(exit {proc.returncode}):\n{proc.stderr}\n"
        )
        sys.exit(2)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"extract_and_time: extract-assembly emitted non-JSON on {elf}: {e}\n")
        sys.exit(2)


def _fetch_chunk(chunk: str, arch: str, api_key: str, session_id: str, timeout: int) -> str:
    """Send one CSV chunk to the API. Returns CSV text; raises on error (caught by caller)."""
    status, body = api_client._mcp_tools_call(chunk, arch, api_key, session_id, timeout)
    if status == 429:
        sys.stderr.buffer.write(body)
        sys.stderr.write("\n")
        sys.exit(5)  # quota — stop the whole run, mirrors skill rule
    if not (200 <= status < 300):
        sys.stderr.write(f"extract_and_time: HTTP {status} from API\n")
        sys.stderr.buffer.write(body)
        sys.stderr.write("\n")
        sys.exit(4)
    raw_text = api_client._parse_sse(body)
    try:
        rpc = json.loads(raw_text)
        return rpc["result"]["content"][0]["text"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        sys.stderr.write(f"extract_and_time: unexpected API response shape: {e}\n{raw_text[:500]}\n")
        sys.exit(4)


def _concat_csv(csv_texts: list[str]) -> str:
    """Concatenate CSV responses, keeping the header from the first only."""
    out_lines: list[str] = []
    header: str | None = None
    for text in csv_texts:
        lines = text.splitlines()
        if not lines:
            continue
        if header is None:
            header = lines[0]
            out_lines.append(header)
        # drop any repeated header rows
        out_lines.extend(ln for ln in lines[1:] if ln.strip())
    return "\n".join(out_lines) + ("\n" if out_lines else "")


def _process_side(label: str, extract: dict, out_dir: Path, basename: str,
                  fn_tag: str, api_arch: str, api_key: str, session_id: str,
                  jobs: int, timeout: int) -> dict:
    """Extract chunks for one side (pre/post), fetch concurrently, write CSV + CFG."""
    chunks = extract.get("timing_csv_chunks") or []
    if not chunks:
        sys.stderr.write(f"extract_and_time: no timing_csv_chunks for {label} side\n")
        sys.exit(2)

    # Persist the raw extract JSON and CFG text for the skill's reasoning pass.
    json_path = out_dir / f"{basename}.{fn_tag}.extract_{label}.json"
    json_path.write_text(json.dumps(extract), encoding="utf-8")
    cfg_path = out_dir / f"{basename}.{fn_tag}.cfg_{label}.txt"
    cfg_path.write_text(extract.get("control_flow_graph") or "", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(
            lambda c: _fetch_chunk(c, api_arch, api_key, session_id, timeout),
            chunks,
        ))

    csv_text = _concat_csv(results)
    csv_path = out_dir / f"{basename}.{fn_tag}.timing_{label}.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    rows = max(0, len([ln for ln in csv_text.splitlines() if ln.strip()]) - 1)
    return {
        "extract_json": str(json_path),
        "cfg_txt": str(cfg_path),
        "timing_csv": str(csv_path),
        "chunks": len(chunks),
        "block_rows": rows,
    }


def main() -> int:
    p = argparse.ArgumentParser(prog="extract_and_time.py")
    p.add_argument("--asm-analyze", required=True,
                   help="full asm-analyze command, e.g. '<venv-python> <plugin>/lib/asm_analyze.py'")
    p.add_argument("--arch", required=True, help="loci_target / asm-analyze --arch (e.g. aarch64)")
    p.add_argument("--build-dir", required=True, help="dir holding <basename>.o and <basename>.o.prev")
    p.add_argument("--basename", required=True, help="object basename, no extension")
    p.add_argument("--functions", default="", help="comma-separated function names (omit = all)")
    p.add_argument("--added", action="store_true",
                   help="post-only: skip the pre side (no .o.prev, no diff baseline)")
    p.add_argument("--api-arch", default="",
                   help="arch string sent to the API (default: extract-assembly's timing_architecture)")
    p.add_argument("--out-dir", default="", help="output dir (default: --build-dir)")
    p.add_argument("--jobs", type=int, default=8, help="max concurrent API calls (default 8)")
    p.add_argument("--timeout", type=int, default=api_client.DEFAULT_TIMEOUT_S)
    args = p.parse_args()

    asm_cmd = shlex.split(args.asm_analyze)
    build_dir = Path(args.build_dir)
    out_dir = Path(args.out_dir) if args.out_dir else build_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    fn_tag = "all" if not args.functions else args.functions.replace(",", "_")[:60]

    post_o = build_dir / f"{args.basename}.o"
    if not post_o.is_file():
        sys.stderr.write(f"extract_and_time: post-edit object not found: {post_o}\n")
        return 2

    # ---- Step 3: extract (post always; pre unless --added) ----
    post_extract = _run_extract(asm_cmd, post_o, args.functions, args.arch)
    api_arch = args.api_arch or post_extract.get("timing_architecture") or args.arch

    pre_extract = None
    if not args.added:
        pre_o = build_dir / f"{args.basename}.o.prev"
        if not pre_o.is_file():
            sys.stderr.write(
                f"extract_and_time: --added not set but {pre_o} is missing. "
                "Pass --added for a first-edit (no baseline) measurement.\n"
            )
            return 2
        pre_extract = _run_extract(asm_cmd, pre_o, args.functions, args.arch)

    # ---- Step 4: fan out every chunk through the LOCI API concurrently ----
    api_key = api_client._load_api_key()
    if not api_key:
        sys.stderr.write(
            f"extract_and_time: no LOCI bearer token. Set ${api_client.ENV_VAR} "
            f"or add {{\"{api_client.CONFIG_KEY}\": \"...\"}} to {api_client.CONFIG_PATH}.\n"
        )
        return 3
    session_id = api_client._mcp_initialize(api_key, args.timeout)  # exits 4/5/6 on failure

    manifest: dict = {
        "basename": args.basename,
        "functions": args.functions or "(all)",
        "arch": args.arch,
        "api_arch": api_arch,
        "added": args.added,
        "out_dir": str(out_dir),
    }
    manifest["post"] = _process_side(
        "post", post_extract, out_dir, args.basename, fn_tag,
        api_arch, api_key, session_id, args.jobs, args.timeout,
    )
    if pre_extract is not None:
        manifest["pre"] = _process_side(
            "pre", pre_extract, out_dir, args.basename, fn_tag,
            api_arch, api_key, session_id, args.jobs, args.timeout,
        )

    json.dump(manifest, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
