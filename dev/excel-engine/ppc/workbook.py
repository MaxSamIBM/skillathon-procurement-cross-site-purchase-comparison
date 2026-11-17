"""Step 9: the evidence workbook. Tabs: Summary, Opportunities, Supporting transactions,
Comparability checks, Needs review."""
from __future__ import annotations

from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HDR = PatternFill("solid", fgColor="1F3864")
WARN = PatternFill("solid", fgColor="FCE4D6")
OKF = PatternFill("solid", fgColor="E2EFDA")


def _v(x):
    if isinstance(x, Decimal):
        return float(x)
    return x


def _sheet(wb, title, headers, rows, widths=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), HDR
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in rows:
        ws.append([_v(x) for x in r])
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(h, max(12, min(45, len(h) + 4)))
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    return ws


def finding_text(o) -> str:
    return (f"{o['plant']} bought {o['spec_id']} at ${o['price_per_1000']:,.2f} per 1,000 labels. "
            f"{o['benchmark_plant']} bought the same specification at ${o['benchmark_price_per_1000']:,.2f} "
            f"under the same order size and freight terms during the comparison period. "
            f"Confirm whether {o['benchmark_plant']}'s pricing can apply to {o['plant']} at its volume, "
            f"location and terms before including the difference in a savings plan.")


