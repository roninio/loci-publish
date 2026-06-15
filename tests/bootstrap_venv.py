#!/usr/bin/env python3
"""Bootstrap the plugin .venv and install test dependencies.

Creates the same Python 3.12 venv that setup.sh creates, installs
loci-service-asmslicer from PyPI, and adds pytest + test deps.

Usage:
    python tests/bootstrap_venv.py          # create/update venv
    python tests/bootstrap_venv.py --run    # create/update, then run pytest with forwarded args

After bootstrapping, run tests with:
    .venv/Scripts/python -m pytest           (Windows)
    .venv/bin/python -m pytest               (Unix)
"""

import os
import platform
import subprocess
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = PLUGIN_DIR / ".venv"


def _venv_python() -> Path:
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _run(cmd, **kwargs):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True, **kwargs)


def bootstrap():
    # 1. Check uv is available
    try:
        subprocess.run(["uv", "--version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("ERROR: 'uv' not found. Install it first: https://docs.astral.sh/uv/")
        sys.exit(1)

    # 2. Create venv if missing OR wrong Python version.
    # A stale 3.11 venv from a prior install would otherwise be silently reused.
    vpy_path = _venv_python()
    pyver = None
    if vpy_path.is_file():
        try:
            out = subprocess.check_output(
                [str(vpy_path), "-c",
                 "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                stderr=subprocess.DEVNULL, timeout=10,
            ).decode().strip()
            pyver = out
        except (OSError, subprocess.SubprocessError):
            pyver = None

    if not VENV_DIR.is_dir() or pyver != "3.12":
        if VENV_DIR.is_dir() and pyver != "3.12":
            print(f"Venv has Python {pyver or 'unknown'} (need 3.12) — rebuilding...")
            import shutil as _shutil
            _shutil.rmtree(VENV_DIR, ignore_errors=True)
        else:
            print("Creating Python 3.12 venv...")
        _run(["uv", "venv", "--python", "3.12", str(VENV_DIR)])
    else:
        print(f"Venv exists: {VENV_DIR} (Python {pyver})")

    vpy = str(_venv_python())
    env = {**os.environ, "VIRTUAL_ENV": str(VENV_DIR)}

    # 3. Install asmslicer from PyPI
    print("Installing asmslicer from PyPI...")
    _run(
        ["uv", "pip", "install", "loci_service_asmslicer"],
        env=env,
    )

    # 4. Install runtime deps (pandas, etc.) and test deps
    print("Installing runtime + test dependencies...")
    _run(
        ["uv", "pip", "install", "pandas", "pydot", "unicorn",
         "pytest>=8.0", "pytest-timeout>=2.2"],
        env=env,
    )

    # 5. Verify asmslicer import
    print("Verifying asmslicer import...")
    result = subprocess.run(
        [vpy, "-c", "from loci.service.asmslicer import asmslicer; print('OK')"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  asmslicer: {result.stdout.strip()}")
    else:
        # Try to auto-install missing deps (same logic as setup.sh)
        for attempt in range(5):
            err = result.stderr
            if "ModuleNotFoundError" not in err:
                break
            missing = err.split("No module named '")[1].split("'")[0]
            print(f"  Installing undeclared dependency: {missing}")
            _run(["uv", "pip", "install", missing], env=env)
            result = subprocess.run(
                [vpy, "-c", "from loci.service.asmslicer import asmslicer; print('OK')"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"  asmslicer: {result.stdout.strip()}")
                break
        else:
            print(f"  WARNING: asmslicer import failed: {result.stderr.strip()}")

    print(f"\nDone. Run tests with:\n  {vpy} -m pytest\n")
    return vpy


if __name__ == "__main__":
    vpy = bootstrap()

    if "--run" in sys.argv:
        # Forward remaining args to pytest
        extra = [a for a in sys.argv[1:] if a != "--run"]
        os.execv(vpy, [vpy, "-m", "pytest"] + extra)
