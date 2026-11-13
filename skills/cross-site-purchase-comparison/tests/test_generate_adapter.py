"""Best-effort conventional entry-point adapter tests."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import generate  # noqa: E402

EX = os.path.join(ROOT, "examples", "synthetic")


class TestGenerateAdapter(unittest.TestCase):
    def test_staged_path_list_runs_existing_pipeline(self):
        paths = [
            os.path.join(EX, "scope.json"),
            os.path.join(EX, "rules"),
            os.path.join(EX, "decisions_1_scope_confirmed.json"),
        ]
        with tempfile.TemporaryDirectory(dir=ROOT) as out:
            # Staged paths are supplied in the payload; output remains confined to the
            # project working folder by the adapter's path policy.
            result = json.loads(generate.generate(paths, out_dir=out))
            self.assertEqual(result["status"], "DRAFT")
            self.assertTrue(os.path.isfile(os.path.join(out, "run_report.json")))
            self.assertTrue(os.path.isfile(os.path.join(out, "price_comparison_draft.xlsx")))

    def test_discovers_scope_rules_and_decisions_from_working_directory(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory(dir=ROOT) as work:
            import shutil
            shutil.copy2(os.path.join(EX, "scope.json"), work)
            shutil.copy2(os.path.join(EX, "decisions_0_start.json"), os.path.join(work, "decisions.json"))
            shutil.copytree(os.path.join(EX, "rules"), os.path.join(work, "rules"))
            shutil.copytree(os.path.join(EX, "inputs"), os.path.join(work, "inputs"))
            os.chdir(work)
            try:
                result = json.loads(generate.generate())
                self.assertEqual(result["status"], "STOPPED_AT_GATE_1")
                self.assertTrue(os.path.isfile(os.path.join(work, "out", "run_report.json")))
            finally:
                os.chdir(old_cwd)

    def test_rejects_path_outside_working_directory(self):
        outside = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        outside.close()
        try:
            with self.assertRaises(ValueError):
                generate.generate({"scope_path": outside.name})
        finally:
            os.unlink(outside.name)


if __name__ == "__main__":
    unittest.main()
