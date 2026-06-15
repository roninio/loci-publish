#!/bin/bash
# LOCI plugin — shared bash logger for session-init.sh and detect-project.sh.
#
# Appends checkpoint lines to $LOCI_STATE_DIR/loci.log so devs can reconstruct
# what the plugin did. Format matches Claude Code's debug-log line shape, so
# correlating by timestamp against ~/.claude/debug/<session>.txt is trivial:
#
#   2026-05-05T11:29:03.107Z [INFO] [loci.<source>] message
#
# Sourcing this file is a no-op beyond defining functions and resolving the
# log file path once. Safe to source multiple times. Disabled by default —
# set LOCI_LOG_LEVEL to one of DEBUG/INFO/WARN/ERROR to opt in. Unset or
# empty = silent.

# Idempotent guard
[ -n "${_LOCI_LOG_SOURCED:-}" ] && return 0
_LOCI_LOG_SOURCED=1

# Numeric level for filtering.
_loci_log_level_num() {
    case "${1:-}" in
        DEBUG) echo 10 ;;
        INFO)  echo 20 ;;
        WARN)  echo 30 ;;
        ERROR) echo 40 ;;
        OFF)   echo 99 ;;
        *)     echo 20 ;;
    esac
}

# Resolve log destination ONCE at source time. Per-call resolution costs a
# stat + mkdir, which on Git Bash on Windows is ~50ms — multiplied by ~50
# log calls per SessionStart, the per-call check dominated logger overhead.
# Same reasoning for the rotation check.
_LOCI_LOG_FILE=""
_LOCI_LOG_THRESHOLD=99
if [ -n "${LOCI_LOG_LEVEL:-}" ]; then
    _LOCI_LOG_THRESHOLD=$(_loci_log_level_num "$LOCI_LOG_LEVEL")
    _loci_dir="${LOCI_STATE_DIR:-${HOME}/.loci/state}"
    if mkdir -p "$_loci_dir" 2>/dev/null; then
        _LOCI_LOG_FILE="$_loci_dir/loci.log"
        # One-shot rotation at source time. Truncate to last 1MB if file
        # exceeds 5MB. Per-call rotation was ~50ms × 50 calls = 2.5s
        # SessionStart penalty.
        if [ -f "$_LOCI_LOG_FILE" ]; then
            _loci_size=$(wc -c < "$_LOCI_LOG_FILE" 2>/dev/null | tr -d '[:space:]')
            if [ -n "$_loci_size" ] && [ "$_loci_size" -gt 5242880 ]; then
                tail -c 1048576 "$_LOCI_LOG_FILE" > "${_LOCI_LOG_FILE}.tmp" 2>/dev/null \
                    && mv -f "${_LOCI_LOG_FILE}.tmp" "$_LOCI_LOG_FILE" 2>/dev/null
            fi
            unset _loci_size
        fi
    fi
    unset _loci_dir
fi

# Public API: loci_log <LEVEL> <source-tag> <message...>
# Example: loci_log INFO session-init "jq detection: found at /usr/bin/jq"
loci_log() {
    # Disabled when LOCI_LOG_LEVEL is unset or path resolution failed.
    [ -z "$_LOCI_LOG_FILE" ] && return 0
    local level="${1:-INFO}"; shift || true
    local source="${1:-loci}"; shift || true
    local cur_n
    cur_n=$(_loci_log_level_num "$level")
    [ "$cur_n" -lt "$_LOCI_LOG_THRESHOLD" ] && return 0

    # Timestamp without subprocess spawn. EPOCHREALTIME is bash 5+ and gives
    # microsecond precision; %(...)T is a bash 4.2+ printf builtin honouring
    # the TZ assignment prefix. Fall back to a single date spawn on older
    # bash. On Git Bash on Windows this saves ~40ms per call.
    local ts
    if [ -n "${EPOCHREALTIME:-}" ]; then
        local _epoch_int="${EPOCHREALTIME%.*}"
        local _epoch_ms="${EPOCHREALTIME#*.}"
        TZ=UTC printf -v ts '%(%Y-%m-%dT%H:%M:%S)T' "$_epoch_int"
        ts="${ts}.${_epoch_ms:0:3}Z"
    else
        ts=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ" 2>/dev/null)
        case "$ts" in *3N*) ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ") ;; esac
    fi

    printf '%s [%s] [loci.%s] %s\n' "$ts" "$level" "$source" "$*" \
        >> "$_LOCI_LOG_FILE" 2>/dev/null || true
}

# Convenience: time a block and log start/end. Usage:
#   loci_log_around session-init "venv check" _venv_is_py312
loci_log_around() {
    local source="$1"; shift
    local label="$1"; shift
    loci_log INFO "$source" "start: $label"
    "$@"
    local rc=$?
    loci_log INFO "$source" "end: $label (rc=$rc)"
    return $rc
}
