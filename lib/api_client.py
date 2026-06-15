#!/usr/bin/env python3
"""
LOCI HTTP API client.

Sends compiled-assembly CSV chunks to the LOCI service and returns timing CSV
for downstream parsing (jq, awk, and the skill's per-row reading).

Protocol
--------
  1. POST initialize  → receives Mcp-Session-Id header
  2. POST tools/call  (name=get_assembly_block_exec_behavior) with that session
  Both calls go to: https://mcp.auroralabs.com/mcp/v1

Auth
----
  Authorization: Bearer <LOCI_API_KEY>   (required)

  Resolution order, first hit wins:
    1. `$LOCI_API_KEY` environment variable
    2. `.loci/config.json` in the current working directory, key
       `"LOCI_API_KEY"` (string). Example file:
           { "LOCI_API_KEY": "sk-loci-..." }
    3. `.loci/setup.json` in the current working directory, key
       `"LOCI_API_KEY"` (string).

  If none is set, the helper exits 3 and prints a message naming both
  locations so the user can pick whichever fits their workflow.

Request arguments (tools/call)
-------------------------------
  csv_text:     "<one chunk from extract-assembly.timing_csv_chunks[]>"
  architecture: "<value of extract-assembly.timing_architecture>"

Response
--------
  text/csv, columns exactly: function_name,std_dev_ns,execution_time_ns,energy_ws
  Streamed verbatim to stdout so the caller can pipe to jq/awk/files.

Exit codes
----------
  0   success — CSV on stdout
  2   bad invocation (missing flag, empty stdin, unreadable file)
  3   no LOCI_API_KEY found
  4   HTTP error (non-2xx); body is on stderr, status code is the suffix
  5   quota / rate-limit (HTTP 429); body is on stderr verbatim
  6   network/transport error (DNS, TLS, connection reset, timeout)

CLI
---
  exec-behavior --architecture <arch>   (reads CSV from stdin by default)
  exec-behavior --architecture <arch>   --csv-file <path>

Examples
--------
  # One chunk from the skill, via stdin:
  jq -c '.timing_csv_chunks[]' .loci-build/extract.json | while read -r CHUNK; do
    echo "$CHUNK" | <venv-python> <plugin-dir>/lib/api_client.py exec-behavior \
        --architecture A53
  done

  # From a file inside the working directory:
  <venv-python> <plugin-dir>/lib/api_client.py exec-behavior \
      --architecture CortexM4 --csv-file .loci-build/chunk_0.csv
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

MCP_ENDPOINT = "https://mcp.auroralabs.com/mcp/v1"
TOOL_NAME = "get_assembly_block_exec_behavior"
ENV_VAR = "LOCI_API_KEY"
CONFIG_PATH = Path(".loci/config.json")   # resolved relative to cwd
SETUP_PATH  = Path(".loci/setup.json")    # fallback used by session-init
CONFIG_KEY = "LOCI_API_KEY"
DEFAULT_TIMEOUT_S = 60


def _load_api_key() -> str:
    """Resolve the bearer token: env → .loci/config.json → .loci/setup.json."""
    env_val = os.environ.get(ENV_VAR, "").strip()
    if env_val:
        return env_val

    for candidate in (CONFIG_PATH, SETUP_PATH):
        if candidate.is_file():
            try:
                with candidate.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                print(
                    f"api_client: {candidate} exists but could not be read: {e}",
                    file=sys.stderr,
                )
                continue
            val = data.get(CONFIG_KEY, "")
            if isinstance(val, str) and val.strip():
                return val.strip()

    return ""


def _read_csv(args: argparse.Namespace) -> str:
    """Read CSV text from --csv-file or stdin.

    The skill pipes chunks as ``jq -c '.timing_csv_chunks[]'`` which outputs
    each chunk as a JSON-encoded string (outer quotes, escaped newlines). We
    transparently unwrap that encoding so both ``echo "$CHUNK" | ...`` and
    ``jq -r ... | ...`` work correctly.
    """
    if args.csv_file:
        with open(args.csv_file, "r", encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()

    if not raw or not raw.strip():
        print("api_client: empty CSV on stdin", file=sys.stderr)
        sys.exit(2)

    stripped = raw.strip()
    # If the input is a JSON-encoded string (from jq -c), unwrap it.
    if stripped.startswith('"') and stripped.endswith('"'):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return raw


def _mcp_request(payload: dict, api_key: str, timeout: int,
                 session_id: str | None = None) -> tuple[int, bytes, dict]:
    """POST a single MCP JSON-RPC 2.0 message. Returns (status, raw_body, headers)."""
    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(MCP_ENDPOINT, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {}
    except urllib.error.URLError as e:
        print(f"api_client: transport error: {e.reason}", file=sys.stderr)
        sys.exit(6)
    except TimeoutError:
        print(f"api_client: request timed out after {timeout}s", file=sys.stderr)
        sys.exit(6)


def _parse_sse(raw: bytes) -> str:
    """Extract the JSON payload from an SSE response (data: <json> lines)."""
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data:"):
            return line[len("data:"):].strip()
    return raw.decode("utf-8", errors="replace")


def _mcp_initialize(api_key: str, timeout: int) -> str:
    """Send MCP initialize and return the Mcp-Session-Id."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "api_client.py", "version": "2.0"},
        },
    }
    status, body, headers = _mcp_request(payload, api_key, timeout)
    if status == 429:
        sys.stderr.write(body.decode("utf-8", errors="replace"))
        sys.exit(5)
    if not (200 <= status < 300):
        sys.stderr.write(f"api_client: HTTP {status} on initialize\n")
        sys.stderr.buffer.write(body)
        sys.exit(4)
    # Session ID is in the response header (case-insensitive)
    session_id = ""
    for k, v in headers.items():
        if k.lower() == "mcp-session-id":
            session_id = v.strip()
            break
    if not session_id:
        sys.stderr.write("api_client: MCP initialize succeeded but no Mcp-Session-Id in response\n")
        sys.exit(4)
    return session_id


