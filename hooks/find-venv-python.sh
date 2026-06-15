#!/usr/bin/env bash
# Print the path to the LOCI venv Python interpreter, or exit 1 if none found.
#
# Used by PreToolUse / PostToolUse / Stop hooks and any helper that needs the
# venv python but doesn't inherit env from session-init.sh. Probes — in order:
#
#   1. $LOCI_VENV_DIR (set by session-init.sh in the current session)
#   2. ~/.loci/venv   (the shared, version-independent location — default
#                      since the upgrade-survival fix)
#   3. $PLUGIN_DIR/.venv (legacy per-version location, used by venvs from
#                          pre-fix plugin versions until the user rebuilds)
#
# The two-deep probe pattern (shared first, plugin-dir fallback) is what makes
# a plugin upgrade non-disruptive: the new version's hooks find the existing
# shared venv immediately rather than reporting "first-time setup running".

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"

candidates=()
if [ -n "${LOCI_VENV_DIR:-}" ]; then
    candidates+=(
        "${LOCI_VENV_DIR}/Scripts/python.exe"
        "${LOCI_VENV_DIR}/bin/python"
    )
fi
candidates+=(
    "${HOME}/.loci/venv/Scripts/python.exe"
    "${HOME}/.loci/venv/bin/python"
    "${PLUGIN_DIR}/.venv/Scripts/python.exe"
    "${PLUGIN_DIR}/.venv/bin/python"
)

for c in "${candidates[@]}"; do
    if [ -x "$c" ]; then
        printf '%s' "$c"
        exit 0
    fi
done

exit 1
