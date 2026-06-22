#!/bin/bash
# Generate a session summary for LOCI final analysis.
# Input: path to session JSON file

set -euo pipefail

SESSION_FILE="${1:?Usage: generate-summary.sh <session-file>}"

if [ ! -f "$SESSION_FILE" ]; then
  echo '{}' && exit 0
fi

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Persistent state — see hooks/session-init.sh for the same resolution.
# Honour LOCI_STATE_DIR if the caller set it; otherwise default to the
# project-local location (state lives with the project being analyzed),
# falling back to the home dir, then the plugin dir, if it isn't writable.
STATE_DIR="${LOCI_STATE_DIR:-$(pwd)/.loci/state}"
if [ ! -d "$STATE_DIR" ] && ! mkdir -p "$STATE_DIR" 2>/dev/null; then
    STATE_DIR="${HOME}/.loci/state"
    if [ ! -d "$STATE_DIR" ] && ! mkdir -p "$STATE_DIR" 2>/dev/null; then
        STATE_DIR="${PLUGIN_DIR}/state"
    fi
fi
LOG_FILE="${STATE_DIR}/loci-actions.log"
WARNINGS_FILE="${STATE_DIR}/loci-warnings.json"

SESSION_ID=$(jq -r '.session_id' "$SESSION_FILE")

# Count actions by type
ACTION_COUNTS='{}'
if [ -f "$LOG_FILE" ]; then
  ACTION_COUNTS=$(grep "\"session_id\":\"${SESSION_ID}\"" "$LOG_FILE" 2>/dev/null | \
    jq -s 'group_by(.action_type) | map({key: .[0].action_type, value: length}) | from_entries' 2>/dev/null || echo '{}')
fi

# Collect unique files modified
FILES_MODIFIED='[]'
if [ -f "$LOG_FILE" ]; then
  FILES_MODIFIED=$(grep "\"session_id\":\"${SESSION_ID}\"" "$LOG_FILE" 2>/dev/null | \
    jq -s '[.[].files_involved[]?] | unique' 2>/dev/null || echo '[]')
fi

# Collect warnings issued
WARNINGS='[]'
if [ -f "$WARNINGS_FILE" ]; then
  WARNINGS=$(jq '.warnings // []' "$WARNINGS_FILE" 2>/dev/null || echo '[]')
fi

# Build summary
jq -n \
  --arg sid "$SESSION_ID" \
  --argjson action_counts "$ACTION_COUNTS" \
  --argjson files_modified "$FILES_MODIFIED" \
  --argjson warnings "$WARNINGS" \
  --argjson session "$(cat "$SESSION_FILE")" \
  '{
    session_id: $sid,
    started_at: $session.started_at,
    ended_at: $session.ended_at,
    execution_context: $session.execution_context,
    action_counts: $action_counts,
    total_actions: ($action_counts | to_entries | map(.value) | add // 0),
    files_modified: $files_modified,
    files_modified_count: ($files_modified | length),
    warnings_issued: ($warnings | length),
    warnings: $warnings
  }'
