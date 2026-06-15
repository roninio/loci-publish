"""Step 3: DWARF from a sibling `.obj`/`.o` in the discovered build dir.

When CFLAGS are uniform across TUs (TI gmake, CMake out-of-source, Zephyr,
U-Boot), any peer TU's DWARF gives us the exact same flag set the real
build uses. This is how we recover flags for sources in pre-built
libraries (e.g. `hci.c` in TI's OneLib.a / StackWrapper.a) that have no
same-stem object next to them.
"""
from __future__ import annotations

from pathlib import Path

from . import DiscoveryResult, parse_producer
from .flags_normalize import strip_source_and_output


def _enumerate_objs(build_dir: Path) -> list[Path]:
    """Return `.obj`/`.o` files in build_dir (non-recursive, sorted)."""
    out: list[Path] = []
    try:
        for p in build_dir.iterdir():
            if p.is_file() and p.suffix.lower() in (".obj", ".o"):
                out.append(p)
    except OSError:
        return []
    # Stable order so repeated runs pick the same donor
    out.sort(key=lambda p: p.name)
    return out


def _producer_from_obj(obj_path: Path) -> tuple[str, str, str] | None:
    """Return (producer, cu_name, comp_dir) from the first CU with a producer."""
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
            for cu in dwarf.iter_CUs():
                top_die = cu.get_top_DIE()
                if top_die.tag != "DW_TAG_compile_unit":
                    continue
                pa = top_die.attributes.get("DW_AT_producer")
                if pa is None:
                    continue
                pv = pa.value
                producer = pv.decode("utf-8", "replace") if isinstance(pv, bytes) else str(pv)

                name = ""
                na = top_die.attributes.get("DW_AT_name")
                if na is not None:
                    nv = na.value
                    name = nv.decode("utf-8", "replace") if isinstance(nv, bytes) else str(nv)
                comp_dir = ""
                cd = top_die.attributes.get("DW_AT_comp_dir")
                if cd is not None:
                    cv = cd.value
                    comp_dir = cv.decode("utf-8", "replace") if isinstance(cv, bytes) else str(cv)

                return producer, name, comp_dir
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
    if build_dir is None or not build_dir.is_dir():
        return None

    objs = _enumerate_objs(build_dir)
    if not objs:
        return None

    best: DiscoveryResult | None = None
    scanned: list[str] = []

    for obj_path in objs[:12]:
        scanned.append(obj_path.name)
        info = _producer_from_obj(obj_path)
        if info is None:
            continue
        producer, cu_name, comp_dir = info
        parsed = parse_producer(producer)
        if parsed.compiler is None:
            continue

        flags = strip_source_and_output(parsed.flags)

        has_includes = any(f.startswith("-I") or f == "-isystem" for f in flags)
        confidence = "high" if has_includes else "medium"
        partial = not has_includes

        result = DiscoveryResult(
            compiler=parsed.compiler,
            flags=flags,
            kind="sibling-obj-dwarf",
            confidence=confidence,
            details={
                "donor_obj": str(obj_path),
                "donor_cu_name": cu_name,
                "donor_comp_dir": comp_dir,
                "compiler_version": parsed.version or "",
                "scanned_count": len(scanned),
            },
            partial=partial,
        )
        if has_includes:
            return result
        if best is None:
            best = result

    if best is not None:
        best.details["scanned_count"] = len(scanned)
        best.details["note"] = "no donor contained -I flags in DWARF; returning partial"
    return best


__all__ = ["discover"]
