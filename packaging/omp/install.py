#!/usr/bin/env python3
"""Checkout entrypoint for the wheel-bundled, pinned OMP installer."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[2] / "src/oicoach_runtime/bundled/omp/install.py"), run_name="__main__")
