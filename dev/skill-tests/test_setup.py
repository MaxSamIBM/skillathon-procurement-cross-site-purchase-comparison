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

    def approve(self, d):
        """Gate 0 as the agent does it: build the files, then copy the approval token into setup.json unchanged."""
        write(d, "setup.json", self.answered(d))
        r = run(d, "setup")
        self.assertEqual(r["status"], "SETUP_READY", r.get("questions"))
        tpl = json.load(open(os.path.join(d, "setup.json")))
        tpl["approval"] = r["approval_template"]
        write(d, "setup.json", tpl)
        return r

    def test_unapproved_setup_stops_at_gate_0(self):
        d = self.folder()
        write(d, "setup.json", self.answered(d))
        self.assertEqual(run(d, "setup")["status"], "SETUP_READY")
        write(d, "decisions.json", {"gate1": {"confirmed": True, "by": ROLE}})  # gate 1 alone is not enough
        r = run(d, "run")
        self.assertEqual(r["status"], "STOPPED_AT_GATE_0", r)
        self.assertEqual(r["opportunities"] if "opportunities" in r else [], [])

    def test_end_to_end_to_final_without_uploading_rules_or_scope(self):
        d = self.folder()
        ready = self.approve(d)
        self.assertIn("rules_sha256", ready["approval_template"])
        draft = run(d, "run")  # no decisions.json at all: Gate 0 approval is the Gate 1 confirmation
        self.assertEqual(draft["status"], "DRAFT", draft)
        gates = {g["gate"]: g for g in draft["gates"]}
        self.assertEqual(gates["Gate 0"]["status"], "approved")
        self.assertEqual(gates["Gate 1"]["via"], "Gate 0 approval")
        self.assertEqual(draft["opportunities"][0]["scenario_saving"], "20000.00")
        self.assertEqual(draft["inputs"]["gate0"]["approved_by"], ROLE)
        self.assertTrue(os.path.isfile(os.path.join(d, "out", "rules.json")))
        write(d, "decisions.json", {
            "match_decisions": [{"plant": "Plant D", "description": "Lbl wht spec X", "decision": "approve", "by": ROLE}],
            "gate3": {"approved": True, "by": "Procurement lead"}})
        final = run(d, "run")
        self.assertEqual(final["status"], "FINAL", final)
        self.assertEqual([o["scenario_saving"] for o in final["opportunities"]], ["20000.00", "2112.00"])

    def test_changed_answer_after_approval_reopens_gate_0(self):
        d = self.folder()
        self.approve(d)
        tpl = json.load(open(os.path.join(d, "setup.json")))
        tpl["unit_pack_sizes"]["M"] = 500  # someone edits an answer after approving
        write(d, "setup.json", tpl)
        r = run(d, "run")
        self.assertEqual(r["status"], "STOPPED_AT_GATE_0")
        self.assertTrue(any("changed after they were approved" in g for g in r["gate0_open"]), r["gate0_open"])

    def test_edited_plant_file_after_approval_reopens_gate_0(self):
        d = self.folder()
        self.approve(d)
        path = os.path.join(d, "plant_A_purchases.csv")
        with open(path, "a") as f:
            f.write("\n")
        r = run(d, "run")
        self.assertEqual(r["status"], "STOPPED_AT_GATE_0")
        self.assertTrue(any("plant_A_purchases.csv" in g and "differs" in g for g in r["gate0_open"]), r["gate0_open"])

    def test_missing_plant_file_after_approval_is_named(self):
        d = self.folder()
        self.approve(d)
        os.remove(os.path.join(d, "plant_B_export.csv"))
        r = run(d, "run")
        self.assertEqual(r["status"], "STOPPED_AT_GATE_0", r)
        self.assertTrue(any("plant_B_export.csv" in g and "not attached" in g for g in r["gate0_open"]), r["gate0_open"])

    def test_forged_approval_hash_is_refused(self):
        d = self.folder()
        self.approve(d)
        tpl = json.load(open(os.path.join(d, "setup.json")))
        tpl["approval"]["rules_sha256"] = "0" * 64
        write(d, "setup.json", tpl)
        self.assertEqual(run(d, "run")["status"], "STOPPED_AT_GATE_0")

    def test_approval_without_a_name_is_refused(self):
        d = self.folder()
        self.approve(d)
        tpl = json.load(open(os.path.join(d, "setup.json")))
        tpl["approval"]["by"] = ""
        write(d, "setup.json", tpl)
        self.assertEqual(run(d, "run")["status"], "STOPPED_AT_GATE_0")

    def test_case_card_names_every_plant_and_every_answer(self):
        d = self.folder()
        self.approve(d)
        r = run(d, "run")
        txt = r["case_card_text"]
        for needle in ("plant_A_purchases.csv", "plant_B_export.csv", "plant_C_report.csv", "plant_D_extract.csv",
                       "approved by", "unit 'M': 1000 labels", "exchange rate EUR to USD: 1.08"):
            self.assertIn(needle, txt)
        self.assertEqual(len(r["case_card"]["plant_files"]), 4)
        self.assertEqual(r["case_card"]["setup_json_to_restore"]["unit_pack_sizes"]["M"], 1000)

    def test_setup_mode_prints_a_case_card_with_the_plant_files(self):
        d = self.folder()
        r = run(d, "setup")
        self.assertIn("plant_D_extract.csv", r["case_card_text"])
        self.assertIn("Open questions: ", r["case_card_text"])

    def test_wiped_folder_is_rebuilt_from_the_case_card_and_stays_approved(self):
        d = self.folder()
        self.approve(d)
        card = run(d, "run")["case_card"]
        restore_setup, restore_decisions = card["setup_json_to_restore"], card["decisions_json_to_restore"]
        self.assertIn("approval", restore_setup)
        fresh = self.folder()  # new, empty working folder: only the re-attached plant files and what the chat remembered
        write(fresh, "setup.json", restore_setup)
        write(fresh, "decisions.json", restore_decisions)
        again = run(fresh, "run")
        self.assertEqual(again["status"], "DRAFT", again)
        self.assertEqual(again["opportunities"][0]["scenario_saving"], "20000.00")

    def test_platform_style_uploaded_names_work_through_gate_0(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for n in PLANTS:
            shutil.copy(os.path.join(EX, "inputs_csv", n), os.path.join(d, "uploaded_" + n + ".txt"))
        self.approve(d)
        r = run(d, "run")
        self.assertEqual(r["status"], "DRAFT", r)
        self.assertIn("plant_A_purchases.csv", r["case_card_text"])

    def test_supplied_rules_and_scope_still_work_without_setup(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for n in PLANTS:
            shutil.copy(os.path.join(EX, "inputs_csv", n), d)
        shutil.copy(os.path.join(EX, "rules.json"), d)
        shutil.copy(os.path.join(EX, "scope_csv.json"), d)
        shutil.copy(os.path.join(EX, "decisions_1_scope_confirmed.json"), d)
        r = run(d, "run")
        self.assertEqual(r["status"], "DRAFT", r)
        self.assertEqual(r["case_card"]["gate0"], "not used (rules and scope were supplied)")

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
