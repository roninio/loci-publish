#!/bin/bash
# Detect C++ project context: compiler, build system, binaries, ASM files.
# Outputs JSON for session initialization.

set -euo pipefail

CWD="${1:-.}"
IS_WINDOWS=false
[[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]] && IS_WINDOWS=true

# shellcheck source=loci_log.sh
. "$(dirname "$0")/loci_log.sh" 2>/dev/null || true
loci_log INFO detect-project "start: detect-project cwd=$CWD"

# Windows: search well-known install directories for vendor compilers not on PATH.
# Returns the full path to the compiler binary, or fails.
_find_windows_compiler() {
  $IS_WINDOWS || return 1
  local name="$1"
  local candidates=()
  case "$name" in
    tiarmclang)
      candidates=(
        /c/ti/ticlang/bin/tiarmclang.exe
        /c/ti/ccs*/tools/compiler/ti-cgt-armllvm_*/bin/tiarmclang.exe
        /c/ti/ti-cgt-armllvm_*/bin/tiarmclang.exe
      ) ;;
    armcl)
      candidates=(
        /c/ti/ccs*/tools/compiler/ti-cgt-arm_*/bin/armcl.exe
        /c/ti/ti-cgt-arm_*/bin/armcl.exe
      ) ;;
    iccarm)
      candidates=(
        "/c/Program Files/IAR Systems/Embedded Workbench"*/arm/bin/iccarm.exe
        "/c/Program Files (x86)/IAR Systems/Embedded Workbench"*/arm/bin/iccarm.exe
      ) ;;
    armcc)
      candidates=(
        "/c/Keil_v5/ARM/ARMCC/bin/armcc.exe"
        "/c/Keil_v5/ARM/ARMCLANG/bin/armclang.exe"
        "/c/Program Files/Keil_v5/ARM/ARMCC/bin/armcc.exe"
      ) ;;
    arm-none-eabi-gcc)
      candidates=(
        /c/ti/gcc-arm-none-eabi/bin/arm-none-eabi-gcc.exe
        "/c/Program Files/GNU Arm Embedded Toolchain"*/bin/arm-none-eabi-gcc.exe
        "/c/Program Files (x86)/GNU Arm Embedded Toolchain"*/bin/arm-none-eabi-gcc.exe
      ) ;;
  esac
  # Guard against empty array — under `set -u`, expanding "${arr[@]}" of an
  # empty array trips "unbound variable" in bash 3.2 (default macOS /bin/bash)
  # and bash <= 4.3. Safe in bash 4.4+, but we shebang `#!/bin/bash` so the
  # macOS system bash is in scope.
  [ "${#candidates[@]}" -eq 0 ] && return 1
  for candidate in "${candidates[@]}"; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

# Detect C++ compiler (including vendor/embedded toolchains)
detect_compiler() {
  # Check standard compilers first
  command -v g++ >/dev/null 2>&1 && echo "g++" && return
  command -v clang++ >/dev/null 2>&1 && echo "clang++" && return
  # Vendor / embedded compilers
  command -v tiarmclang >/dev/null 2>&1 && echo "tiarmclang" && return
  command -v armcl >/dev/null 2>&1 && echo "armcl" && return
  command -v iccarm >/dev/null 2>&1 && echo "iccarm" && return
  command -v armcc >/dev/null 2>&1 && echo "armcc" && return
  command -v arm-none-eabi-gcc >/dev/null 2>&1 && echo "arm-none-eabi-gcc" && return
  command -v aarch64-linux-gnu-gcc >/dev/null 2>&1 && echo "aarch64-linux-gnu-gcc" && return
  command -v tricore-elf-gcc >/dev/null 2>&1 && echo "tricore-elf-gcc" && return
  # Windows: check well-known install directories
  if $IS_WINDOWS; then
    for comp in tiarmclang armcl iccarm armcc arm-none-eabi-gcc; do
      if _find_windows_compiler "$comp" >/dev/null 2>&1; then
        echo "$comp"
        return
      fi
    done
  fi
  echo "unknown"
}

