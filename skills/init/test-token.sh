#!/usr/bin/env bash
# LOCI init — token smoke-test.
#
# Verifies that a LOCI API token actually authenticates against the LOCI
# endpoint before running setup. Resolves the token from $LOCI_API_KEY, else
# from .loci/config.json (key "LOCI_API_KEY") in the current working directory.
#
# Run from the project root. On Windows, run inside Git Bash (MSYS2/MINGW) —
# never PowerShell or cmd.
#
#   bash <plugin-dir>/skills/init/test-token.sh
#
# Exit codes:
#   0  token OK (authenticated, or quota reached but auth valid)
#   3  no token found
#   4  token rejected (401/403)
#   5  unexpected HTTP status
#   6  network/transport error
#
# Never prints the token value.

set -u

ENDPOINT="https://mcp.auroralabs.com/mcp/v1"

TOKEN="${LOCI_API_KEY:-}"
if [ -z "$TOKEN" ] && [ -f .loci/config.json ]; then
  TOKEN="$(jq -r '.LOCI_API_KEY // empty' .loci/config.json 2>/dev/null)"
fi

if [ -z "$TOKEN" ]; then
  echo "no token found (set \$LOCI_API_KEY or .loci/config.json)"
  exit 3
fi

CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$ENDPOINT" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"loci-init","version":"1"}}}' \
  2>/dev/null)"

case "$CODE" in
  2*)      echo "token OK ($CODE)";                    exit 0 ;;
  401|403) echo "token rejected ($CODE)";              exit 4 ;;
  429)     echo "token OK but quota reached ($CODE)";  exit 0 ;;
  000|"")  echo "network error";                       exit 6 ;;
  *)       echo "unexpected status $CODE";             exit 5 ;;
esac
