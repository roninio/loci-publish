#!/bin/bash
# Revert BLE source file changes made by eval test runs.
#
# Usage:
#   ./revert_eval_changes.sh --ble-root /path/to/ble               # revert all changed files
#   ./revert_eval_changes.sh --ble-root /path/to/ble --eval-id pf-critical-1  # revert files for one eval
#   ./revert_eval_changes.sh --ble-root /path/to/ble --dry-run     # show what would be reverted
#   LOCI_TEST_BLE_ROOT=/path/to/ble ./revert_eval_changes.sh       # env var
#
# With --eval-id: looks up the eval's prompt across all eval JSON files,
# extracts the referenced BLE source files, and reverts only those.
# Without --eval-id: reverts ALL files currently modified in the BLE git repo.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="$SCRIPT_DIR/skills"

BLE_ROOT="${LOCI_TEST_BLE_ROOT:-}"
FILTER_EVAL_ID=""
DRY_RUN=false

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ble-root)    BLE_ROOT="$2"; shift 2 ;;
    --ble-root=*)  BLE_ROOT="${1#*=}"; shift ;;
    --eval-id)     FILTER_EVAL_ID="$2"; shift 2 ;;
    --eval-id=*)   FILTER_EVAL_ID="${1#*=}"; shift ;;
    --dry-run|-n)  DRY_RUN=true; shift ;;
    -h|--help)
      head -12 "$0" | tail -11
      exit 0
      ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# ---------------------------------------------------------------------------
# Validate BLE root
# ---------------------------------------------------------------------------
if [[ -z "$BLE_ROOT" ]]; then
  echo "ERROR: BLE root not set. Use --ble-root <path> or set LOCI_TEST_BLE_ROOT."
  exit 1
fi
BLE_ROOT="$(cd "$BLE_ROOT" && pwd)"

if ! git -C "$BLE_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  echo "ERROR: $BLE_ROOT is not a git repository."
  exit 1
fi

$DRY_RUN && echo -e "${YELLOW}${BOLD}[DRY RUN]${NC} No files will be reverted.\n"

# ---------------------------------------------------------------------------
# Helper: extract BLE-relative file paths from an eval prompt string.
# Matches occurrences of $LOCI_TEST_BLE_ROOT/<path> and returns the
# relative paths (one per line).
# ---------------------------------------------------------------------------
extract_files_from_prompt() {
  local PROMPT="$1"
  echo "$PROMPT" | grep -oE '\$LOCI_TEST_BLE_ROOT/[^ ,\`"]+' \
    | sed 's|\$LOCI_TEST_BLE_ROOT/||' \
    | sort -u
}

# ---------------------------------------------------------------------------
# Helper: find an eval by id across all eval JSON files in SKILLS_DIR.
# Prints the prompt text of the first matching eval.
# ---------------------------------------------------------------------------
find_eval_prompt() {
  local TARGET_ID="$1"
  python3 - "$SKILLS_DIR" "$TARGET_ID" <<'PYEOF'
import json, os, sys

skills_dir = sys.argv[1]
target_id  = sys.argv[2]

for root, dirs, files in os.walk(skills_dir):
    for fname in files:
        if not fname.endswith('.json') or fname.endswith('.disabled'):
            continue
        fpath = os.path.join(root, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
        except Exception:
            continue
        for ev in data.get('evals', []):
            if ev.get('id') == target_id:
                print(ev.get('prompt', ''))
                sys.exit(0)

sys.exit(1)
PYEOF
}

# ---------------------------------------------------------------------------
# Mode A: revert files for a specific eval
# ---------------------------------------------------------------------------
if [[ -n "$FILTER_EVAL_ID" ]]; then
  echo -e "${BOLD}Eval:${NC} $FILTER_EVAL_ID"

  PROMPT=$(find_eval_prompt "$FILTER_EVAL_ID") || {
    echo -e "${RED}ERROR: eval id '$FILTER_EVAL_ID' not found in any eval JSON under $SKILLS_DIR${NC}"
    exit 1
  }

  mapfile -t REL_FILES < <(extract_files_from_prompt "$PROMPT")

  if [[ ${#REL_FILES[@]} -eq 0 ]]; then
    echo -e "${YELLOW}No BLE file references found in prompt for eval $FILTER_EVAL_ID.${NC}"
    exit 0
  fi

  echo -e "${BOLD}BLE root:${NC} $BLE_ROOT"
  echo ""

  REVERTED=0
  for REL in "${REL_FILES[@]}"; do
    ABS="$BLE_ROOT/$REL"
    # Check if file is actually modified in git
    if git -C "$BLE_ROOT" diff --quiet -- "$REL" 2>/dev/null; then
      echo -e "  ${CYAN}CLEAN${NC}    $REL"
    else
      echo -e "  ${YELLOW}REVERT${NC}   $REL"
      if ! $DRY_RUN; then
        git -C "$BLE_ROOT" checkout -- "$REL"
      fi
      REVERTED=$((REVERTED + 1))
    fi
  done

  echo ""
  echo -e "${BOLD}Summary:${NC} $REVERTED file(s) reverted for eval $FILTER_EVAL_ID."

# ---------------------------------------------------------------------------
# Mode B: revert ALL modified files in BLE_ROOT
# ---------------------------------------------------------------------------
else
  echo -e "${BOLD}BLE root:${NC} $BLE_ROOT"
  echo ""

  mapfile -t CHANGED < <(git -C "$BLE_ROOT" diff --name-only 2>/dev/null)

  if [[ ${#CHANGED[@]} -eq 0 ]]; then
    echo -e "${GREEN}Nothing to revert — no modified files in BLE repo.${NC}"
    exit 0
  fi

  echo -e "Modified files (${#CHANGED[@]}):"
  for F in "${CHANGED[@]}"; do
    echo -e "  ${YELLOW}REVERT${NC}   $F"
  done

  echo ""
  if ! $DRY_RUN; then
    git -C "$BLE_ROOT" checkout -- .
    echo -e "${GREEN}Done.${NC} All ${#CHANGED[@]} file(s) restored to HEAD."
  else
    echo -e "${YELLOW}[DRY RUN]${NC} Would revert ${#CHANGED[@]} file(s). Re-run without --dry-run to apply."
  fi
fi
