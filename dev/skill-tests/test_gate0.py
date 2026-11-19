"""Gate 0 (--mode propose) tests: raw files -> needs/proposals -> person's answers -> FINAL."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "skills", "cross-site-purchase-comparison")
SCRIPT = os.path.join(ROOT, "scripts", "compare_prices.py")
EX = os.path.join(ROOT, "examples", "synthetic")
PLANTS = ("plant_A_purchases.csv", "plant_B_export.csv", "plant_C_report.csv", "plant_D_extract.csv")
GATE1 = {"gate1": {"confirmed": True, "by": "Procurement consultant (role)"}}
GATE3 = {"gate3": {"approved": True, "by": "Procurement lead (role)"}}
# Facts only a person can supply. The script must never invent these.
PACK_SIZES = {"ROLL-500": "500", "ROLL-1000": "1000", "RL (500)": "500", "M": "1000", "ROLL": "500"}


def run(cwd, mode):
    p = subprocess.run([sys.executable, SCRIPT, "--mode", mode], cwd=cwd, capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)


class Gate0(unittest.TestCase):
    def folder(self, extra=()):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for n in PLANTS:
            shutil.copy(os.path.join(EX, "inputs_csv", n), d)
        for src, dst in extra:
            shutil.copy(src, os.path.join(d, dst))
        return d

    def answers(self, g0):
        """Build rules.json + scope.json the way the agent would from a person's answers."""
        patch = g0["rules_patch"]
        props = {p["kind"]: p for p in g0["proposals"]}
        units = []
        for i, n in enumerate([n for n in g0["needs"] if n["kind"] == "unit_pack_size"], 1):
            row = dict(n["template_row"], rule_id=f"UC-{i:03d}", labels_per_unit=PACK_SIZES[n["unit"]], source="user_confirmed")
            if n["unit"] == "RL (500)":
                row["plant"] = "Plant C"
            units.append(row)
        fx = dict([n for n in g0["needs"] if n["kind"] == "fx_rate"][0]["template_row"],
                  rule_id="FX-001", rate_to_reporting="1.08", as_of="2026-10-31", source="user_confirmed")
        rules = {"columns": props["columns"]["value"], "settings": props["settings"]["value"],
                 "specs": copy.deepcopy(patch["specs"]), "item_map": copy.deepcopy(patch["item_map"]),
                 "spec_equivalences": [], "abbreviations": copy.deepcopy(patch["abbreviations"]),
                 "unit_conversions": units, "fx_rates": [fx]}
        scope = copy.deepcopy(props["scope"]["value"])
        scope.update(project="SYNTHETIC test", category="packaging-labels",
                     window={"start": "2025-11-01", "end": "2026-10-31"})
        return rules, scope

    # ---- propose on raw files ----
    def test_raw_files_only_names_the_gaps_and_writes_nothing(self):
        d = self.folder()
        before = sorted(os.listdir(d))
        rc, g0 = run(d, "propose")
        self.assertEqual(rc, 0)
        self.assertEqual(g0["status"], "GATE_0_NEEDS_INPUT")
        self.assertIs(g0["wrote_files"], False)
        self.assertEqual(sorted(os.listdir(d)), before, "propose must not write or change any file")
        blocking = {n["kind"] for n in g0["needs"] if n["blocking"]}
        self.assertTrue({"unit_pack_size", "fx_rate"} <= blocking, blocking)

    def test_pack_sizes_and_fx_are_asked_never_filled_in(self):
        _, g0 = run(self.folder(), "propose")
        for n in g0["needs"]:
            if n["kind"] in ("unit_pack_size", "fx_rate"):
                row = n["template_row"]
                self.assertIn("<ask>", json.dumps(row), "a pack size or rate must be left for the person")
        kinds = {p["kind"] for p in g0["proposals"]}
        self.assertNotIn("unit_pack_size", kinds)
        self.assertNotIn("fx_rate", kinds)
        self.assertEqual(g0["rules_patch"]["spec_equivalences"], [], "equivalences are never proposed")

    def test_safe_things_are_proposed_not_asked(self):
        _, g0 = run(self.folder(), "propose")
        kinds = {p["kind"] for p in g0["proposals"]}
        self.assertTrue({"scope", "columns", "candidate_product", "reporting_currency"} <= kinds, kinds)
        scope = [p for p in g0["proposals"] if p["kind"] == "scope"][0]["value"]
        self.assertEqual(len(scope["files"]), 4)

    def test_every_proposal_is_marked_unconfirmed(self):
        _, g0 = run(self.folder(), "propose")
        for p in g0["proposals"]:
            self.assertEqual(p["source"], "agent_proposed")

    def test_one_plant_file_is_a_blocking_need(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        shutil.copy(os.path.join(EX, "inputs_csv", PLANTS[0]), d)
        _, g0 = run(d, "propose")
        self.assertIn("plant_files", {n["kind"] for n in g0["needs"] if n["blocking"]})

    # ---- partial rules ----
    def test_partial_rules_only_ask_what_is_missing(self):
        full = json.load(open(os.path.join(EX, "rules.json")))
        partial = {"unit_conversions": full["unit_conversions"], "fx_rates": full["fx_rates"]}
        d = self.folder()
        json.dump(partial, open(os.path.join(d, "rules.json"), "w"))
        rc, g0 = run(d, "propose")
        self.assertEqual(rc, 0)
        kinds = {n["kind"] for n in g0["needs"]}
        asked = [n["unit"] for n in g0["needs"] if n["kind"] == "unit_pack_size"]
        # The synthetic data plants a bare 'ROLL' unit that has no rule. Asking about it is correct.
        self.assertEqual(asked, ["ROLL"], "only the genuinely unknown unit is asked")
        self.assertNotIn("fx_rate", kinds)

    def test_missing_pack_size_for_one_unit_is_named(self):
        full = json.load(open(os.path.join(EX, "rules.json")))
        units = [u for u in full["unit_conversions"] if u["unit_alias"] != "M"]
        d = self.folder()
        json.dump({"unit_conversions": units, "fx_rates": full["fx_rates"]}, open(os.path.join(d, "rules.json"), "w"))
        _, g0 = run(d, "propose")
        asked = [n["unit"] for n in g0["needs"] if n["kind"] == "unit_pack_size"]
        self.assertEqual(sorted(asked), ["M", "ROLL"])

    def test_malformed_rules_file_is_a_clear_error(self):
        d = self.folder()
        open(os.path.join(d, "rules.json"), "w").write("{not json")
        rc, g0 = run(d, "propose")
        self.assertEqual(rc, 0)
        problems = [n for n in g0["needs"] if n["kind"] == "rules_problem"]
        self.assertEqual(len(problems), 1, "a broken rules file must be reported, not silently ignored")
        self.assertTrue(problems[0]["blocking"])
        self.assertIn("ignored", problems[0]["ask"])

    # ---- full flow ----
    def test_full_flow_from_raw_files_to_final(self):
        _, g0 = run(self.folder(), "propose")
        rules, scope = self.answers(g0)

        d = self.folder()
        json.dump(rules, open(os.path.join(d, "rules.json"), "w"))
        json.dump(scope, open(os.path.join(d, "scope.json"), "w"))

        # (a) nothing decided -> stops at Gate 1
        _, r = run(d, "run")
        self.assertEqual(r["status"], "STOPPED_AT_GATE_1")

        # (b) Gate 1 confirmed but rules still agent_proposed -> DRAFT
        json.dump(GATE1, open(os.path.join(d, "decisions.json"), "w"))
        _, r = run(d, "run")
        self.assertEqual(r["status"], "DRAFT")
        self.assertGreater(r["rules_provenance"]["agent_proposed"], 0)

        # (c) Gate 3 signed while rules are unconfirmed -> must NOT reach FINAL
        json.dump({**GATE1, **GATE3}, open(os.path.join(d, "decisions.json"), "w"))
        _, r = run(d, "run")
        self.assertEqual(r["status"], "DRAFT", "unconfirmed rules must block FINAL")

        # (d) person confirms everything -> FINAL
        for row in rules["specs"] + rules["abbreviations"]:
            row["source"] = "user_confirmed"
        for row in rules["item_map"]:
            row.update(source="user_confirmed", status="approved", approved_by="Category buyer (role)")
        rules["columns"]["source"] = "user_confirmed"
        rules["settings"]["source"] = "user_confirmed"
        json.dump(rules, open(os.path.join(d, "rules.json"), "w"))
        _, r = run(d, "run")
        self.assertEqual(r["status"], "FINAL")
        self.assertEqual(r["rules_provenance"]["agent_proposed"], 0)
        self.assertTrue(all(c["result"] == "PASS" for c in r["release_checks"]))
        self.assertTrue(r["opportunities"], "FINAL run should produce at least one opportunity")

    def test_complete_rules_pack_still_works_unchanged(self):
        """A client who already has a full rules pack must see no change in behaviour."""
        d = self.folder([(os.path.join(EX, "scope_csv.json"), "scope.json"),
                         (os.path.join(EX, "rules.json"), "rules.json"),
                         (os.path.join(EX, "decisions_2_signed_off.json"), "decisions.json")])
        _, r = run(d, "run")
        self.assertEqual(r["status"], "FINAL")
        self.assertEqual({o["scenario_saving"] for o in r["opportunities"]}, {"20000.00", "2112.00"})


if __name__ == "__main__":
    unittest.main()
