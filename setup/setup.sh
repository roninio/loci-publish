#!/bin/bash
# LOCI MCP Plugin - C++ Setup Script

set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

echo ""
echo -e "${BLUE}=========================================${NC}"
echo -e "${BLUE}  LOCI MCP Plugin for Claude Code${NC}"
echo -e "${BLUE}  SW Execution-Aware Analysis${NC}"
echo -e "${BLUE}=========================================${NC}"
echo ""

# 1. Check dependencies
echo -n "Checking dependencies... "
_auto_install() {
  local pkg="$1"
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    brew install "$pkg"
  elif [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]; then
    # Windows: prefer non-admin installers (winget/scoop) before choco
    if command -v winget >/dev/null 2>&1; then
      winget install --accept-package-agreements --accept-source-agreements "$pkg"
    elif command -v scoop >/dev/null 2>&1; then
      scoop install "$pkg"
    elif command -v choco >/dev/null 2>&1; then
      echo -e "${YELLOW}  (choco may require elevated privileges)${NC}"
      choco install -y "$pkg"
    else
      return 1
    fi
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y "$pkg"
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y "$pkg"
  else
    return 1
  fi
}

if ! command -v jq >/dev/null 2>&1; then
  echo -e "${YELLOW}jq not found — installing...${NC}"
  if ! _auto_install jq || ! command -v jq >/dev/null 2>&1; then
    echo -e "${RED}Failed to install jq. Please install it manually.${NC}"
    exit 1
  fi
  echo -e "${GREEN}jq installed${NC}"
fi

# binutils (objdump/readelf) — only needed on Linux/macOS for optional features.
# On Windows, asm_analyze.py reads ELFs via Python (asmslicer) and does not need binutils.
if [[ "$(uname -s)" != MINGW* && "$(uname -s)" != MSYS* ]]; then
  if ! command -v objdump >/dev/null 2>&1 || ! command -v readelf >/dev/null 2>&1; then
    echo -e "${YELLOW}binutils not found — installing...${NC}"
    if ! _auto_install binutils; then
      echo -e "${YELLOW}Failed to install binutils. Some ELF analysis features may be unavailable.${NC}"
    else
      echo -e "${GREEN}binutils installed${NC}"
    fi
  fi
fi

# Detect GNU c++filt that supports -r (required by asm-analyze for symbol demangling).
# On macOS, brew installs binutils keg-only so Apple's c++filt may shadow it.
# Write the result to state/loci-paths.json so asm_analyze.py can prepend the right dir.
_detect_cxxfilt() {
  # Find a working c++filt (binary supporting -r demangling). Returns a
  # directory containing an executable named exactly `c++filt`. When only
  # a vendor-prefixed binary is found (`llvm-cxxfilt`, `arm-none-eabi-c++filt`,
  # etc.), writes a tiny shim under ~/.loci/state/bin/c++filt[.cmd] that
  # forwards to the real binary, since loci-service-asmslicer hardcodes
  # `shutil.which('c++filt')`.
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
  local IS_WIN=false
  if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]; then
    IS_WIN=true
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

  local dir name p
  for dir in "${plain_dirs[@]}" "${vendor_dirs[@]}"; do
    for name in "${plain_names[@]}"; do
      p="$dir/$name"
      if [ -x "$p" ] && echo "_Z3fooi" | "$p" -r >/dev/null 2>&1; then
        echo "$dir"; return 0
      fi
      if $IS_WIN && [ -x "$p.exe" ] \
          && echo "_Z3fooi" | "$p.exe" -r >/dev/null 2>&1; then
        echo "$dir"; return 0
      fi
    done
  done

  local shim_dir="${HOME}/.loci/state/bin"
  mkdir -p "$shim_dir" 2>/dev/null || true
  for dir in "${plain_dirs[@]}" "${vendor_dirs[@]}"; do
    for name in "${vendor_names[@]}"; do
      p="$dir/$name"
      local found=""
      if [ -x "$p" ] && echo "_Z3fooi" | "$p" -r >/dev/null 2>&1; then
        found="$p"
      elif $IS_WIN && [ -x "$p.exe" ] \
          && echo "_Z3fooi" | "$p.exe" -r >/dev/null 2>&1; then
        found="$p.exe"
      fi
      [ -z "$found" ] && continue

      if $IS_WIN; then
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
  # Not c++filt-compatible — rejects `-r`/`-p` — so the shim filters them out
  # before forwarding stdin/stdout. Itanium-ABI demangling works fine.
  local tiarmdem_dirs=()
  if $IS_WIN; then
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
      local win_target; win_target="$(cygpath -w "$td" 2>/dev/null || echo "$td")"
      cat > "$shim_dir/c++filt.cmd" <<EOF
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
CXXFILT_DIR="$(_detect_cxxfilt 2>/dev/null || true)"

