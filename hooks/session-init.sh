#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# LOCI plugin — automatic setup & session initializer
# ──────────────────────────────────────────────────────────────────────────────
# Runs at every SessionStart via hooks/hooks.json.
#
# First run  : installs deps → creates venv → detects project       (~20-40 s)
# After that : re-detects project and refreshes context              (< 2 s)
#
# ALWAYS exits 0 — a failing hook must never block a session.
# Works on Linux, macOS, and Windows (MSYS2/Git Bash).
# ──────────────────────────────────────────────────────────────────────────────

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Persistent state lives in the project working directory so that all LOCI
# artifacts (measurements, stats, cursor, logs, project-context, loci-paths)
# stay with the project being analyzed. This also survives plugin upgrades —
# the project dir is never wiped on a version bump.
#
# NOTE: this exported LOCI_STATE_DIR does NOT propagate to later hooks or to
# skill-invoked Bash calls (Claude Code does not persist hook env). Those
# consumers resolve the SAME path from their own default of <cwd>/.loci/state
# (cwd is always the project root). Keep the two in sync — see
# lib/loci_stats.py _resolve_state_dir() and the other lib defaults.
#
# Fall back to ~/.loci/state, then the plugin dir, only if the project dir
# isn't writable (e.g. a read-only checkout).
STATE_DIR="$(pwd)/.loci/state"
if mkdir -p "$STATE_DIR" 2>/dev/null; then
    # Shield the user's repo: never surface LOCI's runtime writes in git.
    [ -f "$STATE_DIR/.gitignore" ] || printf '*\n' > "$STATE_DIR/.gitignore" 2>/dev/null
else
    STATE_DIR="${HOME}/.loci/state"
    if ! mkdir -p "$STATE_DIR" 2>/dev/null; then
        STATE_DIR="${PLUGIN_DIR}/state"
    fi
fi
export LOCI_STATE_DIR="$STATE_DIR"

# The venv lives outside the versioned plugin dir for the same reason — a
# version bump (0.1.65 → 0.1.66) with unchanged requirements.txt must NOT
# force a full reinstall. The setup marker stores a fingerprint of
# requirements.txt; matching fingerprint + a healthy 3.12 interpreter is
# what makes the venv "ready", not the plugin version number.
# LOCI_VENV_DIR is exported so PreToolUse/PostToolUse/Stop hooks and the
# Python entry points (asm_analyze.py, build_metadata.py) can resolve the
# same location without re-deriving it.
VENV_DIR="${HOME}/.loci/venv"
if ! mkdir -p "$(dirname "$VENV_DIR")" 2>/dev/null; then
    VENV_DIR="${PLUGIN_DIR}/.venv"
fi
SETUP_MARKER="${VENV_DIR}/.setup-complete"
export LOCI_VENV_DIR="$VENV_DIR"

IS_WINDOWS=false
[[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]] && IS_WINDOWS=true

# Force UTF-8 for every Python process the plugin launches. Windows consoles
# default to cp1252, which can't encode the Unicode characters LOCI emits
# (→, ·, µ, ↳, ⚠, ✗, ✅). Setting this env var is the one cross-platform
# knob that survives every subprocess layer — safer than relying on
# sys.stdout.reconfigure() alone.
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

# ── 1. PATH augmentation ─────────────────────────────────────────────────────
# Hook sub-processes don't inherit the login-shell PATH.  Prepend every common
# location where user-installed tools (uv, jq, Python, brew, etc.) live.
for _d in \
    "$HOME/.local/bin" \
    "$HOME/.cargo/bin" \
    "/usr/local/bin" \
    "/opt/homebrew/bin" \
    "/opt/homebrew/opt/binutils/bin"; do
    [ -d "$_d" ] && case ":$PATH:" in *":$_d:"*) ;; *) PATH="$_d:$PATH" ;; esac
done
if $IS_WINDOWS; then
    for _d in \
        "${LOCALAPPDATA:-$HOME/AppData/Local}/uv/bin" \
        "/mingw64/bin" "/ucrt64/bin" "/usr/bin"; do
        [ -d "$_d" ] && case ":$PATH:" in *":$_d:"*) ;; *) PATH="$_d:$PATH" ;; esac
    done
fi
export PATH

# Shared logger (file-only, opt-in via LOCI_LOG_LEVEL). Sourced after PATH
# augmentation but before any work, so every checkpoint below is captured.
# shellcheck source=../lib/loci_log.sh
. "${PLUGIN_DIR}/lib/loci_log.sh" 2>/dev/null || true
loci_log INFO session-init "start: SessionStart hook (cwd=$(pwd) state_dir=$STATE_DIR)"

# ── 2. Helpers ────────────────────────────────────────────────────────────────

_venv_python() {
    if   [ -x "${VENV_DIR}/bin/python" ];        then echo "${VENV_DIR}/bin/python"
    elif [ -x "${VENV_DIR}/Scripts/python.exe" ]; then echo "${VENV_DIR}/Scripts/python.exe"
    else echo "python3"; fi
}

# Canonical key for the current directory, hashed into cwd_hash.
# Uses device:inode so case-variant paths on case-insensitive filesystems
# (e.g. /aurora/BLE vs /aurora/bLE on macOS APFS) and symlinks collapse to
# the same key — without this, state files split across multiple namespaces
# and skills like /loci:trends silently miss prior measurements.
_canonical_cwd_key() {
    local key
    key=$(stat -f '%d:%i' . 2>/dev/null) && [ -n "$key" ] && { printf '%s' "$key"; return 0; }
    key=$(stat -c '%d:%i' . 2>/dev/null) && [ -n "$key" ] && { printf '%s' "$key"; return 0; }
    realpath . 2>/dev/null || pwd
}

