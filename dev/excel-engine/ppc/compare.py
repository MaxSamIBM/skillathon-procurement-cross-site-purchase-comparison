"""Steps 5-7:
5 normalize to price per 1,000 labels in the reporting currency, using only the rules pack;
6 split by buying conditions (window, order-size band, freight class);
7 calculate the gap to the lowest comparable plant price and rank the scenarios."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from .model import Line
from .rules import Rules

C2 = Decimal("0.01")


def q2(x: Decimal) -> Decimal:
    return x.quantize(C2, rounding=ROUND_HALF_UP)


def apply_window(lines: list[Line], start: dt.date, end: dt.date) -> None:
    for ln in lines:
        if ln.date and not (start <= ln.date <= end):
            ln.status = "excluded"
            ln.issues.append(f"Order date {ln.date} is outside the comparison window ({start} to {end})")


def normalize(lines: list[Line], rules: Rules, reporting: str) -> None:
    for ln in lines:
        if ln.status == "excluded":
            continue
        rule = rules.unit_rule(ln.purchase_unit, ln.plant) if ln.purchase_unit else None
        if ln.purchase_unit and not rule:
            ln.flag(f"Unit '{ln.purchase_unit}' has no pack size in unit_conversions.csv - not converted (no guessing)")
        if rule:
            ln.labels_per_unit, ln.conv_rule = rule["labels_per_unit"], rule["rule_id"]
        if ln.currency:
            if ln.currency == reporting.upper():
                ln.fx_rate, ln.fx_rule = Decimal(1), "same currency"
            else:
                fx = rules.fx_rule(ln.currency, reporting)
                if fx:
                    ln.fx_rate, ln.fx_rule = fx["rate"], f"{fx['rule_id']} ({ln.currency}->{reporting} {fx['rate']}, {fx['as_of']})"
                else:
                    ln.flag(f"No exchange rate for {ln.currency}->{reporting} in fx_rates.csv")
        if ln.labels_per_unit and ln.fx_rate is not None and ln.unit_price is not None:
            ln.price_per_1000 = ln.unit_price * ln.fx_rate / ln.labels_per_unit * rules.basis
            if ln.quantity is not None:
                ln.labels = ln.quantity * ln.labels_per_unit
                ln.amount_reporting = ln.quantity * ln.unit_price * ln.fx_rate


def assign_conditions(lines: list[Line], rules: Rules) -> None:
    for ln in lines:
        if ln.status == "excluded":
            continue
        if ln.labels is not None:
            ln.band = rules.band_for(ln.labels)
        term = ln.freight_terms.strip().upper()
        if term:
            ln.freight_class = rules.freight_groups.get(term, "")
            if not ln.freight_class:
                ln.flag(f"Freight term '{term}' is not in settings.freight_groups")
        else:
            ln.freight_class = "unknown"
            ln.flag("Missing freight terms - can't tell if prices are delivered or ex-works")


def build_groups(lines: list[Line], rules: Rules):
    """Only clean, approved, fully converted lines enter a group."""
    groups = defaultdict(list)
    for ln in lines:
        if ln.status == "excluded":
            ln.outcome = "Excluded"
            continue
        if ln.status == "review" or not ln.match_ok or ln.price_per_1000 is None or ln.labels is None:
            ln.outcome = "Needs review"
            continue
        ln.group = f"{ln.canon_spec} | {ln.band} | {ln.freight_class}"
        groups[ln.group].append(ln)
    return groups


def rank(groups, rules: Rules):
    """Per group: volume-weighted price per plant; benchmark = lowest plant price; gap x labels."""
    opps, group_rows = [], []
    for g, lns in sorted(groups.items()):
        by_plant = defaultdict(list)
        for ln in lns:
            by_plant[ln.plant].append(ln)
        stats = {}
        for p, pl in by_plant.items():
            labels = sum((x.labels for x in pl), Decimal(0))
            amount = sum((x.amount_reporting for x in pl), Decimal(0))
            stats[p] = {"labels": labels, "amount": amount,
                        "price": amount / labels * rules.basis, "lines": pl}
        if len(stats) < rules.min_plants:
            for ln in lns:
                ln.outcome = "Not compared"
                ln.issues.append(f"Only one plant in group '{g}' - nothing to compare with")
            group_rows.append({"group": g, "plants": ", ".join(sorted(stats)), "benchmark_plant": "",
                               "benchmark_price": None, "note": "single plant - not compared"})
            continue
        bench_p = min(stats, key=lambda p: (stats[p]["price"], p))
        bench = stats[bench_p]["price"]
        group_rows.append({"group": g, "plants": ", ".join(sorted(stats)), "benchmark_plant": bench_p,
                           "benchmark_price": q2(bench), "note": ""})
        for p, st in stats.items():
            for ln in st["lines"]:
                ln.outcome = "Compared"
            if p == bench_p:
                continue
            gap = st["price"] - bench
            if gap <= 0:
                continue
            saving = gap * st["labels"] / rules.basis
            opps.append({
                "plant": p, "group": g, "spec_id": g.split(" | ")[0],
                "price_per_1000": q2(st["price"]), "benchmark_plant": bench_p,
                "benchmark_price_per_1000": q2(bench), "gap_per_1000": q2(gap),
                "labels": st["labels"], "scenario_saving": q2(saving),
                "rows": [x.ref for x in st["lines"]],
                "benchmark_rows": [x.ref for x in stats[bench_p]["lines"]],
            })
    opps.sort(key=lambda o: (-o["scenario_saving"], o["plant"]))
    for i, o in enumerate(opps, 1):
        o["rank"] = i
    return opps, group_rows