# Write c++filt path to state so asm_analyze.py can prepend the right directory.
# Convert MSYS-style path to Windows form; jq handles JSON escaping.
mkdir -p "${PLUGIN_DIR}/state"
if [ -n "$CXXFILT_DIR" ]; then
  CXXFILT_NATIVE="$CXXFILT_DIR"
  if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]] && command -v cygpath >/dev/null 2>&1; then
    CXXFILT_NATIVE="$(cygpath -w "$CXXFILT_DIR" 2>/dev/null || echo "$CXXFILT_DIR")"
  fi
  if command -v jq >/dev/null 2>&1; then
    jq -n --arg d "$CXXFILT_NATIVE" '{cxxfilt_dir: $d}' > "${PLUGIN_DIR}/state/loci-paths.json"
  else
    # Defense in depth — jq is normally required, but if it's missing the
    # printf fallback must still emit valid JSON. Backslashes in Windows
    # paths are doubled; embedded quotes (very unlikely but defensive)
    # are escaped too.
    _escaped="${CXXFILT_NATIVE//\\/\\\\}"
    _escaped="${_escaped//\"/\\\"}"
    printf '{"cxxfilt_dir":"%s"}\n' "$_escaped" > "${PLUGIN_DIR}/state/loci-paths.json"
    unset _escaped
  fi
else
  printf '{"cxxfilt_dir":null}\n' > "${PLUGIN_DIR}/state/loci-paths.json"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo -e "${YELLOW}uv not found — installing...${NC}"
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    brew install uv
  elif [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]; then
    if command -v winget >/dev/null 2>&1; then
      winget install --id=astral-sh.uv --accept-package-agreements --accept-source-agreements
    elif command -v choco >/dev/null 2>&1; then
      choco install -y uv
    else
      powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    fi
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$LOCALAPPDATA/uv/bin:$PATH"
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo -e "${RED}Failed to install uv. Please install it manually.${NC}"
    exit 1
  fi
  echo -e "${GREEN}uv installed${NC}"
fi

echo -e "${GREEN}OK${NC}"

# 2. Check C++ toolchain (including vendor/embedded compilers)
echo -n "Checking C++ compiler... "
_found_compiler=""
if command -v g++ >/dev/null 2>&1; then
  _found_compiler="g++ $(g++ --version | head -1)"
elif command -v clang++ >/dev/null 2>&1; then
  _found_compiler="clang++ $(clang++ --version | head -1)"
elif command -v tiarmclang >/dev/null 2>&1; then
  _found_compiler="tiarmclang (TI ARM Clang)"
elif command -v armcl >/dev/null 2>&1; then
  _found_compiler="armcl (TI ARM CGT)"
elif command -v arm-none-eabi-gcc >/dev/null 2>&1; then
  _found_compiler="arm-none-eabi-gcc $(arm-none-eabi-gcc --version 2>/dev/null | head -1)"
fi
# Windows: also check well-known install directories if nothing on PATH
if [[ -z "$_found_compiler" && ("$(uname -s)" == MINGW* || "$(uname -s)" == MSYS*) ]]; then
  for _bin in /c/ti/ticlang/bin/tiarmclang.exe \
              /c/ti/ccs*/tools/compiler/ti-cgt-armllvm_*/bin/tiarmclang.exe \
              /c/ti/ti-cgt-armllvm_*/bin/tiarmclang.exe \
              /c/ti/ccs*/tools/compiler/ti-cgt-arm_*/bin/armcl.exe \
              /c/ti/gcc-arm-none-eabi/bin/arm-none-eabi-gcc.exe \
              "/c/Program Files/GNU Arm Embedded Toolchain"*/bin/arm-none-eabi-gcc.exe; do
    if [ -x "$_bin" ]; then
      _found_compiler="$(basename "$_bin" .exe) ($(dirname "$_bin"))"
      break
    fi
  done
