"""Combine examples/synthetic/rules/* into one rules.json (maintainer tool; not shipped in the skill)."""
import csv, json, os
HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.join(HERE, os.pardir, "skills", "cross-site-purchase-comparison", "examples", "synthetic")
RULES = os.path.join(EX, "rules")

def rows(name):
    with open(os.path.join(RULES, name + ".csv"), newline="", encoding="utf-8-sig") as f:
        return [dict(r) for r in csv.DictReader(f)]

def load(name):
    with open(os.path.join(RULES, name + ".json"), encoding="utf-8") as f:
        return json.load(f)

pack = {"_doc": "Single-file rules pack: the eight rules/ files combined. Attach this instead of the rules/ folder.",
        "columns": load("columns"), "settings": load("settings")}
for t in ("specs", "item_map", "spec_equivalences", "abbreviations", "unit_conversions", "fx_rates"):
    pack[t] = rows(t)
with open(os.path.join(EX, "rules.json"), "w", encoding="utf-8") as f:
    json.dump(pack, f, indent=2, ensure_ascii=False)
    f.write("\n")
print("wrote", os.path.join(EX, "rules.json"))