_hash_cwd() {
    local key h
    key=$(_canonical_cwd_key)
    h=$(printf '%s' "$key" | sha256sum 2>/dev/null | cut -c1-12)
    [ -n "$h" ] && { echo "$h"; return 0; }
    h=$(printf '%s' "$key" | shasum -a 256 2>/dev/null | cut -c1-12)
    [ -n "$h" ] && { echo "$h"; return 0; }
    printf '%s' "$key" | cksum | awk '{print $1}'
}

# One-shot migration: rename state files keyed by any pre-fix hash to the
# canonical (device:inode) hash. Discovers candidates by walking
# project-context-*.json and resolving each file's project_root to an inode;
# anything that resolves to the same inode as the current cwd belongs to us
# regardless of which path-spelling produced its legacy hash. Idempotent —
# only renames when a canonical sibling does not already exist.
_inode_key() {
    stat -f '%d:%i' "$1" 2>/dev/null || stat -c '%d:%i' "$1" 2>/dev/null
}

_migrate_legacy_state() {
    local new_hash="$1" slug="$2"
    local current_inode; current_inode=$(_inode_key .)
    [ -z "$current_inode" ] && return 0
    local ctx_file legacy_hash old_root old_inode f new_f
    for ctx_file in "${STATE_DIR}"/project-context-*.json; do
        [ -f "$ctx_file" ] || continue
        legacy_hash=$(basename "$ctx_file" .json)
        legacy_hash="${legacy_hash#project-context-}"
        case "$legacy_hash" in *[!a-f0-9]*) continue;; esac
        [ "$legacy_hash" = "$new_hash" ] && continue
        old_root=$("$JQ" -r '.project_root // empty' "$ctx_file" 2>/dev/null)
        [ -z "$old_root" ] && continue
        old_inode=$(_inode_key "$old_root")
        [ "$old_inode" = "$current_inode" ] || continue
        for f in \
            "${STATE_DIR}/project-context-${legacy_hash}.json" \
            "${STATE_DIR}/loci-measurements-${legacy_hash}-${slug}.jsonl" \
            "${STATE_DIR}/loci-stats-${legacy_hash}-${slug}.json"
        do
            new_f="${f//${legacy_hash}/${new_hash}}"
            [ -f "$f" ] && [ ! -e "$new_f" ] && mv -f "$f" "$new_f" 2>/dev/null
        done
    done
}

_git_branch() {
    git -C "$(pwd)" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown"
}

_branch_slug() {
    printf '%s' "$1" | tr '/' '_' | tr -cd 'A-Za-z0-9_-' | cut -c1-64
}

_plugin_version() {
    local jq_bin="$1" dir="${2:-$PLUGIN_DIR}"
    "$jq_bin" -r '.version // "0"' \
        "${dir}/.claude-plugin/plugin.json" 2>/dev/null || echo "0"
}

# Return 0 if dotted-numeric version $1 is strictly greater than $2. Pads
# missing segments with 0 so "0.1" and "0.1.0" compare equal. Pure bash so
# we don't depend on sort -V (BSD sort before macOS 10.13 lacks it).
_semver_gt() {
    local a b IFS=.
    # shellcheck disable=SC2206
    a=($1)
    # shellcheck disable=SC2206
    b=($2)
    local n=${#a[@]} m=${#b[@]} i
    [ "$m" -gt "$n" ] && n=$m
    for (( i=0; i<n; i++ )); do
        local x=${a[i]:-0} y=${b[i]:-0}
        # Reject non-numeric segments — caller already filters but guard anyway
        case "$x$y" in *[!0-9]*) return 1;; esac
        if   [ "$x" -gt "$y" ]; then return 0
        elif [ "$x" -lt "$y" ]; then return 1
        fi
    done
    return 1
}

# Resolve the authoritative plugin dir for paths emitted in the session
# context. The hook captures PLUGIN_DIR from $0 at launch — that path is
# stale if Claude Code launched a previous CLAUDE_PLUGIN_ROOT or if an
# in-flight upgrade deletes this version's cache dir between session start
# and the first tool call. Scan the cache root for the highest-semver
# version that still has both .claude-plugin/plugin.json and lib/; that's
# the dir whose lib/asm_analyze.py and lib/build_metadata.py survive the
# upgrade. Fall back to PLUGIN_DIR when the cache layout doesn't match
# (dev install from source, test sandbox without sibling versions).
_resolve_authoritative_plugin_dir() {
    local cache_root; cache_root="$(dirname "$PLUGIN_DIR")"
    [ -d "$cache_root" ] || { printf '%s' "$PLUGIN_DIR"; return; }
    local d ver best_dir="" best_ver=""
    for d in "$cache_root"/*/; do
        [ -d "$d" ] || continue
        ver="${d%/}"; ver="${ver##*/}"
        # Dir name must be dotted-numeric (semver-ish) — skip "current",
        # tarballs, hidden files, anything with letters or dashes.
        case "$ver" in ''|*[!0-9.]*) continue;; esac
        [ -f "${d}.claude-plugin/plugin.json" ] || continue
        [ -d "${d}lib" ] || continue
        if [ -z "$best_ver" ] || _semver_gt "$ver" "$best_ver"; then
            best_ver="$ver"; best_dir="${d%/}"
        fi
    done
    if [ -n "$best_dir" ]; then printf '%s' "$best_dir"
    else printf '%s' "$PLUGIN_DIR"
    fi
}