# Detect build system (including vendor IDEs). Emits "ccs+make" when a
# projectspec and a makefile coexist in the same tree — common for TI
# SimpleLink gmake builds that also ship CCS IDE metadata.
detect_build_system() {
  # Check root
  [ -f "$CWD/CMakeLists.txt" ] && echo "cmake" && return
  [ -f "$CWD/Makefile" ] || [ -f "$CWD/makefile" ] && echo "make" && return
  [ -f "$CWD/meson.build" ] && echo "meson" && return
  [ -f "$CWD/BUILD" ] || [ -f "$CWD/WORKSPACE" ] && echo "bazel" && return
  [ -f "$CWD/conanfile.txt" ] || [ -f "$CWD/conanfile.py" ] && echo "conan" && return
  [ -f "$CWD/vcpkg.json" ] && echo "vcpkg" && return

  # Subdir detection — deep scan, bounded by timeout.
  local has_projectspec=false has_makefile=false
  if timeout 4 find "$CWD" -maxdepth 10 \
      -type d \( -name .git -o -name node_modules -o -name .venv \
      -o -name target -o -name vendor -o -name third_party \) -prune -o \
      -name "*.projectspec" -type f -print -quit 2>/dev/null | grep -q .; then
    has_projectspec=true
  fi
  if timeout 4 find "$CWD" -maxdepth 10 \
      -type d \( -name .git -o -name node_modules -o -name .venv \
      -o -name target -o -name vendor -o -name third_party \) -prune -o \
      \( -name "Makefile" -o -name "makefile" -o -name "GNUmakefile" \) \
      -type f -print -quit 2>/dev/null | grep -q .; then
    has_makefile=true
  fi
  if $has_projectspec && $has_makefile; then
    echo "ccs+make" && return
  elif $has_projectspec; then
    echo "ccs" && return
  elif $has_makefile; then
    echo "make" && return
  fi

  find "$CWD" -maxdepth 2 -name "*.ccsproject" -print -quit 2>/dev/null | grep -q . && echo "ccs" && return
  find "$CWD" -maxdepth 2 -name ".cproject" -print -quit 2>/dev/null | grep -q . && echo "ccs" && return
  find "$CWD" -maxdepth 2 -name "*.ewp" -print -quit 2>/dev/null | grep -q . && echo "iar" && return
  find "$CWD" -maxdepth 2 -name "*.eww" -print -quit 2>/dev/null | grep -q . && echo "iar" && return
  find "$CWD" -maxdepth 2 -name "*.uvprojx" -print -quit 2>/dev/null | grep -q . && echo "keil" && return
  find "$CWD" -maxdepth 2 -name "*.uvproj" -print -quit 2>/dev/null | grep -q . && echo "keil" && return
  echo "direct"
}

# Find C++ source files
find_sources() {
  find "$CWD" -maxdepth 2 \( -name "*.cpp" -o -name "*.cxx" -o -name "*.cc" -o -name "*.c" -o -name "*.h" -o -name "*.hpp" \) 2>/dev/null | head -20 | jq -R . | jq -s .
}

