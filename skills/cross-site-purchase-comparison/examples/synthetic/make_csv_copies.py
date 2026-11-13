"""Writes CSV copies of the four synthetic plant workbooks (first sheet of each) plus scope_csv.json.
Use these when a platform turns uploaded .xlsx files into text. Synthetic data only."""
import csv, datetime as dt, json, os
from openpyxl import load_workbook

HERE = os.path.dirname(os.path.abspath(__file__))
SRC, DST = os.path.join(HERE, "inputs"), os.path.join(HERE, "inputs_csv")
os.makedirs(DST, exist_ok=True)


def cell(v):
    if v is None:
        return ""
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    return v


for name in sorted(os.listdir(SRC)):
    if not name.endswith(".xlsx"):
        continue
    ws = load_workbook(os.path.join(SRC, name), data_only=True).worksheets[0]
    with open(os.path.join(DST, name[:-5] + ".csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for row in ws.iter_rows(values_only=True):
            w.writerow([cell(v) for v in row])

scope = json.load(open(os.path.join(HERE, "scope.json")))
for f in scope["files"]:
    f["path"] = f["path"].replace("inputs/", "inputs_csv/").replace(".xlsx", ".csv")
json.dump(scope, open(os.path.join(HERE, "scope_csv.json"), "w"), indent=2)
print("wrote", DST)
