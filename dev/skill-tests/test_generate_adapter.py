"""Tests for the deterministic Process Studio entry point."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest


import contextlib as _contextlib


@_contextlib.contextmanager
def _in_dir(folder):
    previous = os.getcwd()
    os.chdir(folder)
    try:
        yield
    finally:
        os.chdir(previous)


ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skills", "cross-site-purchase-comparison")
ENTRY_PATH = os.path.join(ROOT, "scripts", "generate_cross_site_purchase_comparison.py")
EX = os.path.join(ROOT, "examples", "synthetic")

_spec = importlib.util.spec_from_file_location("ppc_process_studio_entry", ENTRY_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Could not load Process Studio entry point: {ENTRY_PATH}")
entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entry)


class TestGenerateEntryPoint(unittest.TestCase):
    def test_script_is_in_discoverable_scripts_folder(self):
        self.assertTrue(os.path.isfile(ENTRY_PATH))
        self.assertEqual(os.path.basename(ENTRY_PATH), "generate_cross_site_purchase_comparison.py")

    def test_entry_exposes_cwd_staged_generator(self):
        self.assertTrue(callable(entry.generate))
        with tempfile.TemporaryDirectory() as staged:
            import shutil
            shutil.copy(os.path.join(EX, "scope_csv.json"), os.path.join(staged, "scope.json"))
            shutil.copytree(os.path.join(EX, "rules"), os.path.join(staged, "rules"))
            shutil.copytree(os.path.join(EX, "inputs_csv"), os.path.join(staged, "inputs_csv"))
            shutil.copy(os.path.join(EX, "decisions_0_start.json"), os.path.join(staged, "decisions.json"))
            with _in_dir(staged):
                result = entry.generate()
            self.assertEqual(result["status"], "STOPPED_AT_GATE_1")
            self.assertTrue(os.path.isfile(os.path.join(staged, "out", "run_report.json")))


if __name__ == "__main__":
    unittest.main()
