---
name: init
description: >
  Initialize the LOCI project and plugin: verify a LOCI token is present,
  then run the bundled setup script for the user's OS (Git Bash on Windows,
  bash on macOS/Linux). Installs all LOCI state into the project's .loci
  folder. Use when the user asks to set up, install, or initialize LOCI.
when_to_use: >
  When user asks to initialize/set up/install LOCI, prepare the plugin,
  bootstrap a new project, or types /init. Also when the environment shows
  asm-analyze unavailable, no API key, or first-time setup is incomplete.
---

# LOCI Init

One-shot project + plugin initialization. Ensures a LOCI token exists, then
runs the platform-correct setup so all LOCI artifacts land in the project's
`.loci` folder.

## Step 0: Resolve plugin dir

From the session context read `plugin dir: <path>` → `<plugin-dir>`. The
setup script is at `<plugin-dir>/setup/setup.sh`.

## Step 1: Verify a LOCI token

The token resolves from `$LOCI_API_KEY`, else from `.loci/config.json` (key
`"LOCI_API_KEY"`) in the project working directory. If the `.loci` folder
does not exist, there is no config file — go straight to asking the user for
the token. Otherwise check both sources:

```
test -d .loci \
  && { test -n "$LOCI_API_KEY" \
       || jq -e 'has("LOCI_API_KEY") and (.LOCI_API_KEY | length > 0)' .loci/config.json >/dev/null 2>&1; } \
  || echo "no LOCI_API_KEY"
```

If `.loci` is missing or neither source has a token, stop and ask the user to
paste one:

> No `LOCI_API_KEY` found. Log in at https://app.auroralabs.com to retrieve
> your LOCI API token, then paste it here and I'll save it to
> `.loci/config.json` for this project.

When the user pastes the token, write it to `.loci/config.json` for them
(create the folder if needed) — never make the user edit files by hand:

```
mkdir -p .loci && jq -n --arg k "<pasted-token>" '{LOCI_API_KEY: $k}' > .loci/config.json
```

The token helper reads `.loci/config.json` (key `"LOCI_API_KEY"`). Wait for
the user to supply the token; do not proceed to setup without one. Never echo
the token back or commit it to git. The `.loci` folder is git-ignored by
setup. Do not run setup until a token is reachable.

## Step 1b: Smoke-test the token

Once a token is ready, verify it actually authenticates before running setup.
Write a small probe script inside the project (never `/tmp`) and run it with
bash — Git Bash on Windows. It pings the LOCI endpoint's `initialize` method
and branches on the HTTP status:

```
mkdir -p .loci-build && cat > .loci-build/test-token.sh <<'EOF'
#!/usr/bin/env bash
TOKEN="${LOCI_API_KEY:-$(jq -r '.LOCI_API_KEY // empty' .loci/config.json 2>/dev/null)}"
[ -n "$TOKEN" ] || { echo "no token"; exit 3; }
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST https://mcp.auroralabs.com/mcp/v1 \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"loci-init","version":"1"}}}')
case "$CODE" in
  2*)       echo "token OK ($CODE)";        exit 0 ;;
  401|403)  echo "token rejected ($CODE)";  exit 4 ;;
  429)      echo "token OK but quota reached ($CODE)"; exit 0 ;;
  000)      echo "network error";           exit 6 ;;
  *)        echo "unexpected status $CODE";  exit 5 ;;
esac
EOF
bash .loci-build/test-token.sh
```

Branch on the result: `token OK` → continue to Step 2. `token rejected` →
the token is invalid; re-prompt the user for a fresh one from
https://app.auroralabs.com and rewrite `.loci/config.json`. `network error`
→ report it and skip the smoke-test, but still allow setup. Never print the
token itself.

## Step 2: Run setup per OS

All LOCI commands are POSIX `bash`. The install always targets `.loci` in the
project root (cwd), so run setup from the project working directory.

- **Windows** — run inside **Git Bash** (MSYS2/MINGW), never PowerShell/cmd:

  ```
  bash <plugin-dir>/setup/setup.sh
  ```

  Use POSIX paths (`/c/Users/...`, not `C:\Users\...`). One command per call,
  no chaining, no heredocs.

- **macOS / Linux**:

  ```
  bash <plugin-dir>/setup/setup.sh
  ```

Setup auto-installs `jq`, `uv`, the Python 3.12 venv, detects the compiler,
build system, and architecture, and writes project context under `.loci`.

## Step 3: Report status

Summarize the setup output: compiler, build system, architecture, source/
binary counts, and whether asm-analyze is ready. If a token was missing,
report that as the one blocking step. Tell the user to restart so the
SessionStart hook activates LOCI.
