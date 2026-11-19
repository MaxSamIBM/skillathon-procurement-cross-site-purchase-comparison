"""Regression tests for the stdlib-only CSV-first entry script."""
from __future__ import annotations

import ast
import json
import os
import shutil
import tempfile
import sys
import unittest
from unittest import mock


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
ENTRY = os.path.join(ROOT, "scripts", "compare_prices.py")
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
        self.assertEqual(os.path.basename(ENTRY), "compare_prices.py")

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


class BuiltInDemoTests(unittest.TestCase):
    """The built-in demo must run with no staged files, write nothing, and match the file-based run."""

    def _demo_in_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _in_dir(tmp):
                mod = _load()
                result = mod.demo()
                leftovers = os.listdir(tmp)
        return result, leftovers

    def test_demo_passes_all_three_stages_and_writes_nothing(self):
        result, leftovers = self._demo_in_empty_dir()
        self.assertEqual(result["result"], "PASS")
        self.assertEqual([s["status"] for s in result["stages"]], ["STOPPED_AT_GATE_1", "DRAFT", "FINAL"])
        self.assertEqual(leftovers, [])

    def test_demo_figures(self):
        result, _ = self._demo_in_empty_dir()
        final = result["stages"][-1]
        savings = {o["plant"]: o["scenario_saving"] for o in final["opportunities"]}
        self.assertEqual(savings, {"Plant A": "20000.00", "Plant D": "2112.00"})
        self.assertTrue(all("PASS" in c for c in final["release_checks"]))
        draft = result["stages"][1]
        self.assertEqual([o["plant"] for o in draft["opportunities"]], ["Plant A"])

    def test_demo_csv_matches_file_based_run(self):
        result, _ = self._demo_in_empty_dir()
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "inputs_csv"))
            for name in ("scope_csv.json", "rules.json", "decisions_2_signed_off.json"):
                shutil.copy(os.path.join(EX, name), tmp)
            for name in os.listdir(os.path.join(EX, "inputs_csv")):
                shutil.copy(os.path.join(EX, "inputs_csv", name), os.path.join(tmp, "inputs_csv"))
            with _in_dir(tmp):
                _load().generate()
            with open(os.path.join(tmp, "out", "opportunities.csv"), newline="", encoding="utf-8") as f:
                on_disk = f.read()
        self.assertEqual(result["stages"][-1]["opportunities_csv"], on_disk)

    def test_main_runs_demo_when_nothing_staged_and_real_run_when_staged(self):
        import contextlib
        import io as _io
        with tempfile.TemporaryDirectory() as tmp:
            with _in_dir(tmp):
                buf = _io.StringIO()
                with contextlib.redirect_stdout(buf), mock.patch.object(sys, "argv", ["compare_prices.py"]):
                    code = _load().main()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["mode"], "BUILT_IN_DEMO")
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("scope_csv.json", "rules.json", "decisions_0_start.json"):
                shutil.copy(os.path.join(EX, name), tmp)
            for name in os.listdir(os.path.join(EX, "inputs_csv")):
                shutil.copy(os.path.join(EX, "inputs_csv", name), tmp)
            with _in_dir(tmp):
                buf = _io.StringIO()
                with contextlib.redirect_stdout(buf), mock.patch.object(sys, "argv", ["compare_prices.py"]):
                    _load().main()
        self.assertEqual(json.loads(buf.getvalue())["status"], "STOPPED_AT_GATE_1")