# Find ELF/object files in common build directories
#
# maxdepth 10 catches TI CCS / SimpleLink-style layouts where the linked ELF
# lives at
#   examples/rtos/<board>/<stack>/<sample>/<rtos>/<toolchain>/Release/<name>.out
# (depth 9). Heavy dirs are pruned to keep the scan bounded.
find_elf_files() {
  local found=()
  # Prune: skip heavy dirs at any depth. The -prune must come BEFORE the
  # match-type expression. Wrap the whole thing in a `timeout` so a giant
  # tree cannot stall session init.
  local prune='-type d ( -name .git -o -name node_modules -o -name .venv -o -name target -o -name vendor -o -name third_party -o -name cmake-build-debug -o -name cmake-build-release -o -name __pycache__ -o -name .pytest_cache )'
  # shellcheck disable=SC2086
  while IFS= read -r f; do
    [ -n "$f" ] && found+=("$f")
  done < <(
    timeout 6 find "$CWD" -maxdepth 10 $prune -prune -o \
      \( -name "*.elf" -o -name "*.out" -o -name "*.axf" \) -type f -print \
      2>/dev/null | head -60
  )

  # Also check .o files but only in common build directories (too many .o files otherwise)
  for d in build out Debug Release output bin obj artifacts .loci-build; do
    if [ -d "$CWD/$d" ]; then
      while IFS= read -r f; do
        [ -n "$f" ] && found+=("$f")
      done < <(find "$CWD/$d" -maxdepth 3 -name "*.o" -type f 2>/dev/null | head -10)
    fi
  done

  if [ ${#found[@]} -eq 0 ]; then
    echo '[]'
  else
    printf '%s\n' "${found[@]}" | sort -u | head -30 | jq -R . | jq -s .
  fi
}

# Find candidate build directories by locating dirs that contain either a
# linked ELF or a makefile that references $(CC). The Python cascade will
# score and pick one, but publishing the list here avoids re-walking the
# tree on every plan invocation.
find_build_dirs() {
  local prune='-type d ( -name .git -o -name node_modules -o -name .venv -o -name target -o -name vendor -o -name third_party -o -name cmake-build-debug -o -name cmake-build-release -o -name __pycache__ -o -name .pytest_cache )'
  local dirs=()
  # shellcheck disable=SC2086
  while IFS= read -r f; do
    [ -n "$f" ] && dirs+=("$(dirname "$f")")
  done < <(
    timeout 6 find "$CWD" -maxdepth 10 $prune -prune -o \
      \( -name "*.elf" -o -name "*.out" -o -name "*.axf" \) -type f -print \
      2>/dev/null | head -60
  )
  # Also: dirs containing makefile + projectspec together (strong TI signal)
  # shellcheck disable=SC2086
  while IFS= read -r f; do
    [ -n "$f" ] && dirs+=("$(dirname "$f")")
  done < <(
    timeout 6 find "$CWD" -maxdepth 10 $prune -prune -o \
      -name "*.projectspec" -type f -print \
      2>/dev/null | head -40
  )
  if [ ${#dirs[@]} -eq 0 ]; then
    echo '[]'
    return
  fi
  printf '%s\n' "${dirs[@]}" | sort -u | head -40 | jq -R . | jq -s .
}

# Find compiled binaries (executables in CWD root — legacy compat).
#
# Skip text/source extensions before spawning `file`: on MSYS2/Cygwin every
# regular file reports as executable (NTFS has no x bit), so `[ -x ]` does
# not narrow the candidate set and `file` ends up running on every README
# and Makefile in the root. Extension filter cuts ~30 spawns to 0-3 in a
# typical source tree, saving ~1s per SessionStart on Windows.
find_binaries() {
  local bins=()
  for f in "$CWD"/*; do
    [ -f "$f" ] || continue
    case "$f" in
      *.md|*.txt|*.rst|*.json|*.jsonc|*.yml|*.yaml|*.toml|*.ini|*.cfg|*.conf\
      |*.xml|*.html|*.css|*.csv|*.log|*.lock\
      |*.py|*.pyc|*.pyi|*.sh|*.bash|*.zsh|*.ps1|*.bat|*.cmd\
      |*.js|*.ts|*.tsx|*.jsx|*.mjs|*.cjs\
      |*.c|*.cc|*.cpp|*.cxx|*.h|*.hh|*.hpp|*.hxx|*.rs|*.go|*.java|*.kt\
      |*.gitignore|*.gitattributes|*.editorconfig\
      |*Makefile*|*makefile*|README|README.*|LICENSE|LICENSE.*|CHANGELOG|CHANGELOG.*)
        continue ;;
    esac
    if [ -x "$f" ] && file "$f" 2>/dev/null | grep -qiE '(ELF|Mach-O|executable)'; then
      bins+=("$(basename "$f")")
    fi
  done
  if [ ${#bins[@]} -eq 0 ]; then
    echo '[]'
  else
    printf '%s\n' "${bins[@]}" | jq -R . | jq -s .
  fi
}

# Find assembly files
find_asm_files() {
  find "$CWD" -maxdepth 2 \( -name "*.asm" -o -name "*.s" -o -name "*.S" \) 2>/dev/null | head -20 | jq -R . | jq -s .
}

# Locate a working readelf binary (system, cross-toolchain, or vendor)
_find_readelf() {
  local candidates=(readelf arm-none-eabi-readelf aarch64-linux-gnu-readelf tricore-elf-readelf tiarmreadelf)
  for re in "${candidates[@]}"; do
    if command -v "$re" >/dev/null 2>&1; then
      echo "$re"
      return
    fi
  done
  # Not on PATH — search well-known vendor install directories
  local search_dirs=()
  if $IS_WINDOWS; then
    search_dirs=(
      /c/ti/gcc-arm-none-eabi/bin/arm-none-eabi-readelf.exe
      "/c/Program Files/GNU Arm Embedded Toolchain"*/bin/arm-none-eabi-readelf.exe
      "/c/Program Files (x86)/GNU Arm Embedded Toolchain"*/bin/arm-none-eabi-readelf.exe
      /c/ti/ticlang/bin/tiarmreadelf.exe
      /c/ti/ccs*/tools/compiler/ti-cgt-armllvm_*/bin/tiarmreadelf.exe
      /c/ti/ti-cgt-armllvm_*/bin/tiarmreadelf.exe
    )
  else
    search_dirs=(
      /opt/ti/clang/ti-cgt-armllvm_*/bin/tiarmreadelf
      "$HOME/ti/ti-cgt-armllvm_"*/bin/tiarmreadelf
    )
  fi
  for candidate in "${search_dirs[@]}"; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return
    fi
  done
  return 1
}

# Probe ARM ELF build attributes to determine specific ISA (armv6-m vs armv7e-m).
# Returns the ISA string or fails if readelf unavailable or attributes unreadable.
_arm_isa_from_elf() {
  local elf_path="$1"
  local re
  re=$(_find_readelf) || return 1
  local attrs
  attrs=$("$re" -A "$elf_path" 2>/dev/null) || return 1
  # Extract CPU_arch from either format (no grep -P for macOS compat):
  #   standard readelf:   Tag_CPU_arch: v6S-M
  #   tiarmreadelf:       Description: ARM v6S-M
  local cpu_arch
  cpu_arch=$(echo "$attrs" | sed -n 's/.*Tag_CPU_arch:[[:space:]]*\([^ ]*\).*/\1/p' | head -1)
  [ -z "$cpu_arch" ] && \
    cpu_arch=$(echo "$attrs" | grep -A1 'TagName: CPU_arch' | sed -n 's/.*Description:[[:space:]]*ARM[[:space:]]*\([^ ]*\).*/\1/p' | head -1)
  case "$cpu_arch" in
    v6-M|v6S-M)      echo "armv6-m" ;;
    v7E-M)           echo "armv7e-m" ;;
    v7-M)            echo "armv7-m" ;;
    v8-M.main|v81-M) echo "armv8-m.main" ;;
    v8-M.base)       echo "armv8-m.base" ;;
    *)               return 1 ;;  # unknown or A-class — let caller handle
  esac
}

