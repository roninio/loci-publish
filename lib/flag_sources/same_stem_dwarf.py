"""Step 4: DWARF from a `.o`/`.obj` with the same stem as the source.

Covers the typical CMake / plain-Make out-of-tree pattern where the per-TU
object lives in a build dir adjacent to the source.
"""
from __future__ import annotations

from pathlib import Path

from . import DiscoveryResult, parse_producer


def _find_same_stem(source: Path, build_dir: Path | None) -> list[Path]:
    stem = source.stem
    found: list[Path] = []
    seen: set[Path] = set()

    def _try(p: Path):
        if not p.is_file():
            return
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp in seen:
            return
        seen.add(rp)
        found.append(rp)

    # Siblings of the source
    for ext in (".o", ".obj"):
        _try(source.parent / (stem + ext))
    # In the build dir
    if build_dir is not None:
        for ext in (".o", ".obj"):
            _try(build_dir / (stem + ext))
    # Parents up to 3 levels — handles the common ../build/<stem>.o pattern
    parent = source.parent
    for _ in range(3):
        for ext in (".o", ".obj"):
            _try(parent / "build" / (stem + ext))
            _try(parent / ".loci-build" / (stem + ext))
        if parent == parent.parent:
            break
        parent = parent.parent
    return found


def _extract_from(obj_path: Path, source: Path) -> DiscoveryResult | None:
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError:
        return None

    try:
        with open(obj_path, "rb") as f:
            elf = ELFFile(f)
            if not elf.has_dwarf_info(strict=False):
                return None
            dwarf = elf.get_dwarf_info()

            best_parsed = None
            stem = source.stem

            for cu in dwarf.iter_CUs():
                top_die = cu.get_top_DIE()
                if top_die.tag != "DW_TAG_compile_unit":
                    continue

                producer_attr = top_die.attributes.get("DW_AT_producer")
                if producer_attr is None:
                    continue
                producer_val = producer_attr.value
                if isinstance(producer_val, bytes):
                    producer = producer_val.decode("utf-8", errors="replace")
                else:
                    producer = str(producer_val)

                name_attr = top_die.attributes.get("DW_AT_name")
                name_str = ""
                if name_attr is not None:
                    nv = name_attr.value
                    if isinstance(nv, bytes):
                        name_str = nv.decode("utf-8", errors="replace")
                    else:
                        name_str = str(nv)

                parsed = parse_producer(producer)
                if parsed.compiler is None:
                    continue

                if Path(name_str).stem == stem:
                    return DiscoveryResult(
                        compiler=parsed.compiler,
                        flags=list(parsed.flags),
                        kind="same-stem-dwarf",
                        confidence="high",
                        details={
                            "obj_path": str(obj_path),
                            "cu_name": name_str,
                            "compiler_version": parsed.version or "",
                        },
                        partial=(len(parsed.flags) == 0),
                    )

                if best_parsed is None and parsed.flags:
                    best_parsed = (parsed, name_str)

            if best_parsed is not None:
                parsed, name_str = best_parsed
                return DiscoveryResult(
                    compiler=parsed.compiler,
                    flags=list(parsed.flags),
                    kind="same-stem-dwarf",
                    confidence="medium",
                    details={
                        "obj_path": str(obj_path),
                        "cu_name": name_str,
                        "compiler_version": parsed.version or "",
                        "note": "stem did not match any CU; used first parseable CU",
                    },
                    partial=False,
                )
    except Exception:
        return None
    return None


def discover(
    source: Path,
    loci_target: str,
    project_root: Path,
    context: dict,
    build_dir: Path | None,
) -> DiscoveryResult | None:
    for obj_path in _find_same_stem(source, build_dir):
        result = _extract_from(obj_path, source)
        if result is not None:
            return result
    return None


__all__ = ["discover"]