class PlatformContractTests(unittest.TestCase):
    """Names the platform stages (uploaded_<name>, .txt appended) and the manifest the platform checks."""

    def _stage_uploaded(self, tmp, decisions=None):
        shutil.copy(os.path.join(EX, "scope_csv.json"), os.path.join(tmp, "uploaded_scope_csv.json.txt"))
        shutil.copy(os.path.join(EX, "rules.json"), os.path.join(tmp, "uploaded_rules.json.txt"))
        for name in os.listdir(os.path.join(EX, "inputs_csv")):
            shutil.copy(os.path.join(EX, "inputs_csv", name), os.path.join(tmp, "uploaded_" + name + ".txt"))
        if decisions:
            shutil.copy(os.path.join(EX, decisions), os.path.join(tmp, "decisions.json"))

    def test_uploaded_names_run_end_to_end(self):
        for decisions, status in ((None, "STOPPED_AT_GATE_1"), ("decisions_1_scope_confirmed.json", "DRAFT"),
                                  ("decisions_2_signed_off.json", "FINAL")):
            with tempfile.TemporaryDirectory() as tmp:
                self._stage_uploaded(tmp, decisions)
                with _in_dir(tmp):
                    report = _load().generate()
                self.assertEqual(report["status"], status, decisions)

    def test_uploaded_names_give_same_result_as_plain_names(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self._stage_uploaded(a, "decisions_2_signed_off.json")
            os.makedirs(os.path.join(b, "inputs_csv"))
            for name in ("scope_csv.json", "rules.json"):
                shutil.copy(os.path.join(EX, name), b)
            shutil.copy(os.path.join(EX, "decisions_2_signed_off.json"), os.path.join(b, "decisions.json"))
            for name in os.listdir(os.path.join(EX, "inputs_csv")):
                shutil.copy(os.path.join(EX, "inputs_csv", name), os.path.join(b, "inputs_csv"))
            for folder in (a, b):
                with _in_dir(folder):
                    _load().generate()
            for name in ("opportunities.csv", "needs_review.csv"):
                with open(os.path.join(a, "out", name), encoding="utf-8") as fa, open(os.path.join(b, "out", name), encoding="utf-8") as fb:
                    self.assertEqual(fa.read(), fb.read(), name)

    def test_plain_decisions_file_beats_uploaded_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._stage_uploaded(tmp, "decisions_2_signed_off.json")
            shutil.copy(os.path.join(EX, "decisions_0_start.json"), os.path.join(tmp, "uploaded_decisions.json.txt"))
            with _in_dir(tmp):
                report = _load().generate()
            self.assertEqual(report["status"], "FINAL")

    def test_two_files_with_the_same_original_name_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._stage_uploaded(tmp)
            shutil.copy(os.path.join(EX, "rules.json"), os.path.join(tmp, "uploaded_rules.json"))
            with _in_dir(tmp):
                report = _load().generate()
            self.assertEqual(report["status"], "ERROR")
            self.assertIn("attach it only once", report["error"])

    def test_manifest_declares_the_entry_script_with_valid_arguments(self):
        scripts = os.path.join(ROOT, "scripts")
        with open(os.path.join(scripts, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertIn("compare_prices.py", manifest["scripts"])
        self.assertTrue(os.path.isfile(os.path.join(scripts, "compare_prices.py")))
        self.assertFalse(any(n.startswith("generate_") for n in os.listdir(scripts)))
        for flag, spec in manifest["scripts"]["compare_prices.py"]["arguments"].items():
            self.assertRegex(flag, r"^--[A-Za-z0-9-]+$")  # letters, digits, hyphens only
            self.assertIn(spec["type"], ("enum", "string", "input_path", "output_path"))
            for value in spec.get("values", []):
                self.assertLessEqual(len(value), 4096)
                self.assertNotRegex(value, r"[\r\n]")

    def test_frontmatter_declares_what_the_platform_needs(self):
        with open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8") as f:
            head = f.read().split("---")[1]
        for needed in ("type: workflow", "multi_turn: true", "run_skill_script", "list_temp_files", "write_temp_file", "save_output_file"):
            self.assertIn(needed, head)


class DecisionsShapeTests(unittest.TestCase):
    """A decisions file with the wrong shape must be an error, not a silent 'nothing decided'."""

    def _run(self, decisions_text):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("scope_csv.json", "rules.json"):
                shutil.copy(os.path.join(EX, name), tmp)
            for name in os.listdir(os.path.join(EX, "inputs_csv")):
                shutil.copy(os.path.join(EX, "inputs_csv", name), tmp)
            with open(os.path.join(tmp, "decisions.json"), "w", encoding="utf-8") as f:
                f.write(decisions_text)
            with _in_dir(tmp):
                return _load().generate()

    def test_top_level_confirmed_is_rejected_with_a_helpful_message(self):
        report = self._run('{"gate1_confirmed": true}')
        self.assertEqual(report["status"], "ERROR")
        self.assertIn("gate1_confirmed", report["error"])
        self.assertIn('"gate1"', report["error"])

    def test_gate1_as_a_string_is_rejected(self):
        report = self._run('{"gate1": "confirmed"}')
        self.assertEqual(report["status"], "ERROR")

    def test_correct_shape_advances_to_draft_and_records_the_file(self):
        report = self._run('{"gate1": {"confirmed": true, "by": "Procurement consultant (role)"}, '
                           '"column_overrides": {"Plant D": {"Order Qty (M)": "quantity", "Price per M": "unit_price"}}}')
        self.assertEqual(report["status"], "DRAFT")
        self.assertEqual(report["inputs"]["decisions_file"], "decisions.json")