# Detect architecture from an ELF file using `file` command
arch_from_elf() {
  local elf_path="$1"
  local file_output
  file_output=$(file "$elf_path" 2>/dev/null) || return 1
  # Match architecture from file(1) output
  if echo "$file_output" | grep -qiE 'aarch64|ARM aarch64|ARM 64'; then
    echo "aarch64"
  elif echo "$file_output" | grep -qiE 'ARM,.*EABI|Thumb|Cortex|armv7|arm,'; then
    # ARM detected — try to refine to specific ISA via ELF attributes
    local isa
    isa=$(_arm_isa_from_elf "$elf_path") && echo "$isa" || echo "arm"
  elif echo "$file_output" | grep -qiE 'TriCore|tricore'; then
    echo "tricore"
  elif echo "$file_output" | grep -qiE 'x86.64|x86-64|AMD64'; then
    echo "x86_64"
  elif echo "$file_output" | grep -qiE 'Intel 80386|i386|x86,'; then
    echo "i386"
  else
    return 1
  fi
}

# Detect architecture — prefer ELF analysis over uname.
#
# When multiple ELFs are present (common in vendor SDKs that ship rom/driverlib
# binaries alongside the user's actual project output), pick the freshest one
# as the project's "active" target. This avoids reporting a stale armv7e-m
# driverlib when the user just built an armv6-m project.
detect_architecture() {
  local elf_files="$1"
  local arch elf_path

  # 1. Freshest ELF wins.
  elf_path=$(_freshest_elf "$elf_files")
  if [ -n "$elf_path" ] && [ -f "$elf_path" ]; then
    arch=$(arch_from_elf "$elf_path")
    if [ -n "$arch" ]; then
      echo "$arch"
      return
    fi
  fi
  # 2. Fall back to the first ELF — useful when stat is unavailable.
  elf_path=$(echo "$elf_files" | jq -r '.[0] // empty' 2>/dev/null)
  if [ -n "$elf_path" ] && [ -f "$elf_path" ]; then
    arch=$(arch_from_elf "$elf_path")
    if [ -n "$arch" ]; then
      echo "$arch"
      return
    fi
  fi
  # 3. Fall back to executables in CWD. Filter out obvious text/source
  # extensions before spawning `file` — see find_binaries() for the same
  # MSYS2 [-x]-is-always-true rationale.
  for f in "$CWD"/*; do
    [ -f "$f" ] || continue
    case "$f" in
      *.md|*.txt|*.rst|*.json|*.jsonc|*.yml|*.yaml|*.toml|*.ini|*.cfg|*.conf\
      |*.xml|*.html|*.css|*.csv|*.log|*.lock\
      |*.py|*.pyc|*.pyi|*.sh|*.bash|*.zsh|*.ps1|*.bat|*.cmd\
      |*.js|*.ts|*.tsx|*.jsx|*.mjs|*.cjs\
      |*.c|*.cc|*.cpp|*.cxx|*.h|*.hh|*.hpp|*.hxx|*.rs|*.go|*.java|*.kt\
      |*.gitignore|*.gitattributes|*.editorconfig\
      |*Makefile*|*makefile*|README|README.*|LICENSE|LICENSE.*|CHANGELOG|CHANGELOG.*)
        continue ;;
    esac
    if [ -x "$f" ] && file "$f" 2>/dev/null | grep -qiE '(ELF|Mach-O)'; then
      arch=$(arch_from_elf "$f")
      if [ -n "$arch" ]; then
        echo "$arch"
        return
      fi
    fi
  done
  uname -m
}

# Detect available LOCI-compatible cross-compilers
detect_cross_compilers() {
  local compilers=()
  # GCC cross-compilers
  command -v aarch64-linux-gnu-g++ >/dev/null 2>&1 && compilers+=("aarch64")
  command -v aarch64-linux-gnu-gcc >/dev/null 2>&1 && compilers+=("aarch64")
  command -v arm-none-eabi-g++ >/dev/null 2>&1 && compilers+=("cortexm")
  command -v arm-none-eabi-gcc >/dev/null 2>&1 && compilers+=("cortexm")
  command -v tricore-elf-g++ >/dev/null 2>&1 && compilers+=("tricore")
  command -v tricore-elf-gcc >/dev/null 2>&1 && compilers+=("tricore")
  # Vendor compilers that target LOCI architectures
  command -v tiarmclang >/dev/null 2>&1 && compilers+=("cortexm")
  command -v armcl >/dev/null 2>&1 && compilers+=("cortexm")
  command -v iccarm >/dev/null 2>&1 && compilers+=("cortexm")
  command -v armcc >/dev/null 2>&1 && compilers+=("cortexm")
  # Windows: also check well-known install directories
  if $IS_WINDOWS; then
    _find_windows_compiler tiarmclang >/dev/null 2>&1 && compilers+=("cortexm")
    _find_windows_compiler armcl >/dev/null 2>&1 && compilers+=("cortexm")
    _find_windows_compiler iccarm >/dev/null 2>&1 && compilers+=("cortexm")
    _find_windows_compiler armcc >/dev/null 2>&1 && compilers+=("cortexm")
    _find_windows_compiler arm-none-eabi-gcc >/dev/null 2>&1 && compilers+=("cortexm")
  fi
  if [ ${#compilers[@]} -eq 0 ]; then
    echo '[]'
  else
    # Deduplicate
    printf '%s\n' "${compilers[@]}" | sort -u | jq -R . | jq -s .
  fi
}

# Map generic arch name to LOCI timing-backend target
_map_to_timing_target() {
  case "$1" in
    cortexm)  echo "armv7e-m" ;;
    tricore)  echo "tc399" ;;
    aarch64)  echo "aarch64" ;;
    *)        echo "$1" ;;
  esac
}

# Map detected architecture to LOCI target (aarch64, armv7e-m, armv6-m, tc399) or null
resolve_loci_target() {
  local arch="$1"
  local cross_compilers="$2"
  local lower_arch
  lower_arch=$(echo "$arch" | tr '[:upper:]' '[:lower:]')
  local generic
  case "$lower_arch" in
    aarch64|arm64)
      generic="aarch64" ;;
    armv6-m)
      echo "armv6-m" && return ;;
    armv7e-m|armv7-m)
      echo "armv7e-m" && return ;;
    armv8-m.main|armv8-m.base)
      echo "armv7e-m" && return ;;
    arm|armv7*|armv8-m*|cortex-m*|thumb)
      generic="cortexm" ;;
    tricore|tc3*|tc39*)
      generic="tricore" ;;
    *)
      # Host arch is not a LOCI target — check if any cross-compiler is available
      local first
      first=$(echo "$cross_compilers" | jq -r '.[0] // empty' 2>/dev/null)
      if [ -n "$first" ]; then
        generic="$first"
      else
        echo "null"
        return
      fi
      ;;
  esac
  _map_to_timing_target "$generic"
}

# Infer compiler from a path token. TI SimpleLink, NXP MCUXpresso, ST Cube,
# and most vendor SDKs use a per-toolchain build directory naming convention
# (`ticlang`, `iar`, `gcc`, `keil`, `armclang`) that's far more reliable than
# grepping orchestration makefiles which mention every toolchain.
_compiler_from_path() {
  local p="$1"
  case "$p" in
    */ticlang/*)                              echo "tiarmclang"; return 0 ;;
    */iar/*|*/ewarm/*)                        echo "iccarm"; return 0 ;;
    */gcc/*|*/arm-gcc/*)                      echo "arm-none-eabi-gcc"; return 0 ;;
    */keil/*|*/armcc/*)                       echo "armcc"; return 0 ;;
    */armclang/*)                             echo "armcc"; return 0 ;;
    */tricore/*)                              echo "tricore-elf-gcc"; return 0 ;;
    */aarch64/*|*/aarch64-linux-gnu/*|*/arm64/*) echo "aarch64-linux-gnu-gcc"; return 0 ;;
  esac
  return 1
}

# Pick the freshest (most recently modified) linked binary from a JSON array.
# Skips intermediate object files (.o) — they live in caches like .loci-build/
# and don't represent the project's active toolchain. Only .out/.elf/.axf
# linker outputs are meaningful for path-based compiler inference.
_freshest_elf() {
  local elfs="$1"
  local best="" best_mt=0 mt
  while IFS= read -r elf; do
    [ -z "$elf" ] && continue
    [ -f "$elf" ] || continue
    case "$elf" in
      *.o) continue ;;
    esac
    # macOS uses `stat -f %m`, GNU coreutils use `stat -c %Y`.
    mt=$(stat -f %m "$elf" 2>/dev/null || stat -c %Y "$elf" 2>/dev/null)
    [ -z "$mt" ] && continue
    if [ "$mt" -gt "$best_mt" ]; then
      best_mt=$mt
      best=$elf
    fi
  done < <(echo "$elfs" | jq -r '.[]?' 2>/dev/null)
  [ -n "$best" ] && echo "$best"
}

# Tally compiler references across config files; pick the most-mentioned one.
# Used as a fallback when no toolchain path token is found in a fresh ELF.
_tally_compiler_refs() {
  local files=("$@")
  local tia=0 armcl=0 icc=0 armcc=0 gcc_eabi=0 gcc_a64=0 tricore=0
  for f in "${files[@]}"; do
    [ -f "$f" ] || continue
    grep -qiE 'tiarmclang|ti_arm_clang|TI_TOOLCHAIN'         "$f" 2>/dev/null && tia=$((tia+1))
    grep -qiE 'ti_arm_cgt|TI_CGT'                            "$f" 2>/dev/null && armcl=$((armcl+1))
    grep -qiE 'iccarm|IAR_ARMCOMPILER|ewarm'                 "$f" 2>/dev/null && icc=$((icc+1))
    grep -qiE 'armcc|ARMCC|armclang'                         "$f" 2>/dev/null && armcc=$((armcc+1))
    grep -qiE 'arm-none-eabi'                                "$f" 2>/dev/null && gcc_eabi=$((gcc_eabi+1))
    grep -qiE 'aarch64-linux-gnu'                            "$f" 2>/dev/null && gcc_a64=$((gcc_a64+1))
    grep -qiE 'tricore-elf'                                  "$f" 2>/dev/null && tricore=$((tricore+1))
  done
  local best="" best_n=0
  if [ "$tia"      -gt "$best_n" ]; then best="tiarmclang";            best_n=$tia;      fi
  if [ "$armcl"    -gt "$best_n" ]; then best="armcl";                 best_n=$armcl;    fi
  if [ "$icc"      -gt "$best_n" ]; then best="iccarm";                best_n=$icc;      fi
  if [ "$armcc"    -gt "$best_n" ]; then best="armcc";                 best_n=$armcc;    fi
  if [ "$gcc_eabi" -gt "$best_n" ]; then best="arm-none-eabi-gcc";     best_n=$gcc_eabi; fi
  if [ "$gcc_a64"  -gt "$best_n" ]; then best="aarch64-linux-gnu-gcc"; best_n=$gcc_a64;  fi
  if [ "$tricore"  -gt "$best_n" ]; then best="tricore-elf-gcc";       best_n=$tricore;  fi
  echo "$best"
}

# Detect compiler referenced in build configs (not necessarily in PATH).
#
# Strategy:
#   1. Prefer the toolchain implied by the freshest ELF's build path. Vendor
#      SDKs (TI SimpleLink, NXP, ST, Renesas) use per-toolchain build dirs
#      like `ticlang/Release/`, `iar/Debug/`, `gcc/build/` — the most recent
#      output is the project's "active" toolchain.
#   2. Fall back to tallying compiler references across all matched config
#      files, picking whichever compiler is mentioned most. Avoids false
#      positives from SDK orchestration makefiles that mention every toolchain.
detect_build_compiler() {
  local build_sys="$1"
  local elfs="$2"

  # 1. Path-based detection from freshest ELF.
  local freshest
  freshest=$(_freshest_elf "$elfs")
  if [ -n "$freshest" ]; then
    local from_path
    if from_path=$(_compiler_from_path "$freshest"); then
      echo "$from_path"
      return
    fi
  fi

  # 2. Config-file tally fallback.
  local config_files=()
  case "$build_sys" in
    cmake) config_files=("$CWD/CMakeLists.txt" "$CWD/cmake"/*.cmake) ;;
    make) config_files=("$CWD/Makefile" "$CWD/makefile") ;;
    ccs) config_files=("$CWD"/*.projectspec "$CWD"/.cproject) ;;
    ccs+make)
      local prune='-type d ( -name .git -o -name node_modules -o -name .venv -o -name target -o -name vendor -o -name third_party -o -name __pycache__ -o -name .pytest_cache )'
      while IFS= read -r f; do
        [ -n "$f" ] && config_files+=("$f")
      done < <(
        # shellcheck disable=SC2086
        timeout 4 find "$CWD" -maxdepth 10 $prune -prune -o \
          \( -name "*.projectspec" -o -name "Makefile" -o -name "makefile" \) \
          -type f -print 2>/dev/null | head -50
      )
      ;;
    iar) config_files=("$CWD"/*.ewp) ;;
    keil) config_files=("$CWD"/*.uvprojx "$CWD"/*.uvproj) ;;
  esac

  # Guard against empty array — under `set -u`, expanding "${arr[@]}" of an
  # empty array trips "unbound variable" in bash 3.2 (default macOS /bin/bash)
  # and bash <= 4.3. Also handles future build_system values (meson, bazel,
  # conan, vcpkg) that have no config-file mapping yet — graceful degradation
  # to empty BUILD_COMPILER instead of aborting the whole context detection.
  if [ "${#config_files[@]}" -eq 0 ]; then
    echo ""
    return
  fi

  _tally_compiler_refs "${config_files[@]}"
}

# Wrap each stage with start/end log lines so SessionStart slowness can be
# traced via $LOCI_STATE_DIR/loci.log without re-instrumenting at call time.
# `|| rc=$?` keeps set -e happy when the wrapped command exits non-zero.
_stage() {
    local label="$1"; shift
    loci_log INFO detect-project "start: $label"
    local rc=0
    "$@" || rc=$?
    loci_log INFO detect-project "end: $label (rc=$rc)"
    return $rc
}

COMPILER=$(_stage detect_compiler        detect_compiler)
BUILD_SYSTEM=$(_stage detect_build_system detect_build_system)
SOURCES=$(_stage find_sources             find_sources)
ELF_FILES=$(_stage find_elf_files         find_elf_files)
BUILD_DIRS=$(_stage find_build_dirs       find_build_dirs)
BINARIES=$(_stage find_binaries           find_binaries)
ASM_FILES=$(_stage find_asm_files         find_asm_files)
ARCH=$(_stage detect_architecture         detect_architecture "$ELF_FILES")
CROSS_COMPILERS=$(_stage detect_cross_compilers detect_cross_compilers)
LOCI_TARGET=$(_stage resolve_loci_target  resolve_loci_target "$ARCH" "$CROSS_COMPILERS")

# Only compute BUILD_COMPILER when COMPILER is generic — when COMPILER is
# already vendor-specific the precedence rule below would discard the result,
# and detect_build_compiler's fallback grep-tally walks every Makefile +
# projectspec in the tree (~7s on TI SimpleLink projects on Windows).
BUILD_COMPILER=""
case "$COMPILER" in
  g++|clang++|unknown)
    BUILD_COMPILER=$(_stage detect_build_compiler detect_build_compiler "$BUILD_SYSTEM" "$ELF_FILES")
    [ -n "$BUILD_COMPILER" ] && COMPILER="$BUILD_COMPILER"
    ;;
  *)
    loci_log INFO detect-project "skip: detect_build_compiler (COMPILER=$COMPILER is vendor-specific)"
    ;;
esac

loci_log INFO detect-project "result: compiler=$COMPILER build_system=$BUILD_SYSTEM arch=$ARCH loci_target=$LOCI_TARGET elfs=$(echo "$ELF_FILES" | jq 'length' 2>/dev/null || echo ?) build_dirs=$(echo "$BUILD_DIRS" | jq 'length' 2>/dev/null || echo ?)"

# Resolve full path for compilers discovered via Windows search (not on PATH)
COMPILER_PATH=""
if $IS_WINDOWS && [ "$COMPILER" != "unknown" ] && ! command -v "$COMPILER" >/dev/null 2>&1; then
  COMPILER_PATH=$(_find_windows_compiler "$COMPILER" 2>/dev/null || true)
fi

# Determine LOCI compatibility
if [ "$LOCI_TARGET" != "null" ]; then
  LOCI_COMPATIBLE="true"
else
  LOCI_COMPATIBLE="false"
fi

jq -n \
  --arg compiler "$COMPILER" \
  --arg compiler_path "$COMPILER_PATH" \
  --arg build_compiler "$BUILD_COMPILER" \
  --arg build_system "$BUILD_SYSTEM" \
  --arg project_type "cpp" \
  --arg architecture "$ARCH" \
  --argjson source_files "$SOURCES" \
  --argjson binaries "$BINARIES" \
  --argjson elf_files "$ELF_FILES" \
  --argjson build_dirs "$BUILD_DIRS" \
  --argjson asm_files "$ASM_FILES" \
  --argjson cross_compilers "$CROSS_COMPILERS" \
  --argjson loci_compatible "$LOCI_COMPATIBLE" \
  --arg loci_target "$LOCI_TARGET" \
  --arg detected_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
  '{
    language_stack: ["cpp"],
    compiler: $compiler,
    compiler_path: (if $compiler_path == "" then null else $compiler_path end),
    build_compiler: (if $build_compiler == "" then null else $build_compiler end),
    build_system: $build_system,
    project_type: $project_type,
    architecture: $architecture,
    source_files: $source_files,
    binaries: $binaries,
    elf_files: $elf_files,
    build_dirs: $build_dirs,
    asm_files: $asm_files,
    cross_compilers: $cross_compilers,
    loci_compatible: $loci_compatible,
    loci_target: (if $loci_target == "null" then null else $loci_target end),
    detected_at: $detected_at,
    scan_depth: 8
  }'