def _mcp_tools_call(csv_text: str, architecture: str, api_key: str,
                    session_id: str, timeout: int) -> tuple[int, bytes]:
    """Call get_assembly_block_exec_behavior via MCP tools/call."""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": TOOL_NAME,
            "arguments": {"csv_text": csv_text, "architecture": architecture},
        },
    }
    status, body, _ = _mcp_request(payload, api_key, timeout, session_id=session_id)
    return status, body


def cmd_exec_behavior(args: argparse.Namespace) -> int:
    api_key = _load_api_key()
    if not api_key:
        print(
            f"api_client: no LOCI bearer token found. Set ${ENV_VAR} "
            f"or add {{\"{CONFIG_KEY}\": \"...\"}} to {CONFIG_PATH} "
            "in the current working directory.",
            file=sys.stderr,
        )
        return 3

    csv_text = _read_csv(args)

    # Step 1: MCP initialize → get session ID
    session_id = _mcp_initialize(api_key, args.timeout)

    # Step 2: tools/call with the CSV chunk
    status, body = _mcp_tools_call(csv_text, args.architecture, api_key, session_id, args.timeout)

    if status == 429:
        sys.stderr.buffer.write(body)
        sys.stderr.write("\n")
        return 5

    if not (200 <= status < 300):
        sys.stderr.write(f"api_client: HTTP {status}\n")
        sys.stderr.buffer.write(body)
        sys.stderr.write("\n")
        return 4

    # Extract the CSV text from the SSE/JSON-RPC response
    raw_text = _parse_sse(body)
    try:
        rpc = json.loads(raw_text)
        csv_out = rpc["result"]["content"][0]["text"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        sys.stderr.write(f"api_client: unexpected response shape: {e}\n")
        sys.stderr.write(raw_text[:500] + "\n")
        return 4

    sys.stdout.write(csv_out)
    if not csv_out.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="api_client.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    eb = sub.add_parser(
        "exec-behavior",
        help="POST a single timing CSV chunk to get_assembly_block_exec_behavior",
    )
    eb.add_argument("--architecture", required=True,
                    help="timing_architecture from extract-assembly (A53, CortexM4, CortexM0P, TC399)")
    eb.add_argument("--csv-file",
                    help="read the CSV chunk from a file inside the working dir (e.g. .loci-build/...). "
                         "NEVER /tmp/ — Claude Code prompts on every out-of-project access. "
                         "Omit to read from stdin (default).")
    eb.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                    help=f"request timeout in seconds (default {DEFAULT_TIMEOUT_S})")
    eb.set_defaults(func=cmd_exec_behavior)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
