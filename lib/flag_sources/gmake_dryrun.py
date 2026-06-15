"""Step 2: `make --dry-run` flag harvesting — the primary fix.

For gmake-style projects (TI SimpleLink, Zephyr, U-Boot, Linux kernel,
most plain-make trees) the only reliable way to recover every `-I`
and `-D` is to ask the build system itself. `make --dry-run` prints
each compile command without executing — we scan stdout for the first
line that compiles a `.c`/`.cpp`, tokenize it, and reuse the flags for
our own source.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import DiscoveryMiss, DiscoveryResult, shlex_split_line
from .flags_normalize import strip_source_and_output
from .response_file import expand_response_files


# Cap how many donor targets we retry from OBJECTS before giving up. The
# first donor is by far the most likely to work; the retry exists to
# survive cases like "user prepended a target whose .c source is absent
# or whose recipe references a missing tool" — common during in-progress
# work where a build target was added before its source file was created.
_MAX_DONOR_CANDIDATES = 5


# Known cross-compiler basename fragments used when tokenizing the compile
# command line. First match in tokens becomes the compiler.
_KNOWN_COMPILER_BASENAMES = (
    "tiarmclang",
    "arm-none-eabi-gcc", "arm-none-eabi-g++",
    "arm-none-eabi-clang", "arm-none-eabi-clang++",
    "aarch64-linux-gnu-gcc", "aarch64-linux-gnu-g++",
    "aarch64-none-elf-gcc", "aarch64-none-elf-g++",
    "tricore-elf-gcc", "tricore-elf-g++",
    "armclang", "armcl", "iccarm", "armcc",
    # Keep native names LAST so cross-prefixes win on prefix matching.
    "clang++", "clang", "g++", "gcc",
)


def _find_make() -> tuple[str | None, str | None]:
    """Return (path_to_make, version_line) for a GNU Make, or (None, None)."""
    for name in ("gmake", "make", "mingw32-make"):
        p = shutil.which(name)
        if not p:
            continue
        try:
            out = subprocess.run(
                [p, "--version"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        first_line = (out.stdout or "").splitlines()[0] if out.stdout else ""
        if "GNU Make" in first_line:
            return p, first_line.strip()

    # Windows: try CCS-bundled gmake.exe
    if os.name == "nt":
        for cand in (
            r"C:\ti\ccs*\utils\bin\gmake.exe",
            r"C:\ti\ccs*\ccs\utils\bin\gmake.exe",
        ):
            import glob
            for found in glob.glob(cand):
                try:
                    out = subprocess.run(
                        [found, "--version"],
                        capture_output=True, text=True, timeout=10,
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                first = (out.stdout or "").splitlines()[0] if out.stdout else ""
                if "GNU Make" in first:
                    return found, first.strip()

    return None, None


def _pick_donor_targets(makefile_path: Path, limit: int = _MAX_DONOR_CANDIDATES) -> list[str]:
    """Scan the makefile for an ``OBJECTS = ...`` line and return up to
    ``limit`` `.obj`/`.o` targets in source order.

    Returning multiple candidates lets ``discover()`` retry with the next
    donor when the first one's recipe fails (e.g. its `.c` source is
    absent in the user's tree). The first candidate is the legacy
    selection; the rest are fallbacks.
    """
    try:
        text = makefile_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Find `OBJECTS = ...` or `OBJS = ...` — capture the full variable body
    # including continuation lines.
    m = re.search(
        r"^\s*(?:OBJECTS|OBJS)\s*[:+]?=\s*(.+?(?:\\\n.+?)*)(?:\n|$)",
        text, re.MULTILINE,
    )
    candidates: list[str] = []
    if m:
        body = m.group(1).replace("\\\n", " ")
        for tok in body.split():
            tok = tok.strip()
            # Skip `$(...)` or `$(patsubst ...)` tokens — they may not resolve
            # when we ask make to build them by name
            if tok.startswith("$(") or tok.startswith("${"):
                continue
            if (tok.endswith(".obj") or tok.endswith(".o")) and tok not in candidates:
                candidates.append(tok)
                if len(candidates) >= limit:
                    break
    if candidates:
        return candidates

    # Fallback: find any `<name>.obj:` or `<name>.o:` rule head
    for rule_m in re.finditer(r"^([A-Za-z0-9_]+)\.(?:obj|o)\s*:", text, re.MULTILINE):
        name = f"{rule_m.group(1)}.obj"
        if name not in candidates:
            candidates.append(name)
            if len(candidates) >= limit:
                break
    return candidates


def _pick_donor_target(makefile_path: Path) -> str | None:
    """Backward-compatible single-donor wrapper — retained so external
    callers (and existing tests) keep working unchanged."""
    candidates = _pick_donor_targets(makefile_path, limit=1)
    return candidates[0] if candidates else None


def _absolutize_include_paths(flags: list[str], build_dir: Path) -> list[str]:
    """Make every relative `-I`, `-isystem`, and `@file` path absolute
    against build_dir, since the compile runs with a different CWD."""
    out: list[str] = []
    it = iter(flags)
    for f in it:
        if f.startswith("-I"):
            path = f[2:]
            if path and not Path(path).is_absolute():
                resolved = (build_dir / path).resolve()
                out.append(f"-I{resolved}")
            else:
                out.append(f)
        elif f == "-isystem":
            try:
                nxt = next(it)
            except StopIteration:
                out.append(f)
                break
            out.append(f)
            if nxt and not Path(nxt).is_absolute():
                out.append(str((build_dir / nxt).resolve()))
            else:
                out.append(nxt)
        elif f.startswith("-isystem="):
            path = f[len("-isystem="):]
            if path and not Path(path).is_absolute():
                out.append(f"-isystem={(build_dir / path).resolve()}")
            else:
                out.append(f)
        elif f.startswith("@") and len(f) > 1:
            path = f[1:]
            if path and not Path(path).is_absolute():
                out.append(f"@{(build_dir / path).resolve()}")
            else:
                out.append(f)
        else:
            out.append(f)
    return out


def _find_makefile(build_dir: Path) -> Path | None:
    for name in ("makefile", "Makefile", "GNUmakefile"):
        p = build_dir / name
        if p.is_file():
            return p
    return None


def _extract_compile_line(stdout: str) -> tuple[str, list[str]] | None:
    """Return (compiler, tokens) for the first compile line, or None.

    Scans stdout for a line that (a) contains `-c <source>` somewhere, and
    (b) whose first token after optional `@echo …` prefixes is a known
    cross-compiler basename.
    """
    for line in stdout.splitlines():
        if " -c " not in line and "\t-c " not in line:
            continue
        try:
            tokens = shlex_split_line(line)
        except ValueError:
            continue
        if not tokens:
            continue
        cc_idx = -1
        for i, tok in enumerate(tokens):
            name = Path(tok.strip('"\'')).name.lower()
            if name.endswith(".exe"):
                name = name[:-4]
            if any(name == known or name.endswith("/" + known)
                   or name.endswith("\\" + known)
                   for known in _KNOWN_COMPILER_BASENAMES):
                cc_idx = i
                break
        if cc_idx < 0:
            continue
        if "-c" not in tokens[cc_idx + 1:]:
            continue
        compiler = tokens[cc_idx].strip('"\'')
        rest = tokens[cc_idx + 1:]
        return compiler, rest
    return None


def _run_dryrun(
    make_exe: str,
    build_dir: Path,
    makefile: Path,
    target: str,
    variables: dict,
) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.update({k: v for k, v in variables.items() if v is not None})
    cmd = [
        make_exe, "--dry-run", "--no-print-directory", "--always-make",
        "-C", str(build_dir), "-f", str(makefile), target,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=30,
            env=env,
            cwd=str(build_dir),
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "make --dry-run timed out after 30s"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def discover(
    source: Path,
    loci_target: str,
    project_root: Path,
    context: dict,
    build_dir: Path | None,
) -> DiscoveryResult | DiscoveryMiss | None:
    if build_dir is None:
        return DiscoveryMiss(reason="no build_dir discovered")
    if not build_dir.is_dir():
        return DiscoveryMiss(reason=f"build_dir is not a directory: {build_dir}")

    makefile = _find_makefile(build_dir)
    if makefile is None:
        return DiscoveryMiss(reason=f"no makefile in build_dir ({build_dir})")

    make_exe, make_version = _find_make()
    if make_exe is None:
        # Signal a real error (not "missing") so the orchestrator's attempt
        # trace explains it via raise → caught → "error" path.
        raise RuntimeError(
            "GNU Make (gmake/make/mingw32-make) not on PATH; "
            "install it or add the CCS-bundled gmake to PATH"
        )

    candidates = _pick_donor_targets(makefile)
    if not candidates:
        return DiscoveryMiss(
            reason=f"no donor target found in makefile {makefile.name} "
                   f"(no OBJECTS/OBJS line and no <name>.obj: rule)"
        )

    variables: dict = {}
    # Pull variables from user override if any (already loaded by the
    # orchestrator on a subsequent call; we accept none here)
    ov_context = context.get("user_override_variables") if isinstance(context, dict) else None
    if isinstance(ov_context, dict):
        variables.update(ov_context)

    # Try each donor candidate until one yields a parseable compile line.
    # Failure modes per donor: (a) make --dry-run rc!=0 with empty stdout
    # (e.g. donor's .c source absent), (b) stdout has no parseable compile
    # line. Either case falls through to the next candidate.
    target: str | None = None
    stdout = ""
    stderr = ""
    rc = 0
    duration_ms = 0
    extracted: tuple[str, list[str]] | None = None
    failures: list[str] = []
    t_total = time.monotonic()
    for cand in candidates:
        t0 = time.monotonic()
        rc, stdout, stderr = _run_dryrun(
            make_exe, build_dir, makefile, cand, variables,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        if rc != 0 and not stdout:
            failures.append(
                f"donor {cand!r}: make --dry-run rc={rc}; "
                f"stderr_tail={(stderr or '').strip().splitlines()[-1][:200] if stderr else ''!r}"
            )
            continue
        extracted = _extract_compile_line(stdout)
        if extracted is None:
            failures.append(
                f"donor {cand!r}: dry-run rc={rc} but no compile line "
                f"matched in {len(stdout)} chars of stdout"
            )
            continue
        target = cand
        break

    total_duration_ms = int((time.monotonic() - t_total) * 1000)
    if extracted is None or target is None:
        # All donor candidates failed. Surface the per-donor reasons so
        # the user can see which targets were tried and why each failed.
        joined = "; ".join(failures) if failures else "no candidates ran"
        return DiscoveryMiss(
            reason=(
                f"all {len(candidates)} donor target(s) failed in "
                f"{total_duration_ms} ms: {joined}"
            )
        )
    compiler, raw_tokens = extracted

    # Strip `-c <donor_source>`, `-o <output>`, and the donor source positional.
    # Keep the -c flag itself so the command remains a compile request.
    tokens: list[str] = []
    skip_next = False
    saw_dash_c = False
    for t in raw_tokens:
        if skip_next:
            skip_next = False
            continue
        if t == "-c":
            tokens.append(t)
            skip_next = True   # drop the donor source path after -c
            saw_dash_c = True
            continue
        if t == "-o":
            skip_next = True
            continue
        if t.startswith("-o") and len(t) > 2 and not t.lower().startswith("-oz"):
            continue
        tokens.append(t)
    # Drop any remaining positional .c/.cpp tokens (some makefiles put the
    # source before -c); keep everything starting with '-' or '@' intact.
    tokens = [
        t for t in tokens
        if t.startswith("-") or t.startswith("@") or t.startswith('"')
        or not t.lower().endswith((".c", ".cpp", ".cc", ".cxx", ".c++"))
    ]
    if not saw_dash_c:
        tokens.append("-c")

    # Expand @file.opt response files relative to build_dir
    expanded, augmentations = expand_response_files(tokens, build_dir)

    # Rewrite relative -I / -isystem paths to absolutes against build_dir.
    # The makefile expresses `-I.` / `-I../..` relative to its own directory;
    # we'll be invoking the compiler with a different CWD, so those paths
    # must be resolved here.
    expanded = _absolutize_include_paths(expanded, build_dir)

    # TI gmake specificity: `SYSCFG_OPT_FILES` is populated via `$(shell …)`
    # which runs SysConfig. In `make --dry-run` that shell call may return
    # empty (no `SYSCONFIG_TOOL` on PATH, or not set in env). Detect that
    # and inject any pre-existing `ti_*.opt` files in the build_dir so the
    # generated `-D<device_family>` / stack-config defines get picked up.
    already_used = set()
    for aug in augmentations:
        if aug.get("kind") == "response_file_expand":
            already_used.add(Path(aug.get("file", "")).resolve())
    for opt_file in sorted(build_dir.glob("ti_*.opt")):
        try:
            rp = opt_file.resolve()
        except OSError:
            continue
        if rp in already_used:
            continue
        fallback_tokens, fallback_aug = expand_response_files(
            [f"@{opt_file.name}"], build_dir,
        )
        for tok in fallback_tokens:
            if tok not in expanded and not tok.startswith("@"):
                expanded.append(tok)
        for a in fallback_aug:
            a["note"] = "auto-injected by gmake_dryrun (SysConfig dry-run empty)"
            augmentations.append(a)

    # Normalize compiler path: if the literal path extracted from the make
    # output doesn't exist (common cross-platform trap where imports.mak
    # has a Linux-style default path), fall back to the basename so PATH
    # lookup finds the locally-installed compiler.
    compiler_path = Path(compiler)
    if not compiler_path.is_file():
        basename = compiler_path.name
        # Strip .exe for uniformity
        if basename.lower().endswith(".exe"):
            basename = basename[:-4]
        # If the basename is on PATH we use it; else keep the literal path so
        # the caller sees the right "not found" error.
        import shutil
        if shutil.which(basename):
            compiler = basename
        elif shutil.which(basename + ".exe"):
            compiler = basename + ".exe"

    details: dict = {
        "build_dir": str(build_dir),
        "make_path": make_exe,
        "make_version": make_version or "",
        "target": target,
        "duration_ms": duration_ms,
        "augmented_by": augmentations,
        "stderr_tail": (stderr or "")[-500:] if rc != 0 else "",
    }
    # If we had to skip earlier donor candidates, record that so
    # `.meta.json` shows the chain — useful for debugging surprising
    # donor selections (e.g. user prepended a target whose source is
    # absent and we silently fell back to a working one).
    if failures:
        details["donor_fallbacks"] = failures
        details["donor_candidates_tried"] = [c for c in candidates if c != target] + [target]

    return DiscoveryResult(
        compiler=compiler,
        flags=expanded,
        kind="gmake-dry-run",
        confidence="exact",
        details=details,
    )


__all__ = ["discover", "_pick_donor_target", "_pick_donor_targets"]
