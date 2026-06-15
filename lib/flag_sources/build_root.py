"""Scored ranker that picks the build directory for a given source file.

Given a source like `.../simplelink-lowpower-f3-sdk/.../hci.c`, the real
build dir is often far from the source — e.g. for BLE it's
`.../examples/rtos/LP_EM_CC2340R5/ble5stack/basic_ble/freertos/ticlang/`.
We score candidate directories by a bag of signals (makefile present,
ELF present, `.obj` siblings, projectspec, CU references in DWARF) and
return the highest scorer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BuildDirCandidate:
    path: Path
    score: int = 0
    signals: dict = field(default_factory=dict)


_HEAVY_DIRS = {
    ".git", "node_modules", ".venv", "target", "vendor",
    "third_party", "cmake-build-debug", "cmake-build-release",
    "__pycache__", ".pytest_cache", "build_tmp",
}


def _is_heavy(path: Path) -> bool:
    return path.name in _HEAVY_DIRS or path.name.startswith("cmake-build-")


def _has_makefile(d: Path) -> tuple[bool, int]:
    """Return (has_makefile, score_boost) based on content inspection."""
    for name in ("makefile", "Makefile", "GNUmakefile"):
        p = d / name
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:20_000]
            except OSError:
                return True, 3
            has_cc_rule = "$(CC)" in text or "-c $<" in text or "$(CXX)" in text
            has_obj_rule = ".obj:" in text or ".o:" in text
            boost = 5 if has_cc_rule else 3
            if has_obj_rule:
                boost += 1
            return True, boost
    return False, 0


def _has_projectspec(d: Path) -> bool:
    try:
        return any(d.glob("*.projectspec"))
    except OSError:
        return False


def _has_obj(d: Path) -> int:
    """Return count of .obj/.o files in d (non-recursive)."""
    n = 0
    try:
        for p in d.iterdir():
            if p.suffix.lower() in (".obj", ".o") and p.is_file():
                n += 1
                if n >= 3:
                    break
    except OSError:
        pass
    return n


def _has_linked_elf(d: Path) -> bool:
    try:
        for ext in ("*.out", "*.elf", "*.axf"):
            for _ in d.glob(ext):
                return True
    except OSError:
        pass
    return False


def _has_ti_opt(d: Path) -> bool:
    try:
        for p in d.glob("ti_*.opt"):
            if p.is_file():
                return True
    except OSError:
        pass
    return False


def _source_referenced_in_makefile(d: Path, source: Path) -> bool:
    """Check if source's name or relative path appears in any makefile here."""
    names = [source.name, source.stem + ".obj", source.stem + ".o"]
    for name in ("makefile", "Makefile", "GNUmakefile"):
        p = d / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:100_000]
        except OSError:
            continue
        for needle in names:
            if needle in text:
                return True
    for p in d.glob("*.mk"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:100_000]
        except OSError:
            continue
        for needle in names:
            if needle in text:
                return True
    return False


def _source_referenced_in_elf(d: Path, source: Path) -> bool:
    """Quick check via strings — cheap, avoids full DWARF parse."""
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError:
        return False

    source_name = source.name.encode()
    for ext in ("*.out", "*.elf", "*.axf"):
        for elf_p in d.glob(ext):
            try:
                # Fast path: search the raw ELF for the source filename as
                # bytes. DWARF string tables contain the CU names.
                data = elf_p.read_bytes()
            except OSError:
                continue
            # Cap to 20 MB — larger ELFs get a real DWARF scan instead
            if len(data) > 20 * 1024 * 1024:
                try:
                    with open(elf_p, "rb") as f:
                        elf = ELFFile(f)
                        if not elf.has_dwarf_info(strict=False):
                            continue
                        dwarf = elf.get_dwarf_info()
                        for cu in dwarf.iter_CUs():
                            top_die = cu.get_top_DIE()
                            na = top_die.attributes.get("DW_AT_name")
                            if na is None:
                                continue
                            nv = na.value
                            name = nv.decode("utf-8", "replace") if isinstance(nv, bytes) else str(nv)
                            if Path(name).stem == source.stem:
                                return True
                except Exception:
                    continue
            else:
                if source_name in data:
                    return True
    return False


