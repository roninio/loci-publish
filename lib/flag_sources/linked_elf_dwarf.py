"""Step 5: DWARF from a linked ELF.

Iterates compile units; prefers the CU whose `DW_AT_name` resolves to the
source file (taking `DW_AT_comp_dir` into account). Falls back to any CU
with a non-trivial producer string.
"""
from __future__ import annotations

from pathlib import Path

from . import DiscoveryResult, parse_producer


def _iter_elf_candidates(
    source: Path,
    project_root: Path,
    context: dict,
    build_dir: Path | None,
) -> list[Path]:
    """Collect ELF paths to scan, in priority order."""
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path):
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp in seen or not rp.is_file():
            return
        seen.add(rp)
        candidates.append(rp)

    # 1) build_dir — linked ELF usually lives here for TI/CMake
    if build_dir is not None:
        for ext in ("*.out", "*.elf", "*.axf"):
            for p in build_dir.glob(ext):
                _add(p)

    # 2) context.elf_files
    if isinstance(context, dict):
        for elf in context.get("elf_files") or []:
            _add(Path(elf))

    # 3) shallow search in project_root (depth 3) as last resort
    try:
        for depth in range(1, 4):
            pattern = "/".join(["*"] * depth)
            for ext in ("*.out", "*.elf", "*.axf"):
                for p in project_root.glob(f"{pattern}/{ext}"):
                    _add(p)
    except OSError:
        pass

    return candidates


def _cu_matches_source(name_str: str, comp_dir: str, source: Path) -> bool:
    if not name_str:
        return False
    # Direct stem match
    if Path(name_str).stem == source.stem:
        # Also check full-path match if possible
        cu_path = Path(name_str)
        if not cu_path.is_absolute() and comp_dir:
            cu_path = Path(comp_dir) / cu_path
        try:
            if cu_path.resolve() == source.resolve():
                return True
        except OSError:
            pass
        # Stem-only fallback — useful when comp_dir is missing
        return True
    return False


def _scan_elf(elf_path: Path, source: Path) -> DiscoveryResult | None:
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError:
        return None

    try:
        with open(elf_path, "rb") as f:
            elf = ELFFile(f)
            if not elf.has_dwarf_info(strict=False):
                return None
            dwarf = elf.get_dwarf_info()

            matched: DiscoveryResult | None = None
            fallback: DiscoveryResult | None = None

            for cu in dwarf.iter_CUs():
                top_die = cu.get_top_DIE()
                if top_die.tag != "DW_TAG_compile_unit":
                    continue

                producer_attr = top_die.attributes.get("DW_AT_producer")
                if producer_attr is None:
                    continue
                pv = producer_attr.value
                producer = pv.decode("utf-8", errors="replace") if isinstance(pv, bytes) else str(pv)

                parsed = parse_producer(producer)
                if parsed.compiler is None:
                    continue

                name_str = ""
                comp_dir = ""
                na = top_die.attributes.get("DW_AT_name")
                if na is not None:
                    nv = na.value
                    name_str = nv.decode("utf-8", errors="replace") if isinstance(nv, bytes) else str(nv)
                cd = top_die.attributes.get("DW_AT_comp_dir")
                if cd is not None:
                    cv = cd.value
                    comp_dir = cv.decode("utf-8", errors="replace") if isinstance(cv, bytes) else str(cv)

                is_match = _cu_matches_source(name_str, comp_dir, source)

                result = DiscoveryResult(
                    compiler=parsed.compiler,
                    flags=list(parsed.flags),
                    kind="linked-elf-dwarf",
                    confidence="high" if is_match else "medium",
                    details={
                        "elf_path": str(elf_path),
                        "cu_name": name_str,
                        "cu_comp_dir": comp_dir,
                        "compiler_version": parsed.version or "",
                        "stem_match": is_match,
                    },
                    partial=(len(parsed.flags) == 0),
                )

                if is_match:
                    # Take the first exact match
                    matched = result
                    break
                if fallback is None and parsed.flags:
                    fallback = result

            return matched or fallback
    except Exception:
        return None


def discover(
    source: Path,
    loci_target: str,
    project_root: Path,
    context: dict,
    build_dir: Path | None,
) -> DiscoveryResult | None:
    for elf_path in _iter_elf_candidates(source, project_root, context, build_dir):
        result = _scan_elf(elf_path, source)
        if result is not None:
            return result
    return None


__all__ = ["discover"]