def write(path, status, scope, files, lines, opps, groups, queue, checks, gate_log, version):
    wb = Workbook()
    wb.remove(wb.active)

    ws = _sheet(wb, "Summary", ["Item", "Value"], [], {"Item": 34, "Value": 90})
    rows = [
        ("Status", status),
        ("Skill", f"cross-site-purchase-comparison {version}"),
        ("Project", scope.get("project", "")),
        ("Category", scope.get("category", "")),
        ("Comparison window", f"{scope['window']['start']} to {scope['window']['end']}"),
        ("Reporting currency", scope["reporting_currency"]),
        ("Price basis", "per 1,000 labels"),
        ("Opportunities", len(opps)),
        ("Total scenario savings", sum((o["scenario_saving"] for o in opps), Decimal(0))),
        ("Lines needing review", sum(1 for ln in lines if ln.outcome == "Needs review")),
        ("Important", "Savings are scenarios for a buyer to confirm, not achieved or committed savings."),
    ]
    for g in gate_log:
        rows.append((g[0], g[1]))
    for c in checks:
        rows.append((f"Release check {c['id']}", f"{c['result']} - {c['check']}: {c['detail']}"))
    for r in rows:
        ws.append([_v(x) for x in r])
    ws["B2"].fill = OKF if status == "FINAL" else WARN

    _sheet(wb, "Opportunities",
           ["Rank", "Plant", "Specification", "Comparison group", "Plant price per 1,000", "Benchmark plant",
            "Benchmark price per 1,000", "Gap per 1,000", "Labels in window", "Scenario saving",
            "Finding (for the buyer)", "Assumptions"],
           [[o["rank"], o["plant"], o["spec_id"], o["group"], o["price_per_1000"], o["benchmark_plant"],
             o["benchmark_price_per_1000"], o["gap_per_1000"], o["labels"], o["scenario_saving"],
             finding_text(o),
             "Volume-weighted prices; benchmark = lowest plant price in the same group; saving = gap x this plant's "
             "in-window volume; assumes the benchmark price is available at this plant's volume, location and terms."]
            for o in opps],
           {"Finding (for the buyer)": 70, "Assumptions": 50, "Comparison group": 28})

    sup = []
    opp_rows = {r: f"#{o['rank']} {o['plant']}" for o in opps for r in o["rows"]}
    bench_rows = {r: f"benchmark for #{o['rank']}" for o in opps for r in o["benchmark_rows"]}
    for ln in lines:
        if ln.outcome != "Compared":
            continue
        role = opp_rows.get(ln.ref) or bench_rows.get(ln.ref) or "compared, no gap"
        sup.append([role, ln.plant, ln.file, ln.sheet, ln.row, ln.date.isoformat() if ln.date else "",
                    ln.item_code, ln.description, ln.supplier, ln.quantity, ln.purchase_unit, ln.unit_price,
                    ln.currency, ln.freight_terms, ln.labels, ln.price_per_1000, ln.group])
    _sheet(wb, "Supporting transactions",
           ["Used in", "Plant", "File", "Sheet", "Row", "Date", "Item code", "Description", "Supplier",
            "Quantity", "Unit", "Unit price", "Currency", "Freight", "Labels", "Price per 1,000 (reporting)", "Group"],
           sup, {"Description": 34, "Group": 28, "File": 26})

    comp = []
    for ln in lines:
        if ln.status == "excluded":
            continue
        comp.append([ln.plant, ln.ref, ln.description, ln.spec_id, ln.match_status, ln.match_basis,
                     ln.purchase_unit, ln.conv_rule, ln.labels_per_unit, ln.currency, ln.fx_rule,
                     ln.band, ln.freight_class, ln.group, ln.outcome])
    ws = _sheet(wb, "Comparability checks",
                ["Plant", "Source", "Description", "Specification", "Match status", "Match basis", "Unit",
                 "Conversion rule", "Labels per unit", "Currency", "FX rule", "Order-size band", "Freight class",
                 "Group", "Outcome"],
                comp, {"Source": 34, "Match basis": 40, "FX rule": 34, "Description": 30, "Group": 28})
    r0 = ws.max_row + 2
    ws.cell(r0, 1, "Groups").font = Font(bold=True)
    ws.append(["Group", "Plants", "Benchmark plant", "Benchmark price per 1,000", "Note"])
    for g in groups:
        ws.append([g["group"], g["plants"], g["benchmark_plant"], _v(g["benchmark_price"]), g["note"]])
    r1 = ws.max_row + 2
    ws.cell(r1, 1, "File reconciliation").font = Font(bold=True)
    ws.append(["File", "Sheet", "Header row", "Rows in source", "Rows loaded", "Rows skipped", "Loaded amount",
               "Stated total", "Total check", "Columns mapped", "Columns kept, not used"])
    for f in files:
        ws.append([f.file, f.sheet, f.header_row, f.recon.get("rows_in_source"), f.recon.get("rows_loaded"),
                   "; ".join(f.recon.get("skipped_detail", [])) or 0, _v(f.recon.get("loaded_amount")),
                   _v(f.recon.get("stated_total")), f.recon.get("total_check"),
                   "; ".join(f"{h} -> {fld} ({f.mapping_basis[h]})" for h, fld in f.mapping.items()),
                   ", ".join(f.unmapped_headers)])

    rev = []
    for q in queue:
        rev.append(["Gate 2 - match to approve", q["plant"], q["example_row"], q["description"],
                    f"Proposed {q['proposed_spec'] or '(none)'} with score {q['score']}",
                    "Approve or reject in decisions.json > match_decisions"])
    for ln in lines:
        in_gate2 = ln.match_status in ("proposed", "ambiguous")
        if ln.outcome == "Needs review" and in_gate2 and len(ln.issues) == 1:
            continue  # already listed above as a Gate 2 item, nothing else wrong with it
        if ln.outcome in ("Needs review", "Excluded"):
            rev.append([ln.outcome, ln.plant, ln.ref, ln.description, "; ".join(ln.issues),
                        "Fix the source or the rules pack, then re-run" if ln.outcome == "Needs review"
                        else "No action unless the window is wrong"])
        elif ln.outcome == "Not compared":
            rev.append(["Not compared", ln.plant, ln.ref, ln.description, "; ".join(ln.issues) or
                        f"Group '{ln.group}' has no other plant", "Informational"])
    _sheet(wb, "Needs review", ["Type", "Plant", "Source", "Description", "Reason", "What to do"], rev,
           {"Reason": 70, "Source": 34, "Description": 30, "What to do": 40})
    wb.save(path)
