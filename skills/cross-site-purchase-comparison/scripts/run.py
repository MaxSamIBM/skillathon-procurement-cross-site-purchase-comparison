#!/usr/bin/env python3
"""Local CLI wrapper for the skill-root runner."""
from __future__ import annotations

import importlib.util
import os
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

_spec = importlib.util.spec_from_file_location("_cross_site_purchase_comparison_runner", os.path.join(SKILL_DIR, "run.py"))
if _spec is None or _spec.loader is None:
    raise ImportError("Could not load the skill-root run.py")
_runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runner)

# Keep the runner's public names available to local scripts and tests.
for _name in dir(_runner):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_runner, _name)

if __name__ == "__main__":
    sys.exit(_runner.main())