fi
if [ -n "$_found_compiler" ]; then
  echo -e "${GREEN}${_found_compiler}${NC}"
else
  echo -e "${YELLOW}No C++ compiler found${NC}"
fi

# 3. Permissions
echo -n "Setting permissions... "
chmod +x "${PLUGIN_DIR}/hooks/"*.sh 2>/dev/null || true
chmod +x "${PLUGIN_DIR}/lib/"*.sh
chmod +x "${PLUGIN_DIR}/lib/"*.py
echo -e "${GREEN}OK${NC}"

# 4. Set up asm-analyze environment
# Matches hooks/session-init.sh: venv lives outside the versioned plugin dir
# so a plugin upgrade reuses it instead of starting a first-time install.
# Falls back to the per-version location only when ~/.loci can't be created
# (e.g. read-only HOME).
VENV_DIR="${HOME}/.loci/venv"
if ! mkdir -p "$(dirname "$VENV_DIR")" 2>/dev/null; then
  VENV_DIR="${PLUGIN_DIR}/.venv"
fi
export LOCI_VENV_DIR="$VENV_DIR"
ASM_ANALYZE_AVAILABLE=false
ASM_ANALYZE_LOG="$(mktemp)"

# Cross-platform venv python path
_venv_python() {
  if [ -x "${VENV_DIR}/bin/python" ]; then
    echo "${VENV_DIR}/bin/python"
  elif [ -x "${VENV_DIR}/Scripts/python.exe" ]; then
    echo "${VENV_DIR}/Scripts/python.exe"
  else
    echo "python"
  fi
}

