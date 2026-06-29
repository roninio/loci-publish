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
`"LOCI_API_KEY"`) in the project working directory. Check both:

```
test -n "$LOCI_API_KEY" \
  || jq -e 'has("LOCI_API_KEY") and (.LOCI_API_KEY | length > 0)' .loci/config.json >/dev/null 2>&1 \
  || echo "no LOCI_API_KEY"
```

If neither has a token, stop and ask the user for one:

> No `LOCI_API_KEY` found. Log in at https://app.auroralabs.com to retrieve
> your LOCI API token, then provide it. Either export it
> (`export LOCI_API_KEY=sk-loci-...`) or create `.loci/config.json` in this
> project with `{ "LOCI_API_KEY": "sk-loci-..." }`, then re-run `/init`.

Wait for the user to supply the token; do not proceed to setup without one.
Do not embed the token on any command line or write it to git. The `.loci`
folder is git-ignored by setup. Do not run setup until a token is reachable.

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
