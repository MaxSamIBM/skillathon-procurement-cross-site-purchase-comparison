"""Tests for cross-site-purchase-comparison 0.1.0 against the planted traps in examples/synthetic.

Run:  python3 -m unittest discover -s tests -v
Expected values come from examples/synthetic/expected/expected_final.json, which was calculated by hand
before the engine was written."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal

from openpyxl import load_workbook

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import run as runner  # noqa: E402
from ppc.rules import Rules, RulesError  # noqa: E402

EX = os.path.join(ROOT, "examples", "synthetic")
RULES = os.path.join(EX, "rules")
SCOPE = os.path.join(EX, "scope.json")
with open(os.path.join(EX, "expected", "expected_final.json")) as f:
    EXPECTED = json.load(f)


def _run(decisions_name, scope=SCOPE, rules=RULES):
    out = tempfile.mkdtemp(prefix="ppc_test_")
    rep = runner.run(scope, rules, os.path.join(EX, decisions_name), out)
    return rep, out


class TestGates(unittest.TestCase):
    def test_start_stops_at_gate_1(self):
        rep, _ = _run("decisions_0_start.json")
        self.assertEqual(rep["status"], EXPECTED["start_run"]["status"])
        text = " ".join(rep["gate1_open"])
        self.assertIn("Order Qty (M)", text)          # trap 4: unknown headers go to a person
        self.assertIn("Scope not confirmed", text)
        self.assertNotIn("workbook", rep)             # nothing produced before Gate 1

    def test_draft_holds_back_unapproved_match(self):
        rep, out = _run("decisions_1_scope_confirmed.json")
        self.assertEqual(rep["status"], "DRAFT")
        plants = [o["plant"] for o in rep["opportunities"]]
        self.assertEqual(plants, EXPECTED["draft_run"]["opportunity_plants"])   # trap 11: D not counted yet
        self.assertEqual(rep["opportunities"][0]["scenario_saving"], Decimal("20000.00"))
        self.assertEqual([q["plant"] for q in rep["gate2_queue"]], ["Plant D"])
        self.assertTrue(os.path.isfile(os.path.join(out, "price_comparison_draft.xlsx")))

    def test_final_requires_gate3(self):
        rep, out = _run("decisions_2_signed_off.json")
        self.assertEqual(rep["status"], "FINAL")
        self.assertTrue(os.path.isfile(os.path.join(out, "price_comparison_final.xlsx")))


class TestFinalResults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep, cls.out = _run("decisions_2_signed_off.json")
        cls.wb = load_workbook(os.path.join(cls.out, "price_comparison_final.xlsx"))

    def test_opportunities_match_hand_calculation(self):
        got = self.rep["opportunities"]
        exp = EXPECTED["opportunities"]
        self.assertEqual(len(got), len(exp))
        for g, e in zip(got, exp):
            self.assertEqual(g["rank"], e["rank"])
            self.assertEqual(g["plant"], e["plant"])
            self.assertEqual(g["price_per_1000"], Decimal(str(e["price_per_1000"])).quantize(Decimal("0.01")))
            self.assertEqual(g["benchmark_price_per_1000"], Decimal("60.00"))
            self.assertEqual(g["benchmark_plant"], EXPECTED["benchmark"]["plant"])
            self.assertEqual(int(g["labels"]), e["labels"])
            self.assertEqual(g["scenario_saving"], Decimal(str(e["scenario_saving"])).quantize(Decimal("0.01")))

    def test_roll_price_trap(self):
        # trap 7: B costs more per roll ($60 vs $35) but less per 1,000 labels ($60 vs $70)
        a = next(o for o in self.rep["opportunities"] if o["plant"] == "Plant A")
        self.assertGreater(a["price_per_1000"], a["benchmark_price_per_1000"])

    def test_all_release_checks_pass(self):
        fails = [c for c in self.rep["release_checks"] if c["result"] != "PASS"]
        self.assertEqual(fails, [])
        self.assertEqual(len(self.rep["release_checks"]), 7)

    def test_reconciliation_ties_to_stated_totals(self):
        for f in self.rep["files"]:
            self.assertEqual(f["reconciliation"]["row_check"], "PASS", f["plant"])
            self.assertEqual(f["reconciliation"]["total_check"], "PASS", f["plant"])

    def test_header_detection_on_messy_sheet(self):
        c = next(f for f in self.rep["files"] if f["plant"] == "Plant C")
        self.assertEqual(c["header_row"], 4)          # trap 3: title and blank rows above the header

    def test_only_data_sheet_read(self):
        d = next(f for f in self.rep["files"] if f["plant"] == "Plant D")
        self.assertEqual(d["sheet"], "Data")          # trap 6: Notes sheet ignored

    def _review_rows(self):
        ws = self.wb["Needs review"]
        return [r for r in ws.iter_rows(min_row=2, values_only=True) if any(r)]

    def test_needs_review_items(self):
        rows = self._review_rows()
        for item in EXPECTED["must_be_in_needs_review"]:
            hit = [r for r in rows if r[0] == "Needs review" and r[1] == item["plant"]
                   and item["reason_contains"].lower() in str(r[4]).lower()]
            self.assertTrue(hit, f"expected Needs review for {item}")

    def test_excluded_items(self):
        rows = self._review_rows()
        for item in EXPECTED["must_be_excluded"]:
            hit = [r for r in rows if r[0] == "Excluded" and r[1] == item["plant"]
                   and item["reason_contains"].lower() in str(r[4]).lower()]
            self.assertTrue(hit, f"expected Excluded for {item}")

    def test_spec_y_never_compared(self):
        # trap 8: no Plant C line appears behind any opportunity
        for o in self.rep["opportunities"]:
            self.assertFalse(any("plant_C" in r for r in o["rows"] + o["benchmark_rows"]))

    def test_cheaper_exw_price_is_not_benchmark(self):
        # trap 16: $52 EXW is cheaper but must not set the delivered benchmark
        for o in self.rep["opportunities"]:
            self.assertNotIn("plant_B_export.xlsx / Sheet1 / row 7", o["benchmark_rows"])
        # trap 15: $75 small spot order is not in the standard group either
        for o in self.rep["opportunities"]:
            self.assertNotIn("plant_B_export.xlsx / Sheet1 / row 5", o["benchmark_rows"])

    def test_fx_recorded(self):
        ws = self.wb["Comparability checks"]
        d_rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[0] == "Plant D" and r[9] == "EUR"]
        self.assertTrue(d_rows)
        self.assertTrue(all("FX-001" in str(r[10]) for r in d_rows))   # trap 10

    def test_workbook_tabs(self):
        for t in ("Summary", "Opportunities", "Supporting transactions", "Comparability checks", "Needs review"):
            self.assertIn(t, self.wb.sheetnames)

    def test_savings_are_labelled_scenarios(self):
        text = " ".join(str(c) for row in self.wb["Opportunities"].iter_rows(values_only=True) for c in row if c)
        self.assertIn("scenario", text.lower())
        self.assertIn("confirm", text.lower())


class TestFailures(unittest.TestCase):
    """The run must stop, not quietly carry on, when something is wrong."""

    def _copy_example(self):
        tmp = tempfile.mkdtemp(prefix="ppc_fail_")
        shutil.copytree(EX, os.path.join(tmp, "ex"))
        return os.path.join(tmp, "ex")

    def test_reconciliation_failure_blocks(self):
        ex = self._copy_example()
        path = os.path.join(ex, "inputs", "plant_A_purchases.xlsx")
        wb = load_workbook(path)
        ws = wb.active
        for row in ws.iter_rows():
            if str(row[0].value).strip().lower() == "total":
                amt_col = len(row) - 1
                for c in row:
                    if isinstance(c.value, (int, float)):
                        c.value = c.value + 1000      # the stated total no longer matches the rows
        wb.save(path)
        out = tempfile.mkdtemp()
        rep = runner.run(os.path.join(ex, "scope.json"), os.path.join(ex, "rules"),
                         os.path.join(ex, "decisions_2_signed_off.json"), out)
        self.assertEqual(rep["status"], "STOPPED_RECONCILIATION")
        self.assertNotIn("workbook", rep)

    def test_missing_rules_file(self):
        ex = self._copy_example()
        os.remove(os.path.join(ex, "rules", "unit_conversions.csv"))
        with self.assertRaises(RulesError):
            Rules(os.path.join(ex, "rules"))

    def test_unknown_unit_is_not_guessed(self):
        rep, _ = _run("decisions_2_signed_off.json")
        # Plant C row 6 uses unit ROLL with no pack size: it must not be converted
        c6 = [o for o in rep["opportunities"] if any("plant_C_report.xlsx / Packaging / row 6" in r for r in o["rows"])]
        self.assertEqual(c6, [])

    def test_approved_equivalence_brings_spec_y_in(self):
        # if a person approves X = Y, Plant C becomes comparable - the rule, not the code, decides
        ex = self._copy_example()
        with open(os.path.join(ex, "rules", "spec_equivalences.csv"), "a") as f:
            f.write("SPEC-X,SPEC-Y,approved,Category buyer (role),synthetic test only\n")
        out = tempfile.mkdtemp()
        rep = runner.run(os.path.join(ex, "scope.json"), os.path.join(ex, "rules"),
                         os.path.join(ex, "decisions_2_signed_off.json"), out)
        self.assertEqual(rep["status"], "FINAL")
        self.assertIn("Plant C", [o["plant"] for o in rep["opportunities"]])


class TestRules(unittest.TestCase):
    def setUp(self):
        self.r = Rules(RULES)

    def test_unit_conversions(self):
        self.assertEqual(self.r.unit_rule("ROLL-500", "Plant A")["labels_per_unit"], Decimal(500))
        self.assertEqual(self.r.unit_rule("ROLL-1000", "Plant B")["labels_per_unit"], Decimal(1000))
        self.assertEqual(self.r.unit_rule("RL (500)", "Plant C")["labels_per_unit"], Decimal(500))
        self.assertIsNone(self.r.unit_rule("RL (500)", "Plant A"))   # plant-specific rule stays plant-specific
        self.assertIsNone(self.r.unit_rule("ROLL", "Plant C"))       # no pack size, no rule

    def test_fx(self):
        self.assertEqual(self.r.fx_rule("EUR", "USD")["rate"], Decimal("1.08"))
        self.assertIsNone(self.r.fx_rule("GBP", "USD"))

    def test_spec_x_and_y_not_equivalent(self):
        self.assertNotEqual(self.r.canonical_spec("SPEC-X"), self.r.canonical_spec("SPEC-Y"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