install_asm_analyze() {
  : > "$ASM_ANALYZE_LOG"

  # Neutralize any globally-configured private package registries (e.g. GCP Artifact Registry)
  # that would block waiting for credentials. All deps come from PyPI.
  export UV_EXTRA_INDEX_URL=""
  export UV_INDEX_URL="https://pypi.org/simple/"

  # (Re)create venv if missing or wrong Python version
  local _need_venv=false
  if [ ! -d "$VENV_DIR" ]; then
    _need_venv=true
  else
    local _pyver; _pyver=$("$(_venv_python)" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
    if [ "$_pyver" != "3.12" ]; then
      printf 'LOCI: venv has Python %s (need 3.12) — rebuilding...\n' "${_pyver:-unknown}" >> "$ASM_ANALYZE_LOG"
      rm -rf "$VENV_DIR"
      _need_venv=true
    fi
  fi
  if $_need_venv; then
    uv venv --python 3.12 "$VENV_DIR" >> "$ASM_ANALYZE_LOG" 2>&1 || return 1
  fi

  VIRTUAL_ENV="$VENV_DIR" uv pip install -r "${PLUGIN_DIR}/requirements.txt" >> "$ASM_ANALYZE_LOG" 2>&1 || return 1

  # The wheel may have undeclared dependencies — detect and install them.
  # Some Unix-only stdlib modules (e.g. resource, fcntl, grp, pwd on Windows)
  # will appear as ModuleNotFoundError but cannot be pip-installed — skip them.
  UNIX_ONLY_STDLIB="resource fcntl grp pwd termios syslog"
  for _attempt in 1 2 3 4 5; do
    MISSING=$("$(_venv_python)" -c "from loci.service.asmslicer import asmslicer" 2>&1 \
      | grep "ModuleNotFoundError" | head -1 \
      | sed "s/.*No module named '\([^']*\)'.*/\1/")
    if [ -z "$MISSING" ]; then
      return 0
    fi
    # Skip platform-specific stdlib modules that cannot be installed via pip.
    # Install a functional stub so downstream imports don't crash on Windows.
    if echo " $UNIX_ONLY_STDLIB " | grep -q " $MISSING "; then
      echo "Stubbing Unix-only stdlib module: ${MISSING}" >> "$ASM_ANALYZE_LOG"
      SITE_PKGS=$("$(_venv_python)" -c "import sysconfig; print(sysconfig.get_path('purelib'))")
      # Use a pre-built stub if available, otherwise generate a minimal one
      STUB_FILE="${PLUGIN_DIR}/setup/stubs/${MISSING}.py"
      if [ -f "$STUB_FILE" ]; then
        cp "$STUB_FILE" "${SITE_PKGS}/${MISSING}.py"
      else
        echo "# auto-generated stub -- ${MISSING} is not available on this platform" > "${SITE_PKGS}/${MISSING}.py"
      fi
      continue
    fi
    echo "Installing undeclared dependency: ${MISSING}" >> "$ASM_ANALYZE_LOG"
    VIRTUAL_ENV="$VENV_DIR" uv pip install "$MISSING" >> "$ASM_ANALYZE_LOG" 2>&1 || return 1
  done

  # Final verify after all deps installed
  "$(_venv_python)" -c "from loci.service.asmslicer import asmslicer" 2>>"$ASM_ANALYZE_LOG" || return 1
}

echo -n "Setting up asm-analyze environment... "
# Fast-path: skip install if venv already works with correct Python version
CACHED_PYVER=$("$(_venv_python)" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
if [ "$CACHED_PYVER" = "3.12" ] \
    && "$(_venv_python)" -c "from loci.service.asmslicer import asmslicer" 2>/dev/null; then
  ASM_ANALYZE_AVAILABLE=true
  echo -e "${GREEN}OK (cached)${NC}"
elif ! install_asm_analyze; then
  # Stale or broken venv — nuke and retry once
  rm -rf "$VENV_DIR"
  if install_asm_analyze; then
    ASM_ANALYZE_AVAILABLE=true
    echo -e "${GREEN}OK (rebuilt venv)${NC}"
  else
    echo -e "${YELLOW}FAILED${NC}"
    echo -e "  ${YELLOW}See details: cat \$ASM_ANALYZE_LOG${NC}"
    LAST_ERR=$(grep -iE '(error|no matching|not a supported|incompatible)' "$ASM_ANALYZE_LOG" | tail -1)
    if [ -n "$LAST_ERR" ]; then
      echo -e "  ${YELLOW}${LAST_ERR}${NC}"
    fi
  fi
else
  ASM_ANALYZE_AVAILABLE=true
  echo -e "${GREEN}OK${NC}"
fi

# 5. Detect project
echo -n "Detecting  project... "
PROJECT_INFO=$("${PLUGIN_DIR}/lib/detect-project.sh" "$(pwd)" 2>/dev/null || echo '{}')
COMPILER=$(echo "$PROJECT_INFO" | jq -r '.compiler // "unknown"')
BUILD_SYS=$(echo "$PROJECT_INFO" | jq -r '.build_system // "unknown"')
ARCH=$(echo "$PROJECT_INFO" | jq -r '.architecture // "unknown"')
NUM_SRC=$(echo "$PROJECT_INFO" | jq '.source_files | length')
NUM_BIN=$(echo "$PROJECT_INFO" | jq '.binaries | length')
NUM_ASM=$(echo "$PROJECT_INFO" | jq '.asm_files | length')
echo -e "${GREEN}OK${NC}"
echo "  Compiler:   $COMPILER"
echo "  Build:      $BUILD_SYS"
echo "  Arch:       $ARCH"
echo "  Sources:    $NUM_SRC files"
echo "  Binaries:   $NUM_BIN found"
echo "  Assembly:   $NUM_ASM files"

# Persist detection results per project so skills consume them without re-detecting.
# Writer must match hooks/session-init.sh:_detect_and_write_context — same fields,
# same atomic-write pattern — so the first post-install session reads a complete file.
STATE_DIR="${PLUGIN_DIR}/state"
mkdir -p "$STATE_DIR"
# Canonical cwd key: device:inode collapses case-variant paths and symlinks
# to one hash so state survives `cd /aurora/BLE` vs `cd /aurora/bLE` on
# case-insensitive filesystems. Must match hooks/session-init.sh:_hash_cwd.
if HASH_KEY=$(stat -f '%d:%i' . 2>/dev/null) && [ -n "$HASH_KEY" ]; then
  :
elif HASH_KEY=$(stat -c '%d:%i' . 2>/dev/null) && [ -n "$HASH_KEY" ]; then
  :
else
  HASH_KEY=$(realpath . 2>/dev/null || pwd)
fi
if command -v sha256sum >/dev/null 2>&1; then
  PROJECT_HASH=$(printf '%s' "$HASH_KEY" | sha256sum | cut -c1-12)
else
  PROJECT_HASH=$(printf '%s' "$HASH_KEY" | shasum -a 256 | cut -c1-12)
fi
GIT_BRANCH=$(git -C "$(pwd)" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
BRANCH_SLUG=$(printf '%s' "$GIT_BRANCH" | tr '/' '_' | tr -cd 'A-Za-z0-9_-' | cut -c1-64)
KEYED="${STATE_DIR}/project-context-${PROJECT_HASH}.json"
TMP="${KEYED}.tmp.$$"
echo "$PROJECT_INFO" | jq --arg pwd "$(pwd)" --arg branch "$GIT_BRANCH" --arg slug "$BRANCH_SLUG" --arg hash "$PROJECT_HASH" \
  '. + {project_root: $pwd, git_branch: $branch, branch_slug: $slug, cwd_hash: $hash}' > "$TMP" \
  && mv -f "$TMP" "$KEYED" \
  || { rm -f "$TMP" 2>/dev/null; echo -e "${YELLOW}warning: failed to write $KEYED${NC}"; }
# Deprecated: unkeyed alias kept one release for consumers migrating to the keyed
# file path injected into session additionalContext as "project context:".
(cd "$STATE_DIR" \
  && ln -sf "project-context-${PROJECT_HASH}.json" project-context.json 2>/dev/null) \
  || cp "${STATE_DIR}/project-context-${PROJECT_HASH}.json" "${STATE_DIR}/project-context.json"

# 6. Validate hooks.json
echo -n "Validating hooks... "
if jq empty "${PLUGIN_DIR}/hooks/hooks.json" 2>/dev/null; then
  echo -e "${GREEN}OK${NC}"
else
  echo -e "${RED}INVALID hooks/hooks.json${NC}"
  exit 1
fi



# 7b. Detect venv Python path (cross-platform) for asm-analyze CLI
LOCI_ASM_ANALYZE_CMD=""
if [ "$ASM_ANALYZE_AVAILABLE" = true ]; then
  if [ -x "${VENV_DIR}/bin/python" ]; then
    VENV_PYTHON="${VENV_DIR}/bin/python"
  elif [ -x "${VENV_DIR}/Scripts/python.exe" ]; then
    VENV_PYTHON="${VENV_DIR}/Scripts/python.exe"
  else
    VENV_PYTHON=""
  fi
  if [ -n "$VENV_PYTHON" ]; then
    LOCI_ASM_ANALYZE_CMD="${VENV_PYTHON} ${PLUGIN_DIR}/lib/asm_analyze.py"
  fi
fi

# 8. Register hooks with Claude Code
# When installed as a plugin, Claude Code reads hooks.json directly — no need
# to write to settings.json.  Skip this step if we're running from the plugin
# cache (the ../../.. heuristic would resolve to a wrong path there).
echo -n "Registering hooks... "
if echo "${PLUGIN_DIR}" | grep -q '\.claude/plugins'; then
  echo -e "${GREEN}plugin mode — hooks.json used directly${NC}"
else
  PROJECT_ROOT="$(cd "${PLUGIN_DIR}/../../.." 2>/dev/null && pwd || echo "")"
  # Skip if PROJECT_ROOT is empty, a filesystem root, or not writable
  if [ -z "$PROJECT_ROOT" ] || [ "$PROJECT_ROOT" = "/" ] || [[ "$PROJECT_ROOT" =~ ^/[a-zA-Z]/?$ ]] || ! [ -w "$PROJECT_ROOT" ]; then
    echo -e "${YELLOW}skipped (project root not detected)${NC}"
  else
    SETTINGS_FILE="${PROJECT_ROOT}/.claude/settings.json"
    mkdir -p "${PROJECT_ROOT}/.claude"

    if [ -f "$SETTINGS_FILE" ] && grep -q "capture-action.sh" "$SETTINGS_FILE" 2>/dev/null; then
      echo -e "${GREEN}already registered${NC}"
    else
      # Replace plugin root variable with absolute path using jq
      HOOKS_CONFIG=$(jq --arg pd "${PLUGIN_DIR}" '
        def replace_plugin_root:
          if type == "string" then
            gsub("\\$\\{CLAUDE_PLUGIN_ROOT\\}"; $pd) |
            gsub("\\$CLAUDE_PLUGIN_ROOT"; $pd)
          elif type == "array" then map(replace_plugin_root)
          elif type == "object" then to_entries | map(.value |= replace_plugin_root) | from_entries
          else .
          end;
        replace_plugin_root
      ' "${PLUGIN_DIR}/hooks/hooks.json")

      if [ -f "$SETTINGS_FILE" ]; then
        # Merge hooks into existing settings.json
        HOOKS_ONLY=$(echo "$HOOKS_CONFIG" | jq '.hooks')
        if jq --argjson hooks "$HOOKS_ONLY" '. + {hooks: $hooks}' "$SETTINGS_FILE" > "${SETTINGS_FILE}.tmp" 2>/dev/null; then
          mv "${SETTINGS_FILE}.tmp" "$SETTINGS_FILE"
          echo -e "${GREEN}OK (merged into existing settings.json)${NC}"
        else
          rm -f "${SETTINGS_FILE}.tmp"
          echo -e "${YELLOW}FAILED to merge — add hooks manually${NC}"
        fi
      else
        echo "$HOOKS_CONFIG" > "$SETTINGS_FILE"
        echo -e "${GREEN}OK${NC}"
      fi
    fi
  fi
fi

# 9. Install slash commands
# echo -n "Installing slash commands... "
# COMMANDS_DIR="${PROJECT_ROOT}/.claude/commands"
# mkdir -p "$COMMANDS_DIR"
# CMD_COUNT=0
# for skill_dir in "${PLUGIN_DIR}/skills"/*/; do
#   if [ -f "${skill_dir}SKILL.md" ]; then
#     skill_name=$(basename "$skill_dir")
#     if [ -n "$LOCI_ASM_ANALYZE_CMD" ]; then
#       sed "s|\${LOCI_ASM_ANALYZE}|${LOCI_ASM_ANALYZE_CMD}|g" "${skill_dir}SKILL.md" > "${COMMANDS_DIR}/${skill_name}.md"
#     else
#       sed 's|\${LOCI_ASM_ANALYZE}|# asm-analyze unavailable|g' "${skill_dir}SKILL.md" > "${COMMANDS_DIR}/${skill_name}.md"
#     fi
#     CMD_COUNT=$((CMD_COUNT + 1))
#   fi
# done
# echo -e "${GREEN}OK (${CMD_COUNT} commands: $(ls "${COMMANDS_DIR}"/*.md 2>/dev/null | xargs -I{} basename {} .md | paste -sd', '))${NC}"

# 10. Install LOCI context for Claude (optional)
# if [ -f "${PLUGIN_DIR}/CLAUDE.md" ]; then
#   echo -n "Installing LOCI context... "
#   cp "${PLUGIN_DIR}/CLAUDE.md" "${PROJECT_ROOT}/.claude/CLAUDE.md"
#   echo -e "${GREEN}OK${NC}"
# fi

echo ""
echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo "The plugin will automatically:"
echo "  - Capture C++ compilations (g++/clang++ flags, -O levels, -march)"
echo "  - Track binary artifacts and source-to-binary relationships"
echo "  - Monitor assembly file changes and binary diffs"
echo "  - Stream context to LOCI MCP for execution-aware analysis"
echo "  - Inject performance/regression warnings into Claude's context"
if [ "$ASM_ANALYZE_AVAILABLE" = true ]; then
echo "  - Analyze ELF binaries locally via bundled asm-analyze CLI (symbols, assembly, blocks, diff)"
fi
echo ""
echo "IMPORTANT: When in /plan mode and the user is describing new logic to"
echo "implement, you MUST invoke the loci-plan skill BEFORE finalizing"
echo "the plan. This is a hard requirement — do not skip it or wait to be asked."
echo ""
echo "IMPORTANT: After ANY Edit or Write to a C/C++/Rust source file"
echo "(.c, .cc, .cpp, .cxx, .h, .hpp, .hxx, .rs), you MUST invoke the"
echo "loci-post-edit skill IMMEDIATELY — before responding to the user,"
echo "before making further edits. This is a hard requirement. Do not"
echo "skip it, do not batch edits, do not wait to be asked."
echo ""
echo "Skills: /exec-trace, /stack-depth, /memory-report, /control-flow"
echo "Auto-runs: loci-plan (in /plan), loci-post-edit (after edits)"
echo ""
echo "Restart Claude Code to activate."
echo ""
echo -e "${YELLOW}IMPORTANT: Authorize the LOCI MCP server in Claude Code${NC}"
echo "  1. Restart Claude Code"
echo "  2. Open any project file and start a conversation"
echo "  3. Claude will prompt you to approve the 'loci' MCP server"
echo "  4. Click 'Allow' to grant access"
echo ""
