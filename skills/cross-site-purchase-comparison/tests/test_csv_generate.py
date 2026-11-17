"""Regression tests for the stdlib-only CSV-first entry script."""
from __future__ import annotations

import ast
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "scripts", "generate_cross_site_purchase_comparison.py")
EX = os.path.join(ROOT, "examples", "synthetic")


class TestCsvGenerate(unittest.TestCase):
    def staged(self, tmp, decisions="decisions_2_signed_off.json"):
        shutil.copy(os.path.join(EX, "scope_csv.json"), os.path.join(tmp, "scope.json"))
        shutil.copytree(os.path.join(EX, "rules"), os.path.join(tmp, "rules"))
        shutil.copytree(os.path.join(EX, "inputs_csv"), os.path.join(tmp, "inputs_csv"))
        shutil.copy(os.path.join(EX, decisions), os.path.join(tmp, "decisions.json"))

    def execute(self, decisions="decisions_2_signed_off.json"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.staged(tmp.name, decisions)
        with patch("os.getcwd", return_value=tmp.name):
            # Entry discovers fixed names under the current working directory.
            import importlib.util
            spec = importlib.util.spec_from_file_location("csv_generate_test", ENTRY)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.generate()
        return tmp.name, result

    def test_script_uses_only_stdlib_and_no_local_runtime_imports(self):
        with open(ENTRY, encoding="utf-8") as source:
            tree = ast.parse(source.read())
        imports = {alias.name.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module.split(".")[0] for node in ast.walk(tree)
                       if isinstance(node, ast.ImportFrom) and node.module)
        self.assertFalse(imports & {"ppc", "run", "openpyxl"})
        self.assertEqual(os.path.basename(ENTRY), "generate_cross_site_purchase_comparison.py")

    def test_final_run_writes_json_and_both_csv_artifacts(self):
        root, result = self.execute()
        self.assertEqual(result["status"], "FINAL")
        out = os.path.join(root, "out")
        for name in ("run_report.json", "opportunities.csv", "needs_review.csv"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        with open(os.path.join(out, "opportunities.csv"), newline="", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Plant A", text)
        self.assertIn("20000.00", text)

    def test_gate_one_is_explicit_and_does_not_create_opportunities(self):
        root, result = self.execute("decisions_0_start.json")
        self.assertEqual(result["status"], "STOPPED_AT_GATE_1")
        with open(os.path.join(root, "out", "opportunities.csv"), newline="", encoding="utf-8") as f:
            self.assertEqual(list(__import__("csv").reader(f)), [])

    def test_scope_symlink_escape_fails_closed(self):
        root, _ = self.execute()
        input_file = os.path.join(root, "inputs_csv", "plant_A_purchases.csv")
        outside = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        outside.close()
        self.addCleanup(lambda: os.path.exists(outside.name) and os.unlink(outside.name))
        os.unlink(input_file)
        os.symlink(outside.name, input_file)
        with patch("os.getcwd", return_value=root):
            import importlib.util
            spec = importlib.util.spec_from_file_location("csv_generate_escape_test", ENTRY)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.generate()
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("outside", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
