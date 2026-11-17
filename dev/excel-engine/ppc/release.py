"""Step 8: checks that must pass before a workbook is released. Any failure blocks it."""
from __future__ import annotations

from decimal import Decimal

from .model import Line


def release_checks(files, lines: list[Line], opps: list[dict], rules) -> list[dict]:
    by_ref = {ln.ref: ln for ln in lines}
    checks = []

    def add(cid, name, ok, detail):
        checks.append({"id": cid, "check": name, "result": "PASS" if ok else "FAIL", "detail": detail})

    # R1 totals reconcile for every file
    bad = [f"{f.file}: rows {f.recon['row_check']}, totals {f.recon['total_check']}"
           for f in files if f.recon.get("row_check") != "PASS" or f.recon.get("total_check") == "FAIL"]
    add("R1", "Every file reconciles to its source (rows and stated totals)", not bad,
        "; ".join(bad) or f"{len(files)} file(s) reconciled")

    # R2 every opportunity traces to source rows that exist and are Compared
    missing = [r for o in opps for r in o["rows"] + o["benchmark_rows"]
               if r not in by_ref or by_ref[r].outcome != "Compared"]
    add("R2", "Every finding traces to source file, sheet and row", not missing,
        "; ".join(missing[:5]) or f"{sum(len(o['rows']) + len(o['benchmark_rows']) for o in opps)} row references checked")

    # R3 no comparison without an approved match; one specification per group
    unapproved = [ln.ref for ln in lines if ln.outcome == "Compared" and not ln.match_ok]
    mixed = []
    for o in opps:
        specs = {by_ref[r].canon_spec for r in o["rows"] + o["benchmark_rows"] if r in by_ref}
        if len(specs) != 1:
            mixed.append(o["group"])
    add("R3", "No item compared without an approved specification match", not unapproved and not mixed,
        "; ".join(unapproved[:5] + mixed) or "all compared lines have approved matches")

    # R4 every conversion came from the rules pack
    noconv = [ln.ref for ln in lines if ln.outcome == "Compared" and (not ln.conv_rule or not ln.fx_rule)]
    add("R4", "Every unit and currency conversion came from the rules pack", not noconv,
        "; ".join(noconv[:5]) or "all conversions cite a rule")

    # R5 each group has one band and one freight class
    multi = []
    for o in opps:
        conds = {(by_ref[r].band, by_ref[r].freight_class) for r in o["rows"] + o["benchmark_rows"] if r in by_ref}
        if len(conds) != 1:
            multi.append(o["group"])
    add("R5", "Compared purchases share the same order-size band and freight class", not multi,
        "; ".join(multi) or "conditions consistent in every group")

    # R6 recompute every saving from the rows
    wrong = []
    for o in opps:
        lab = sum((by_ref[r].labels for r in o["rows"]), Decimal(0))
        if lab != o["labels"]:
            wrong.append(f"{o['plant']} labels")
        expect = (o["price_per_1000"] - o["benchmark_price_per_1000"]) * lab / rules.basis
        if abs(expect - o["scenario_saving"]) > Decimal("1.00"):
            wrong.append(f"{o['plant']} saving")
    add("R6", "Every saving recomputes from its supporting rows", not wrong,
        "; ".join(wrong) or f"{len(opps)} saving(s) recomputed")

    # R7 nothing silently dropped: every loaded line has an outcome
    unaccounted = [ln.ref for ln in lines if ln.outcome not in ("Compared", "Not compared", "Needs review", "Excluded")]
    add("R7", "Every loaded line is accounted for (compared, not compared, review or excluded)",
        not unaccounted, "; ".join(unaccounted[:5]) or f"{len(lines)} lines accounted for")
    return checks