# ── 3. Locate / auto-install jq ──────────────────────────────────────────────

_find_jq() {
    for _c in jq /usr/bin/jq /usr/local/bin/jq /opt/homebrew/bin/jq \
              "$HOME/.local/bin/jq"; do
        if command -v "$_c" >/dev/null 2>&1; then echo "$_c"; return 0; fi
        [ "$_c" != jq ] && [ -x "$_c" ] && { echo "$_c"; return 0; }
    done
    return 1
}

_install_jq() {
    printf 'LOCI: installing jq...\n'
    if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
        HOMEBREW_NO_AUTO_UPDATE=1 brew install jq >/dev/null 2>&1
    elif $IS_WINDOWS && command -v pacman >/dev/null 2>&1; then
        pacman -S --noconfirm jq >/dev/null 2>&1
    elif command -v apt-get >/dev/null 2>&1; then
        sudo -n apt-get install -y jq >/dev/null 2>&1   # -n = non-interactive
    elif command -v dnf >/dev/null 2>&1; then
        sudo -n dnf install -y jq >/dev/null 2>&1
    fi
    _find_jq   # re-check
}

loci_log INFO session-init "start: jq detection"
JQ=$(_find_jq) || JQ=$(_install_jq) || {
    loci_log ERROR session-init "jq not found"
    printf 'LOCI: jq not found — install with: apt-get install jq  or  brew install jq\n' >&2
    exit 0
}
loci_log INFO session-init "end: jq detection (path=$JQ)"

mkdir -p "$STATE_DIR" 2>/dev/null || exit 0

# ── 4. First-time setup ──────────────────────────────────────────────────────
# Guarded by a version-stamped marker.  Runs once after install or plugin
# upgrade; skipped entirely on subsequent sessions.

_detect_cxxfilt() {
    # Find a working c++filt binary that supports `-r` (Itanium-ABI demangling).
    #
    # Returns a directory that contains an executable named exactly `c++filt`
    # (or `c++filt.exe`/`c++filt.cmd` on Windows). The asmslicer wheel calls
    # `shutil.which('c++filt')` with that hardcoded name, so vendor-prefixed
    # binaries (`llvm-cxxfilt`, `arm-none-eabi-c++filt`, …) are wrapped in a
    # tiny shim under ~/.loci/bin/ that forwards to the real binary.
    #
    # Search order:
    #   1. plain `c++filt` on PATH (fast path)
    #   2. plain `c++filt` in known toolchain dirs
    #   3. vendor-prefixed binaries in TI / ARM / mcuxpresso / Homebrew dirs
    #
    # Stays silent on stdout — only the resolved dir (or nothing) is echoed.

    local plain_names=("c++filt")
    local vendor_names=(
        "llvm-cxxfilt"
        "arm-none-eabi-c++filt"
        "aarch64-none-elf-c++filt"
        "aarch64-linux-gnu-c++filt"
        "riscv32-unknown-elf-c++filt"
    )

    local plain_dirs=()
    local vendor_dirs=()
    if $IS_WINDOWS; then
        plain_dirs+=(/mingw64/bin /mingw32/bin /ucrt64/bin /usr/bin)
        for d in /c/ti/gcc-arm-none-eabi/bin \
                 /c/ti/ti-cgt-armllvm_*/bin \
                 /c/ti/ccs*/tools/compiler/ti-cgt-armllvm_*/bin \
                 "/c/Program Files/GNU Arm Embedded Toolchain"*/bin \
                 "/c/Program Files (x86)/GNU Arm Embedded Toolchain"*/bin \
                 "/c/Program Files/Arm/GNU Toolchain mingw-w64-x86_64-"*/bin \
                 "$HOME/.mcuxpressotools/arm-gnu-toolchain-"*/bin \
                 "$HOME/.mcuxpressotools/"*/bin; do
            [ -d "$d" ] && vendor_dirs+=("$d")
        done
    else
        plain_dirs+=(
            /opt/homebrew/opt/binutils/bin /usr/local/opt/binutils/bin
            /usr/bin /usr/local/bin
        )
        for d in "$HOME/.mcuxpressotools/arm-gnu-toolchain-"*/bin \
                 /opt/arm-gnu-toolchain-*/bin \
                 /opt/gcc-arm-none-eabi-*/bin; do
            [ -d "$d" ] && vendor_dirs+=("$d")
        done
    fi
    local cur; cur="$(command -v c++filt 2>/dev/null)"
    [ -n "$cur" ] && plain_dirs+=("$(dirname "$cur")")

    # Pass 1: plain c++filt anywhere known.
    local dir
    for dir in "${plain_dirs[@]}" "${vendor_dirs[@]}"; do
        for name in "${plain_names[@]}"; do
            local p="$dir/$name"
            if [ -x "$p" ] && echo "_Z3fooi" | "$p" -r >/dev/null 2>&1; then
                echo "$dir"; return 0
            fi
            # Windows: also check the .exe form.
            if $IS_WINDOWS && [ -x "$p.exe" ] \
                && echo "_Z3fooi" | "$p.exe" -r >/dev/null 2>&1; then
                echo "$dir"; return 0
            fi
        done
    done

    # Pass 2: vendor-prefixed binary → write a c++filt shim.
    local shim_dir="$STATE_DIR/bin"
    mkdir -p "$shim_dir" 2>/dev/null || true
    for dir in "${plain_dirs[@]}" "${vendor_dirs[@]}"; do
        for name in "${vendor_names[@]}"; do
            local p="$dir/$name"
            local found=""
            if [ -x "$p" ] && echo "_Z3fooi" | "$p" -r >/dev/null 2>&1; then
                found="$p"
            elif $IS_WINDOWS && [ -x "$p.exe" ] \
                && echo "_Z3fooi" | "$p.exe" -r >/dev/null 2>&1; then
                found="$p.exe"
            fi
            [ -z "$found" ] && continue

            # Write a c++filt shim that forwards all args to the real binary.
            if $IS_WINDOWS; then
                local win_target; win_target="$(cygpath -w "$found" 2>/dev/null || echo "$found")"
                printf '@echo off\r\n"%s" %%*\r\n' "$win_target" > "$shim_dir/c++filt.cmd"
            else
                printf '#!/bin/sh\nexec "%s" "$@"\n' "$found" > "$shim_dir/c++filt"
                chmod +x "$shim_dir/c++filt"
            fi
            echo "$shim_dir"; return 0
        done
    done

    # Pass 3: TI tiarmdem (LLVM symbol-undecoration tool bundled with ticlang).
    # CLI is not c++filt-compatible — rejects `-r`/`-p` and prints to stdout
    # without prompts — so the shim filters those flags out before forwarding.
    # Demangles Itanium-ABI symbols from stdin line-by-line, which is enough
    # for asmslicer's invocation pattern.
    local tiarmdem_dirs=()
    if $IS_WINDOWS; then
        for d in /c/ti/ticlang/bin \
                 /c/ti/ti-cgt-armllvm_*/bin \
                 /c/ti/ccs*/tools/compiler/ti-cgt-armllvm_*/bin \
                 "/c/Program Files/Texas Instruments/ti-cgt-armllvm_"*/bin \
                 "/c/Program Files (x86)/Texas Instruments/ti-cgt-armllvm_"*/bin; do
            [ -d "$d" ] && tiarmdem_dirs+=("$d")
        done
    fi
    for dir in "${tiarmdem_dirs[@]}"; do
        local td="$dir/tiarmdem.exe"
        [ -x "$dir/tiarmdem" ] && td="$dir/tiarmdem"
        if [ -x "$td" ] && echo "_Z3fooi" | "$td" 2>/dev/null | grep -q "foo(int)"; then
            local win_target
            win_target="$(cygpath -w "$td" 2>/dev/null || echo "$td")"
            # Shim strips `-r`/`-p` flags asmslicer passes — tiarmdem doesn't
            # know them — and forwards the rest. Stdin/stdout pass through.
            cat <<EOF > "$shim_dir/c++filt.cmd"
@echo off
setlocal enabledelayedexpansion
set "ARGS="
:loop
if "%~1"=="" goto :run
if /i "%~1"=="-r" (shift & goto :loop)
if /i "%~1"=="-p" (shift & goto :loop)
set "ARGS=!ARGS! "%~1""
shift
goto :loop
:run
"$win_target" !ARGS!
EOF
            echo "$shim_dir"; return 0
        fi
    done

    return 1
}