def _score(d: Path, source: Path) -> BuildDirCandidate:
    cand = BuildDirCandidate(path=d)

    has_mk, mk_boost = _has_makefile(d)
    if has_mk:
        cand.score += mk_boost
        cand.signals["makefile"] = True
    has_ps = _has_projectspec(d)
    if has_ps:
        cand.score += 2
        cand.signals["projectspec"] = True
    obj_count = _has_obj(d)
    if obj_count > 0:
        cand.score += 4 if obj_count >= 3 else 2
        cand.signals["obj_count"] = obj_count
    if _has_linked_elf(d):
        cand.score += 4
        cand.signals["linked_elf"] = True
    if _has_ti_opt(d):
        cand.score += 2
        cand.signals["ti_opt"] = True
    if has_mk and _source_referenced_in_makefile(d, source):
        cand.score += 6
        cand.signals["source_in_makefile"] = True
    if _source_referenced_in_elf(d, source):
        cand.score += 8
        cand.signals["source_in_elf"] = True

    return cand


def _iter_candidates(
    source: Path,
    project_root: Path,
    context: dict,
) -> list[Path]:
    """Collect candidate directories, deduped.

    Strategy: prioritize dirs that contain a linked ELF over dirs that
    merely share a name like `ticlang/`. This is what lets us pick the
    actual basic_ble/freertos/ticlang dir out of dozens of ticlang
    siblings in the TI SDK tree.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    heavy_skip = {"simplelink-lowpower-f2-sdk", "simplelink-lowpower-f3-sdk",
                  "simplelink-cc13xx-cc26xx-sdk"}

    def _add(d: Path):
        try:
            if not d.is_dir():
                return
            rp = d.resolve()
        except OSError:
            return
        if rp in seen or _is_heavy(rp):
            return
        seen.add(rp)
        out.append(rp)

    # 1) ELFs listed in context — highest priority
    if isinstance(context, dict):
        for elf in context.get("elf_files") or []:
            p = Path(elf)
            if p.is_file():
                _add(p.parent)
        # 2) build_dirs published by detect-project.sh
        for bd in context.get("build_dirs") or []:
            if isinstance(bd, dict) and bd.get("path"):
                _add(Path(bd["path"]))
            elif isinstance(bd, str):
                _add(Path(bd))

    # 3) Dynamic ELF discovery: every *.out/*.elf/*.axf under project_root
    #    becomes a candidate (its parent dir). This catches TI builds where
    #    detect-project.sh's maxdepth missed them.
    try:
        import os as _os
        for root_dir, dirs, files in _os.walk(project_root):
            # Prune heavy dirs at every level (git/venv/vendor/SDK subtrees)
            dirs[:] = [d for d in dirs if not _is_heavy(Path(d))
                       and d not in heavy_skip]
            for fn in files:
                if fn.endswith((".out", ".elf", ".axf")):
                    _add(Path(root_dir))
                    break
            if len(out) > 60:
                break
    except OSError:
        pass

    # 4) Source's parents up to 8 levels
    parent = source.parent
    for _ in range(8):
        _add(parent)
        if parent == parent.parent:
            break
        parent = parent.parent

    # 5) project_root + common build dirs under it
    _add(project_root)
    for name in ("build", "Debug", "Release", "out", ".loci-build"):
        _add(project_root / name)

    # 6) Shallow glob for ticlang/gcc/iar sibling dirs under project_root
    for marker in ("ticlang", "gcc", "iar"):
        try:
            count = 0
            for d in project_root.rglob(marker):
                if d.is_dir() and not _is_heavy(d.parent):
                    _add(d)
                    count += 1
                    if count > 60:
                        break
        except OSError:
            pass

    return out[:80]


def find_build_root(
    source: Path,
    project_root: Path,
    context: dict,
) -> Path | None:
    """Return the highest-scoring build directory, or None if no viable one."""
    candidates = _iter_candidates(source, project_root, context)
    scored: list[BuildDirCandidate] = []
    for d in candidates:
        cand = _score(d, source)
        if cand.score > 0:
            scored.append(cand)

    if not scored:
        return None

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[0].path


def find_build_root_verbose(
    source: Path,
    project_root: Path,
    context: dict,
) -> list[BuildDirCandidate]:
    """Return all scored candidates (for debugging / provenance)."""
    candidates = _iter_candidates(source, project_root, context)
    scored = [_score(d, source) for d in candidates]
    scored = [c for c in scored if c.score > 0]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


__all__ = ["find_build_root", "find_build_root_verbose", "BuildDirCandidate"]
