"""Regression tests for the stdlib-only CSV-first entry script."""
from __future__ import annotations

import ast
import json
import os
import shutil
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
ENTRY = os.path.join(ROOT, "scripts", "generate_cross_site_purchase_comparison.py")
EX = os.path.join(ROOT, "examples", "synthetic")


def _load():
    import importlib.util
    spec = importlib.util.spec_from_file_location("csv_generate_rules_test", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        with _in_dir(tmp.name):
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
        with _in_dir(root):
            import importlib.util
            spec = importlib.util.spec_from_file_location("csv_generate_escape_test", ENTRY)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.generate()
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("not found in inputs_csv", result["error"].lower())

    def test_scope_file_name_with_folders_is_refused(self):
        root, _ = self.execute()
        scope_file = os.path.join(root, "scope.json")
        with open(scope_file, encoding="utf-8") as f:
            scope = json.load(f)
        scope["files"][0]["file"] = os.path.join(os.pardir, os.pardir, "secret.csv")
        with open(scope_file, "w", encoding="utf-8") as f:
            json.dump(scope, f)
        with _in_dir(root):
            import importlib.util
            spec = importlib.util.spec_from_file_location("csv_generate_name_test", ENTRY)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.generate()
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("bare .csv file name", result["error"].lower())


    def test_single_rules_json_gives_same_results_as_rules_folder(self):
        import csv as _csv
        results = {}
        for mode in ("folder", "json"):
            with tempfile.TemporaryDirectory() as tmp:
                os.makedirs(os.path.join(tmp, "inputs_csv"))
                shutil.copy(os.path.join(EX, "scope_csv.json"), tmp)
                shutil.copy(os.path.join(EX, "decisions_2_signed_off.json"), tmp)
                for name in os.listdir(os.path.join(EX, "inputs_csv")):
                    shutil.copy(os.path.join(EX, "inputs_csv", name), os.path.join(tmp, "inputs_csv"))
                if mode == "folder":
                    shutil.copytree(os.path.join(EX, "rules"), os.path.join(tmp, "rules"))
                else:
                    shutil.copy(os.path.join(EX, "rules.json"), tmp)
                with _in_dir(tmp):
                    report = _load().generate()
                self.assertEqual(report["status"], "FINAL")
                with open(os.path.join(tmp, "out", "opportunities.csv"), encoding="utf-8") as f:
                    results[mode] = list(_csv.reader(f))
        self.assertEqual(results["folder"], results["json"])

    def test_rules_json_missing_section_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "inputs_csv"))
            shutil.copy(os.path.join(EX, "scope_csv.json"), tmp)
            for name in os.listdir(os.path.join(EX, "inputs_csv")):
                shutil.copy(os.path.join(EX, "inputs_csv", name), os.path.join(tmp, "inputs_csv"))
            with open(os.path.join(tmp, "rules.json"), "w", encoding="utf-8") as f:
                f.write('{"settings": {}}')
            with _in_dir(tmp):
                report = _load().generate()
            self.assertNotIn(report.get("status"), ("STOPPED_AT_GATE_1", "DRAFT", "FINAL"))
            self.assertIn("missing section", json.dumps(report))

    def test_flat_staging_without_inputs_csv_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(os.path.join(EX, "scope_csv.json"), os.path.join(tmp, "scope.json"))
            shutil.copy(os.path.join(EX, "rules.json"), os.path.join(tmp, "rules.json"))
            shutil.copy(os.path.join(EX, "decisions_2_signed_off.json"), os.path.join(tmp, "decisions.json"))
            for name in os.listdir(os.path.join(EX, "inputs_csv")):
                shutil.copy(os.path.join(EX, "inputs_csv", name), os.path.join(tmp, name))
            with _in_dir(tmp):
                report = _load().generate()
            self.assertEqual(report["status"], "FINAL")
            self.assertTrue(os.path.isfile(os.path.join(tmp, "out", "opportunities.csv")))

if __name__ == "__main__":
    unittest.main()