_install_uv() {
    command -v uv >/dev/null 2>&1 && return 0
    printf 'LOCI: installing uv...\n'
    if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
        HOMEBREW_NO_AUTO_UPDATE=1 brew install uv >/dev/null 2>&1
    elif $IS_WINDOWS; then
        if command -v winget >/dev/null 2>&1; then
            winget install --accept-package-agreements --accept-source-agreements astral-sh.uv \
                >/dev/null 2>&1
        elif command -v scoop >/dev/null 2>&1; then
            scoop install uv >/dev/null 2>&1
        else
            powershell -ExecutionPolicy ByPass -c \
                "irm https://astral.sh/uv/install.ps1 | iex" >/dev/null 2>&1
        fi
        export PATH="${LOCALAPPDATA:-$HOME/AppData/Local}/uv/bin:$HOME/.cargo/bin:$PATH"
    else
        curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | sh >/dev/null 2>&1
        export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    fi
    command -v uv >/dev/null 2>&1
}

_setup_venv() {
    # Fast-path: venv already valid AND running Python 3.12?
    local vpy; vpy=$(_venv_python)
    if [ -x "$vpy" ]; then
        local _pyver; _pyver=$("$vpy" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        if [ "$_pyver" = "3.12" ] \
            && "$vpy" -c "from loci.service.asmslicer import asmslicer" 2>/dev/null; then
            return 0
        fi
        # Wrong Python version or missing deps — nuke and rebuild
        printf 'LOCI: venv has Python %s (need 3.12) — rebuilding...\n' "${_pyver:-unknown}" >&2
    fi

    printf 'LOCI: setting up asm-analyze environment...\n'

    # Neutralize private registries that would block on credentials
    export UV_EXTRA_INDEX_URL=""
    export UV_INDEX_URL="https://pypi.org/simple/"

    # (Re)create venv
    rm -rf "$VENV_DIR"
    uv venv --python 3.12 "$VENV_DIR" >/dev/null 2>&1 || return 1

    VIRTUAL_ENV="$VENV_DIR" uv pip install -r "${PLUGIN_DIR}/requirements.txt" \
        >/dev/null 2>&1 || return 1

    # Resolve undeclared transitive deps (up to 5 rounds)
    vpy=$(_venv_python)
    local UNIX_ONLY="resource fcntl grp pwd termios syslog"
    local _attempt MISSING
    for _attempt in 1 2 3 4 5; do
        MISSING=$("$vpy" -c "from loci.service.asmslicer import asmslicer" 2>&1 \
            | grep "ModuleNotFoundError" | head -1 \
            | sed "s/.*No module named '\([^']*\)'.*/\1/")
        [ -z "$MISSING" ] && return 0
        # Stub Unix-only stdlib modules on Windows
        if echo " $UNIX_ONLY " | grep -q " $MISSING "; then
            local sp; sp=$("$vpy" -c "import sysconfig; print(sysconfig.get_path('purelib'))")
            local stub="${PLUGIN_DIR}/setup/stubs/${MISSING}.py"
            if [ -f "$stub" ]; then cp "$stub" "${sp}/${MISSING}.py"
            else echo "# stub — ${MISSING} unavailable on this platform" > "${sp}/${MISSING}.py"
            fi
            continue
        fi
        VIRTUAL_ENV="$VENV_DIR" uv pip install "$MISSING" >/dev/null 2>&1 || return 1
    done

    "$vpy" -c "from loci.service.asmslicer import asmslicer" 2>/dev/null
}

_venv_is_ready() {
    # Fast per-session check: is the existing venv running Python 3.12 AND
    # does it still have a working asmslicer import? Returns 0 if both, 1
    # otherwise. Cheap enough (~50 ms cold) to run every session so a venv
    # that drifts from 3.12 (corruption, downgrade) OR has lost its asmslicer
    # install (uv install killed mid-flight, antivirus quarantine, manual
    # package removal) is caught without waiting for the next plugin upgrade.
    #
    # Before this check covered both, a broken venv with intact Python could
    # indefinitely pass _first_time_setup's fast-path skip — the setup probe
    # would report asm-analyze unavailable session after session, but setup
    # never re-ran because Python alone looked healthy.
    local vpy; vpy=$(_venv_python)
    [ -x "$vpy" ] || return 1
    local _pyver
    _pyver=$("$vpy" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
    [ "$_pyver" = "3.12" ] || return 1
    "$vpy" -c "from loci.service.asmslicer import asmslicer" 2>/dev/null
}

_requirements_fingerprint() {
    # Fingerprint = first 16 hex chars of sha256(requirements.txt).
    # Identifies the venv by what's installed in it, not by the plugin version,
    # so unchanged requirements across a plugin upgrade reuse the existing venv.
    local req="${PLUGIN_DIR}/requirements.txt"
    [ -f "$req" ] || { printf 'no-req'; return 0; }
    local h
    if h=$(sha256sum "$req" 2>/dev/null | cut -c1-16) && [ -n "$h" ]; then
        printf '%s' "$h"; return 0
    fi
    if h=$(shasum -a 256 "$req" 2>/dev/null | cut -c1-16) && [ -n "$h" ]; then
        printf '%s' "$h"; return 0
    fi
    printf 'hash-unavailable'
}

_first_time_setup() {
    local fp; fp=$(_requirements_fingerprint)
    # Skip ONLY when the setup marker matches AND the venv is still 3.12.
    # The re-check catches a venv that drifted from 3.12 (corruption, manual
    # downgrade) without waiting for the requirements file to change.
    if [ -f "$SETUP_MARKER" ] \
        && [ "$(cat "$SETUP_MARKER" 2>/dev/null)" = "$fp" ] \
        && _venv_is_ready; then
        return 0
    fi
    # If the marker is stale because the venv drifted, force _setup_venv to rebuild
    # (it already does on version mismatch, but clear the marker so the retry path
    # below treats this like a first-time install).
    if [ -f "$SETUP_MARKER" ] && ! _venv_is_ready; then
        rm -f "$SETUP_MARKER" 2>/dev/null
    fi

    # mkdir lock prevents parallel sessions from corrupting the venv.
    # Lock sits next to VENV_DIR (not inside it) so `rm -rf $VENV_DIR` during
    # rebuild doesn't drop the lock. Now keyed to the shared venv location so a
    # single lock arbitrates between concurrent sessions across plugin versions.
    # PID file inside the lock dir enables stale-lock cleanup after a crash.
    local lock="${VENV_DIR}.lock"
    if [ -d "$lock" ]; then
        local lpid; lpid=$(cat "${lock}/pid" 2>/dev/null || echo "")
        if [ -z "$lpid" ] || ! kill -0 "$lpid" 2>/dev/null; then
            rm -rf "$lock" 2>/dev/null      # stale lock — owner process gone
        fi
    fi
    mkdir "$lock" 2>/dev/null || return 0     # another instance is setting up
    echo $$ > "${lock}/pid" 2>/dev/null
    # shellcheck disable=SC2064
    trap "rm -rf '$lock' 2>/dev/null" EXIT

    local ver; ver=$(_plugin_version "$JQ")
    printf 'LOCI: first-time setup (v%s)...\n' "$ver"

    # ── permissions ──────────────────────────────────────────────────────
    chmod +x "${PLUGIN_DIR}/hooks/"*.sh 2>/dev/null || true
    chmod +x "${PLUGIN_DIR}/lib/"*.sh  2>/dev/null || true
    chmod +x "${PLUGIN_DIR}/lib/"*.py  2>/dev/null || true

    # ── c++filt → loci-paths.json ────────────────────────────────────────
    # asm_analyze.py runs under Windows Python on MSYS hosts, so the path
    # we persist must be in native Windows form (`C:\…`) — MSYS-style
    # `/c/...` is not understood by shutil.which. cygpath does the
    # conversion when present; fall back to a manual transform otherwise.
    # We delegate JSON escaping to jq so the resulting file is always
    # valid (backslashes in Windows paths must be doubled in JSON).
    local cxdir; cxdir=$(_detect_cxxfilt 2>/dev/null || true)
    if [ -n "$cxdir" ]; then
        local native_dir="$cxdir"
        if $IS_WINDOWS; then
            if command -v cygpath >/dev/null 2>&1; then
                native_dir="$(cygpath -w "$cxdir" 2>/dev/null || echo "$cxdir")"
            elif [[ "$cxdir" =~ ^/([a-zA-Z])/(.*)$ ]]; then
                native_dir="${BASH_REMATCH[1]^^}:\\${BASH_REMATCH[2]//\//\\}"
            fi
        fi
        "$JQ" -n --arg d "$native_dir" '{cxxfilt_dir: $d}' > "${STATE_DIR}/loci-paths.json"
    else
        printf '{"cxxfilt_dir":null}\n' > "${STATE_DIR}/loci-paths.json"
    fi

    # ── venv + asm-analyze (non-fatal) ───────────────────────────────────
    if _install_uv && _setup_venv; then
        printf 'LOCI: asm-analyze ready\n'
    else
        printf 'LOCI: asm-analyze unavailable (will retry next session)\n'
        # Don't write marker — retry on next session
        rm -rf "$lock" 2>/dev/null; trap - EXIT
        return 0
    fi

    echo "$fp" > "$SETUP_MARKER"
    printf 'LOCI: setup complete\n'
    rm -rf "$lock" 2>/dev/null; trap - EXIT
}

_welcome_text() {
    # Marker lives at ~/.loci/.welcome-shown so the one-time welcome survives
    # plugin upgrades — same pattern as VENV_DIR/STATE_DIR above. Pre-fix bug:
    # marker was at ${PLUGIN_DIR}/.welcome-shown, i.e. the versioned plugin
    # cache dir, so every version bump landed in a fresh dir with no marker
    # and re-showed the welcome banner on the next session.
    # Fall back to PLUGIN_DIR when ~/.loci isn't writable (read-only HOME).
    local marker_dir="${HOME}/.loci"
    [ -d "$marker_dir" ] || marker_dir="$PLUGIN_DIR"
    local marker="${marker_dir}/.welcome-shown"
    [ -f "$marker" ] && return 0

    cat <<'WELCOME'
LOCI is ready.

Try:
  "What's the execution cost of main()?"   → timing & energy
  "How much ROM/RAM does my build use?"    → memory report
  "Is my stack safe for TaskMain?"         → stack depth

Auto-runs during /plan and after edits — no setup needed.
Authorize the MCP server when prompted for timing/energy.
Type /help for the full rundown.
WELCOME

    touch "$marker" 2>/dev/null
}

# ── 5. Per-session project detection ──────────────────────────────────────────
# Always runs — refreshes state/project-context.json for the current cwd.

_detect_and_write_context() {
    local PROJECT_INFO
    PROJECT_INFO=$("${PLUGIN_DIR}/lib/detect-project.sh" "$(pwd)" 2>/dev/null) \
        || PROJECT_INFO='{}'
    [ -z "$PROJECT_INFO" ] && PROJECT_INFO='{}'

    local COMPILER BUILD_SYS LOCI_TARGET
    COMPILER=$( "$JQ" -r '.compiler     // "unknown"' <<< "$PROJECT_INFO" 2>/dev/null || echo unknown)
    BUILD_SYS=$("$JQ" -r '.build_system // "unknown"' <<< "$PROJECT_INFO" 2>/dev/null || echo unknown)
    LOCI_TARGET=$("$JQ" -r '.loci_target // "unknown"' <<< "$PROJECT_INFO" 2>/dev/null || echo unknown)

    local HASH; HASH=$(_hash_cwd)
    local GIT_BRANCH; GIT_BRANCH=$(_git_branch)
    local BRANCH_SLUG; BRANCH_SLUG=$(_branch_slug "$GIT_BRANCH")
    _migrate_legacy_state "$HASH" "$BRANCH_SLUG"
    local KEYED="${STATE_DIR}/project-context-${HASH}.json"
    local TMP="${KEYED}.tmp.$$"

    # Atomic write — prevents torn reads if two SessionStarts hit same CWD
    "$JQ" --arg pwd "$(pwd)" --arg branch "$GIT_BRANCH" --arg slug "$BRANCH_SLUG" --arg hash "$HASH" \
        '. + {project_root: $pwd, git_branch: $branch, branch_slug: $slug, cwd_hash: $hash}' <<< "$PROJECT_INFO" \
        > "$TMP" 2>/dev/null \
        && mv -f "$TMP" "$KEYED" 2>/dev/null \
        || { rm -f "$TMP" 2>/dev/null; return 1; }

    # Deprecated: unkeyed alias kept one release for consumers migrating to <project-context>.
    # Racy when concurrent sessions run in different projects — readers should prefer
    # the keyed file path injected into additionalContext as "project context:".
    (cd "$STATE_DIR" && ln -sf "$(basename "$KEYED")" project-context.json 2>/dev/null) \
        || cp "$KEYED" "${STATE_DIR}/project-context.json" 2>/dev/null

    # Export for JSON output
    _CTX_TARGET="$LOCI_TARGET"
    _CTX_COMPILER="$COMPILER"
    _CTX_BUILD="$BUILD_SYS"
    _CTX_BRANCH="$GIT_BRANCH"
    _CTX_PROJECT_CONTEXT="$KEYED"
}

# ── main ──────────────────────────────────────────────────────────────────────
loci_log INFO session-init "start: first-time setup"
_first_time_setup >&2      # setup logs go to stderr (not parsed as hook output)
loci_log INFO session-init "end: first-time setup"

loci_log INFO session-init "start: project detection"
_detect_and_write_context
loci_log INFO session-init "end: project detection (target=$_CTX_TARGET compiler=$_CTX_COMPILER build=$_CTX_BUILD)"

# Plugin dir / version advertised to Claude. AUTH_PLUGIN_DIR is the highest-
# semver version installed in the cache root, not necessarily this script's
# own location — see _resolve_authoritative_plugin_dir. Using $0's PLUGIN_DIR
# directly here is the bug fixed by 04-session-start-hook-stale-plugin-version:
# when Claude Code launches a stale CLAUDE_PLUGIN_ROOT after an upgrade, the
# old hook's PLUGIN_DIR points at a soon-to-be-deleted directory and the first
# tool invocation gets "No such file or directory" on the advertised paths.
AUTH_PLUGIN_DIR=$(_resolve_authoritative_plugin_dir)
_LOCI_VER=$(_plugin_version "$JQ" "$AUTH_PLUGIN_DIR")

# Resolve asm-analyze command for session context (skills use <asm-analyze-cmd>, <venv-python>, <plugin-dir>)
_VENV_PY=""
if   [ -x "${VENV_DIR}/bin/python" ];         then _VENV_PY="${VENV_DIR}/bin/python"
elif [ -x "${VENV_DIR}/Scripts/python.exe" ]; then _VENV_PY="${VENV_DIR}/Scripts/python.exe"
fi
_ASM_ANALYZE_CMD=""
_BUILD_METADATA_CMD=""
# Only advertise the CLIs when the setup marker confirms a completed install
# AND the lib scripts actually exist at the advertised path — guards against
# advertising a stale PLUGIN_DIR whose lib/ was removed by an upgrade.
if [ -n "$_VENV_PY" ] && [ -f "$SETUP_MARKER" ] \
        && [ -f "${AUTH_PLUGIN_DIR}/lib/asm_analyze.py" ] \
        && [ -f "${AUTH_PLUGIN_DIR}/lib/build_metadata.py" ]; then
    _ASM_ANALYZE_CMD="${_VENV_PY} ${AUTH_PLUGIN_DIR}/lib/asm_analyze.py"
    _BUILD_METADATA_CMD="${_VENV_PY} ${AUTH_PLUGIN_DIR}/lib/build_metadata.py"
fi

# Build additionalContext for Claude (invisible to user, injected into session)
LOCI_VOICE='LOCI voice: When presenting LOCI analysis results, adopt Aurora Labs "Proof, Not Promises" tone — numerically specific, technically confident, peer-to-peer. Add one short remark per report (max 15 words) that acknowledges the user'\''s work grounded in actual data. LOCI is a buddy that notices good engineering and flags real concerns honestly.
Positive feedback (attribute results to the user'\''s work):
- "That refactor cut worst path by 18%. Clean work."
- "Stack usage down 12% — smart move pulling that buffer off the stack."
- "3 functions, all under 200ns. This is tight code."
- "Energy per call dropped 0.8 uWs. Battery-friendly change."
- "ROM barely moved — +24 bytes. Minimal impact."
Honest concerns (constructive, with specifics):
- "Worst path grew 340ns. Might be worth looking at that snprintf on Cortex-M4."
- "Stack at 78% budget. Still passes, but getting tight."
- "Energy up 2.1 uWs per iteration — worth batching if this runs on battery."
Neutral (when results are baseline or first measurement):
- "Callees look clean. No issues."
- "First measurement recorded — this is your baseline."
Rules: Always cite numbers. Never use emoji. Never be vague ("looks good" without data). Attribute improvements to the user. Skip the remark when results are complex or the user needs raw data only. This is a presentation tone, not a persona — do not roleplay.'

if [ -n "$_ASM_ANALYZE_CMD" ]; then
    CONTEXT=$(printf 'loci version: %s\nTarget: %s, Compiler: %s, Build: %s\nLOCI target: %s\nBranch: %s\nasm-analyze command: %s\nbuild-metadata command: %s\nvenv python: %s\nplugin dir: %s\nproject context: %s\nAvailable: /help, /exec-trace, /stack-depth, /memory-report, /control-flow, /bug-report\nAuto-runs: loci-plan (in /plan), loci-post-edit (after edits)\nLOCI auto-run rules: When in /plan mode and the user describes new C/C++/Rust logic to implement, you MUST invoke the loci:loci-plan skill on existing callees before proposing edits. After any Edit/Write/MultiEdit to C/C++/Rust source files (.c,.cc,.cpp,.cxx,.h,.hpp,.hxx,.rs), you MUST invoke the loci:loci-post-edit skill immediately. These are not optional — they are required whenever LOCI is active.\nLOCI python policy: The plugin requires Python 3.12 (venv above). For any Python invocation inside a Bash call, use the `venv python` path above — never bare `python` or `python3` (on Windows that resolves to the Microsoft Store stub; on macOS it may pick a wrong version). For parsing JSON from `asm-analyze` / `build-metadata`, use `jq` — never `python -c`. Reasons: (1) the plugin emits Unicode (e.g. `→`, `─`, en-dash) in CFG text and `python -c` on Windows defaults to cp1252 stdout and crashes with UnicodeEncodeError; (2) `jq` is faster, simpler, and ships with the plugin. LOCI shell policy: All LOCI commands are POSIX shell (bash). On Windows you MUST run every LOCI command inside Git Bash (MSYS2/MINGW) — never PowerShell or cmd. Do NOT wrap commands as `powershell -Command ...`, `pwsh -c ...`, or call `bash -c \"...\"` from PowerShell: the outer shell mangles quotes, heredocs, and `$` expansion before bash ever sees them. Run ONE command per Bash call (no `;`/`&&` chaining handed to PowerShell), avoid heredocs, and use POSIX paths (`/c/Users/...`, never `C:\\Users\\...`). If you must author an intermediate `.sh` file, write it UTF-8 without BOM and with LF (never CRLF) line endings, or its shebang will fail under bash. Path policy: NEVER write intermediate files to `/tmp/`, `/var/tmp/`, or any path outside the working directory (on Windows, `/tmp/...` additionally cannot be resolved by the venv Python) — Claude Code prompts the user for permission on every out-of-project access, halting automated plan/post-edit/eval runs. Always write inside the project (e.g. `.loci-build/`) so every tool sees the same path.\n%s' \
        "$_LOCI_VER" "$_CTX_TARGET" "$_CTX_COMPILER" "$_CTX_BUILD" "$_CTX_TARGET" "$_CTX_BRANCH" \
        "$_ASM_ANALYZE_CMD" "$_BUILD_METADATA_CMD" "$_VENV_PY" "$AUTH_PLUGIN_DIR" "$_CTX_PROJECT_CONTEXT" "$LOCI_VOICE")
else
    CONTEXT=$(printf 'loci version: %s\nTarget: %s, Compiler: %s, Build: %s\nLOCI target: %s\nBranch: %s\nasm-analyze: unavailable (first-time setup running — restart after ~60 s)\nbuild-metadata: unavailable (first-time setup running — restart after ~60 s)\nvenv python: unavailable\nplugin dir: %s\nproject context: %s\nAvailable: /help, /exec-trace, /stack-depth, /memory-report, /control-flow, /bug-report\nAuto-runs: loci-plan (in /plan), loci-post-edit (after edits)\nLOCI auto-run rules: When in /plan mode and the user describes new C/C++/Rust logic to implement, you MUST invoke the loci:loci-plan skill on existing callees before proposing edits. After any Edit/Write/MultiEdit to C/C++/Rust source files (.c,.cc,.cpp,.cxx,.h,.hpp,.hxx,.rs), you MUST invoke the loci:loci-post-edit skill immediately. These are not optional — they are required whenever LOCI is active.\n%s' \
        "$_LOCI_VER" "$_CTX_TARGET" "$_CTX_COMPILER" "$_CTX_BUILD" "$_CTX_TARGET" "$_CTX_BRANCH" \
        "$AUTH_PLUGIN_DIR" "$_CTX_PROJECT_CONTEXT" "$LOCI_VOICE")
fi

# Impact-token first-time setup. Telemetry needs an HS256 bearer signed by
# the MCP server (see loci-claude-mcp's `mint_impact_token` tool). The MCP
# server is remote (streamable-http), so only Claude — which holds the live
# MCP connection — can request a token; bash hooks have no Cognito creds to
# do it themselves.
#
# We emit the mint instruction ONLY on the first-ever session (no token file
# or malformed JSON). The instruction is paired with a visible user-facing
# welcome line, scoped to a one-time setup event the user is implicitly
# consenting to by installing the plugin and authorizing the LOCI MCP server.
#
# A previous revision also emitted a *silent* re-mint instruction on every
# subsequent session, intended to keep the token's `sub` claim aligned with
# the current Cognito identity after an account switch. That pattern is
# functionally indistinguishable from a prompt injection (gag-order +
# credential write + invisible to user) and has been removed. The remaining
# stale-`sub` case (user switches Cognito accounts, restores $HOME from
# backup, or hands the machine over) is a manual recovery: delete
# ~/.loci/impact-token.json and the next session's first-time path mints a
# fresh one bound to the current MCP identity.
_IMPACT_TOKEN_FILE="$HOME/.loci/impact-token.json"
if [ ! -s "$_IMPACT_TOKEN_FILE" ] || ! "$JQ" -e '.token | type == "string" and length > 0' \
        "$_IMPACT_TOKEN_FILE" >/dev/null 2>&1; then
    CONTEXT="Set up LOCI telemetry credentials once now: call mcp__plugin_loci_loci__mint_impact_token() and write the returned JSON to $_IMPACT_TOKEN_FILE (create the parent directory if needed). Before the tool calls, tell the user in one short line: 'Setting up LOCI telemetry — usage metrics to measure your progress.' Do not echo the token value itself.
$CONTEXT"
fi

# Build visible welcome for user (one-time)
WELCOME=$(_welcome_text)

# Output JSON — Claude Code renders systemMessage visibly and injects
# additionalContext into the conversation for Claude to read.
"$JQ" -n \
    --arg ctx "$CONTEXT" \
    --arg welcome "$WELCOME" \
    '{
        hookSpecificOutput: {
            hookEventName: "SessionStart",
            additionalContext: $ctx
        }
    }
    + if ($welcome | length) > 0
      then { systemMessage: $welcome }
      else {}
      end'

loci_log INFO session-init "end: SessionStart hook"

exit 0
