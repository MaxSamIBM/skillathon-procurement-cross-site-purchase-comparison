"""Gate 0 guided setup (--mode setup): raw plant files -> answers in setup.json -> built rules.json/scope.json -> FINAL."""
from __future__ import annotations

import json
import os
import re
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
ROLE = "Procurement consultant"


def run(cwd, mode):
    p = subprocess.run([sys.executable, SCRIPT, "--mode", mode], cwd=cwd, capture_output=True, text=True)
    return json.loads(p.stdout)


def write(d, name, obj):
    with open(os.path.join(d, name), "w") as fh:
        json.dump(obj, fh)


class Setup(unittest.TestCase):
    def folder(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for n in PLANTS:
            shutil.copy(os.path.join(EX, "inputs_csv", n), d)
        return d

    def answered(self, d, pack_sizes=None):
        tpl = run(d, "setup")["setup_template"]
        tpl.update(confirmed_by=ROLE, category="packaging-labels", reporting_currency="USD",
                   window={"start": "2025-11-01", "end": "2026-10-31"},
                   unit_pack_sizes=pack_sizes or {"M": 1000, "RL (500)": 500, "ROLL": None, "ROLL-1000": 1000, "ROLL-500": 500},
                   fx_rates={"EUR": {"rate": "1.08", "as_of": "2026-01-01", "note": "synthetic"}},
                   products={"approve": ["SPEC-1", "SPEC-2"], "leave_out": ["SPEC-3"],
                             "hold_for_gate2": [{"plant": "Plant D", "description": "Lbl wht spec X"}]})
        tpl["confirm"] = {k: True for k in tpl["confirm"]}
        return tpl

    def test_first_turn_asks_everything_at_once_including_M(self):
        d = self.folder()
        r = run(d, "setup")
        self.assertEqual(r["status"], "SETUP_NEEDS_INPUT")
        asks = " ".join(q["ask"] for q in r["questions"])
        for unit in ("'M'", "ROLL-500", "ROLL-1000", "RL (500)", "'ROLL'"):
            self.assertIn(unit, asks)
        self.assertIn("EUR", asks)
        self.assertEqual(r["wrote_files"], [])
        self.assertFalse(os.path.exists(os.path.join(d, "out")))

    def test_answers_are_recorded_and_not_asked_again(self):
        d = self.folder()
        tpl = run(d, "setup")["setup_template"]
        tpl["unit_pack_sizes"] = {"M": 1000}
        write(d, "setup.json", tpl)
        r = run(d, "setup")
        asks = " ".join(q["ask"] for q in r["questions"])
        self.assertNotIn("'M'", asks)
        self.assertTrue(r["recorded"])
        self.assertIn("ROLL-500", asks)

    def test_ready_writes_both_files_tagged_user_confirmed(self):
        d = self.folder()
        write(d, "setup.json", self.answered(d))
        r = run(d, "setup")
        self.assertEqual(r["status"], "SETUP_READY", r.get("questions"))
        self.assertEqual(sorted(r["wrote_files"]), ["out/rules.json", "out/scope.json"])
        rules = json.load(open(os.path.join(d, "out", "rules.json")))
        for tbl in ("unit_conversions", "fx_rates"):
            self.assertTrue(rules[tbl])
            self.assertTrue(all(x["source"] == "user_confirmed" for x in rules[tbl]), tbl)
        by_plant = {x["plant"]: x for x in rules["item_map"]}
        self.assertEqual(by_plant["Plant A"]["source"], "user_confirmed")
        self.assertEqual(by_plant["Plant D"]["source"], "agent_proposed")  # held for Gate 2
        units = {x["unit_alias"]: x["labels_per_unit"] for x in rules["unit_conversions"]}
        self.assertEqual(units["M"], "1000")
        self.assertNotIn("ROLL", units)  # "I don't know" is never filled in

    def test_pack_size_is_never_taken_from_the_unit_name(self):
        d = self.folder()
        tpl = self.answered(d, pack_sizes={"M": 1000})
        write(d, "setup.json", tpl)
        r = run(d, "setup")
        self.assertEqual(r["status"], "SETUP_NEEDS_INPUT")
        self.assertIn("ROLL-500", " ".join(q["ask"] for q in r["questions"]))

    def test_end_to_end_to_final_without_uploading_rules_or_scope(self):
        d = self.folder()
        write(d, "setup.json", self.answered(d))
        self.assertEqual(run(d, "setup")["status"], "SETUP_READY")
        write(d, "decisions.json", {"gate1": {"confirmed": True, "by": ROLE}})
        draft = run(d, "run")
        self.assertEqual(draft["status"], "DRAFT", draft)
        self.assertEqual(draft["opportunities"][0]["scenario_saving"], "20000.00")
        # the unit/FX rules the person answered are confirmed facts, so they do not hold the run open
        self.assertFalse([g for g in draft.get("gate0_open", []) if "unit" in g.lower() or "fx" in g.lower()])
        write(d, "decisions.json", {
            "gate1": {"confirmed": True, "by": ROLE},
            "match_decisions": [{"plant": "Plant D", "description": "Lbl wht spec X", "decision": "approve", "by": ROLE}],
            "gate3": {"approved": True, "by": "Procurement lead"}})
        final = run(d, "run")
        self.assertEqual(final["status"], "FINAL", final)
        self.assertEqual([o["scenario_saving"] for o in final["opportunities"]], ["20000.00", "2112.00"])

    def test_window_is_the_one_the_person_gave(self):
        d = self.folder()
        tpl = self.answered(d)
        tpl["window"] = {"start": "2025-09-15", "end": "2026-09-09"}
        write(d, "setup.json", tpl)
        run(d, "setup")
        scope = json.load(open(os.path.join(d, "out", "scope.json")))
        self.assertEqual(scope["window"], {"start": "2025-09-15", "end": "2026-09-09"})

    def test_skill_folder_has_no_flagged_path_patterns(self):
        src = open(SCRIPT).read()
        for bad in ("realpath", "abspath", "__file__", "sys.argv", "os.environ", "../"):
            self.assertNotIn(bad, src)
        self.assertTrue(re.search(r'"setup"', open(os.path.join(ROOT, "scripts", "manifest.json")).read()))


if __name__ == "__main__":
    unittest.main()
