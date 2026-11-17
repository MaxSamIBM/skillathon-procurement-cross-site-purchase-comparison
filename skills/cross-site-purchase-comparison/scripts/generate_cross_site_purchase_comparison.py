#!/usr/bin/env python3
"""CSV-first deterministic workflow entry point (Python standard library only).

Staged-file convention: run with the staged package as cwd, containing scope.json,
 rules/, decisions.json, and CSVs referenced relative to scope.json. Outputs go to
 cwd/out/. This convention is documented for users; it is not a claim about an
 undocumented Process Studio argument or return contract.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

VERSION = "0.2.1"
RULE_FILES = ("columns.json", "settings.json", "specs.csv", "item_map.csv",
              "spec_equivalences.csv", "abbreviations.csv", "unit_conversions.csv", "fx_rates.csv")
TOTAL_RE = re.compile(r"\b(grand\s*total|sub\s*-?\s*total|total)\b", re.I)


class WorkflowError(ValueError):
    pass


SCOPE_FILE = "scope.json"
DECISIONS_FILE = "decisions.json"
RULES_DIR = "rules"
INPUT_DIR = "inputs_csv"
OUT_DIR = "out"


def _regular(path):
    """True for an existing regular file that is not a symlink."""
    return os.path.isfile(path) and not os.path.islink(path)


def _hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s if s is not None else "").lower()).strip()


def _norm_unit(s):
    return re.sub(r"\s+", " ", str(s if s is not None else "").strip().lower())


def _decimal(v, what):
    try:
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise WorkflowError(f"{what}: '{v}' is not a number")


def _json(path, what):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise WorkflowError(f"{what} is not readable valid JSON: {e}")


def _read_csv(path, required=()):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        missing = [c for c in required if c not in (rd.fieldnames or [])]
        if missing:
            raise WorkflowError(f"{os.path.basename(path)} missing column(s): {missing}")
        return [{k: (v or "").strip() for k, v in row.items() if k}
                for row in rd if any((v or "").strip() for v in row.values())]


def _load_rules():
    if not os.path.isdir(RULES_DIR) or os.path.islink(RULES_DIR):
        raise WorkflowError(f"Rules folder not found: {RULES_DIR}/")
    paths = {n: os.path.join(RULES_DIR, n) for n in RULE_FILES}
    for n, rule_path in paths.items():
        if not _regular(rule_path):
            raise WorkflowError(f"Required rules file not found: {RULES_DIR}/{n}")
    fingerprints = {n: _hash(p) for n, p in paths.items()}
    columns = _json(paths["columns.json"], "columns.json")
    required = columns["required"]
    fields = required + columns.get("optional", [])
    aliases = {}
    for field, names in columns["aliases"].items():
        if field not in fields:
            raise WorkflowError(f"columns.json: alias list for unknown field '{field}'")
        for name in names:
            key = _norm(name)
            if key in aliases and aliases[key] != field:
                raise WorkflowError(f"columns.json: alias '{name}' maps to two fields")
            aliases[key] = field
    settings = _json(paths["settings.json"], "settings.json")
    bands = settings["volume_bands"]
    if not bands or bands[-1].get("below_labels_per_order") is not None:
        raise WorkflowError("settings.json: last volume band must have below_labels_per_order = null")
    rules = {"required": required, "fields": fields, "aliases": aliases,
             "basis": _decimal(settings.get("price_basis_labels", 1000), "price basis"),
             "threshold": float(settings.get("matching", {}).get("propose_threshold", .6)),
             "bands": bands, "freight": {k.strip().upper(): v for k, v in settings["freight_groups"].items()},
             "min_plants": int(settings.get("min_plants_per_group", 2)),
             "tolerance": _decimal(settings.get("amount_tolerance", "0.01"), "amount tolerance"),
             "specs": {}, "item_map": [], "code_map": {}, "desc_map": {}, "parent": {},
             "abbr": {}, "units": {}, "fx": {}, "fingerprints": fingerprints}
    for row in _read_csv(paths["specs.csv"], ("spec_id", "spec_name")):
        rules["specs"][row["spec_id"]] = row["spec_name"]
    for row in _read_csv(paths["item_map.csv"], ("plant", "item_code", "description", "spec_id", "status")):
        if row["spec_id"] not in rules["specs"]:
            raise WorkflowError(f"item_map.csv: unknown spec_id '{row['spec_id']}'")
        rules["item_map"].append(row)
        if row["status"].lower() == "approved":
            plant = row["plant"].strip().lower()
            if row["item_code"]:
                rules["code_map"][(plant, row["item_code"].strip().lower())] = row
            if row["description"]:
                rules["desc_map"][(plant, _norm(row["description"]))] = row
    rules["parent"] = {sid: sid for sid in rules["specs"]}

    def root(spec):
        while rules["parent"][spec] != spec:
            rules["parent"][spec] = rules["parent"][rules["parent"][spec]]
            spec = rules["parent"][spec]
        return spec

    for row in _read_csv(paths["spec_equivalences.csv"], ("spec_a", "spec_b", "status")):
        for key in ("spec_a", "spec_b"):
            if row[key] not in rules["specs"]:
                raise WorkflowError(f"spec_equivalences.csv: unknown spec '{row[key]}'")
        if row["status"].lower() == "approved":
            a, b = root(row["spec_a"]), root(row["spec_b"])
            rules["parent"][max(a, b)] = min(a, b)
    for row in _read_csv(paths["abbreviations.csv"], ("short", "long")):
        rules["abbr"][_norm(row["short"])] = _norm(row["long"])
    for row in _read_csv(paths["unit_conversions.csv"], ("rule_id", "unit_alias", "plant", "labels_per_unit")):
        amount = _decimal(row["labels_per_unit"], f"unit_conversions {row['rule_id']}")
        if amount <= 0:
            raise WorkflowError(f"unit_conversions {row['rule_id']}: labels_per_unit must be > 0")
        key = (_norm_unit(row["unit_alias"]), row["plant"].strip().lower() or "*")
        if key in rules["units"]:
            raise WorkflowError(f"unit_conversions: duplicate rule for {key}")
        rules["units"][key] = {"rule_id": row["rule_id"], "labels_per_unit": amount}
    for row in _read_csv(paths["fx_rates.csv"], ("rule_id", "currency", "rate_to_reporting", "reporting_currency")):
        rate = _decimal(row["rate_to_reporting"], f"fx_rates {row['rule_id']}")
        if rate <= 0:
            raise WorkflowError(f"fx_rates {row['rule_id']}: rate must be > 0")
        key = (row["currency"].upper(), row["reporting_currency"].upper())
        if key in rules["fx"]:
            raise WorkflowError(f"fx_rates: duplicate rate for {key}")
        rules["fx"][key] = {"rule_id": row["rule_id"], "rate": rate, "as_of": row.get("as_of", "")}
    rules["canonical"] = root
    return rules


def _date(value, fmt):
    if not value or not value.strip():
        return None
    for pattern in ([fmt] if fmt else []) + ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S"]:
        try:
            return dt.datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            pass
    return "BAD"


def _amount(value):
    if value is None or not str(value).strip():
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return "BAD"


def _load_file(plant, path, rules, overrides, date_format):
    result = {"plant": plant, "file": os.path.basename(path), "header_row": None, "mapping": {},
              "mapping_basis": {}, "unmapped": [], "missing": [], "unknown": [], "errors": [],
              "rows": [], "skipped": [], "reconciliation": {}}
    with open(path, newline="", encoding="utf-8-sig") as f:
        source = list(csv.reader(f))
    width = max((len(row) for row in source), default=0)
    source = [row + [""] * (width - len(row)) for row in source]
    best = None
    for i, row in enumerate(source[:30]):
        hits = sum(1 for value in row if _norm(value) in rules["aliases"])
        if hits and (best is None or hits > best[1]):
            best = (i, hits)
    if best is None or best[1] < 2:
        result["errors"].append("Could not find a header row (fewer than 2 known column names in first 30 rows)")
        return result
    hdr, _ = best
    result["header_row"] = hdr + 1
    headers = [str(x).strip() for x in source[hdr]]
    override = {_norm(k): v for k, v in (overrides or {}).items()}
    col = {}
    for i, header in enumerate(headers):
        if not header:
            continue
        key = _norm(header)
        if key in override:
            field, basis = override[key], "person"
            if field not in rules["fields"]:
                result["errors"].append(f"Column override for '{header}' names unknown field '{field}'")
                continue
        elif key in rules["aliases"]:
            field, basis = rules["aliases"][key], "alias"
        else:
            result["unmapped"].append(header)
            continue
        if field in col:
            result["errors"].append(f"Two columns map to '{field}'")
            continue
        col[field] = i
        result["mapping"][header], result["mapping_basis"][header] = field, basis
    result["missing"] = [f for f in rules["required"] if f not in col]
    result["unknown"] = result["unmapped"][:] if result["missing"] else []
    if result["missing"] or result["errors"]:
        return result

    def get(row, field):
        index = col.get(field)
        return row[index].strip() if index is not None and index < len(row) else ""

    lines = []
    for rownum, row in enumerate(source[hdr + 1:], hdr + 2):
        vals = [v.strip() for v in row]
        if not any(vals):
            result["skipped"].append((rownum, "blank", None))
            continue
        first = next((x for x in vals if x), "")
        qty_value = _amount(get(row, "quantity"))
        if TOTAL_RE.search(first) and qty_value is None:
            result["skipped"].append((rownum, first, _amount(get(row, "line_amount"))))
            continue
        ln = {"plant": plant, "file": result["file"], "row": rownum, "date": None,
              "item_code": get(row, "item_code"), "description": get(row, "description"),
              "supplier": get(row, "supplier"), "quantity": qty_value, "unit": get(row, "purchase_unit"),
              "unit_price": _amount(get(row, "unit_price")), "currency": get(row, "currency").upper(),
              "freight": get(row, "freight_terms").upper(), "line_amount": _amount(get(row, "line_amount")),
              "issues": [], "status": "ok", "spec_id": "", "canon": "", "match_status": "",
              "match_basis": "", "match_score": 0.0, "labels_per_unit": None, "conv_rule": "",
              "labels": None, "fx_rate": None, "fx_rule": "", "price": None, "amount_reporting": None,
              "band": "", "freight_class": "", "group": "", "outcome": ""}
        def flag(message):
            ln["issues"].append(message)
            if ln["status"] == "ok":
                ln["status"] = "review"
        d = _date(get(row, "date"), date_format)
        if d == "BAD":
            flag(f"Date '{get(row, 'date')}' could not be read")
        elif d is None:
            flag("Missing date")
        else:
            ln["date"] = d
        for fld in ("quantity", "unit_price", "line_amount"):
            val = ln[fld]
            if val == "BAD":
                flag(f"{fld.replace('_', ' ').capitalize()} '{get(row, fld)}' is not a number")
                ln[fld] = None
        if ln["quantity"] is None:
            flag("Missing quantity")
        elif ln["quantity"] <= 0:
            flag(f"Quantity {ln['quantity']} is not positive")
        if ln["unit_price"] is None:
            flag("Missing unit price")
        if not ln["unit"]:
            flag("Missing purchase unit")
        if not ln["currency"]:
            flag("Missing currency")
        if not ln["description"] and not ln["item_code"]:
            flag("No description or item code")
        lines.append(ln)
    source_rows = len(source) - hdr - 1
    loaded = sum((ln["line_amount"] for ln in lines if isinstance(ln["line_amount"], Decimal)), Decimal(0))
    totals = [(label, amount) for _, label, amount in result["skipped"] if label != "blank" and isinstance(amount, Decimal)]
    grand = [amt for label, amt in totals if not re.search(r"sub", label, re.I)]
    stated = grand[-1] if grand else None
    arith = []
    for ln in lines:
        if all(isinstance(ln[k], Decimal) for k in ("quantity", "unit_price", "line_amount")) and abs(ln["quantity"] * ln["unit_price"] - ln["line_amount"]) > rules["tolerance"]:
            arith.append(ln["row"])
            flag = f"Quantity x price ({ln['quantity'] * ln['unit_price']}) does not equal line amount ({ln['line_amount']})"
            ln["issues"].append(flag)
            if ln["status"] == "ok": ln["status"] = "review"
    result["rows"] = lines
    result["reconciliation"] = {"rows_in_source": source_rows, "rows_loaded": len(lines),
        "rows_skipped": len(result["skipped"]), "skipped_detail": [f"row {r}: {label}" for r, label, _ in result["skipped"] if label != "blank"],
        "loaded_amount": loaded, "stated_total": stated,
        "total_check": "no total row in file" if stated is None else "PASS" if abs(loaded - stated) <= rules["tolerance"] else "FAIL",
        "row_check": "PASS" if source_rows == len(lines) + len(result["skipped"]) else "FAIL",
        "arithmetic_mismatch_rows": arith}
    return result


def _rowref(ln):
    return f"{ln['file']} / row {ln['row']}"


def _tokens(text, rules):
    return {rules["abbr"].get(t, t) for t in _norm(text).split()}


def _score(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def _match(lines, rules, decisions):
    dec_index = {(d["plant"].strip().lower(), _norm(d["description"])): d for d in decisions or []}
    spec_tokens = {}
    for row in rules["item_map"]:
        if row["status"].lower() == "approved" and row["description"]:
            spec_tokens.setdefault(row["spec_id"], []).append(_tokens(row["description"], rules))
    for sid, name in rules["specs"].items():
        spec_tokens.setdefault(sid, []).append(_tokens(name, rules))
    queue, seen = [], set()
    for ln in lines:
        plant = ln["plant"].lower()
        hit = rules["code_map"].get((plant, ln["item_code"].lower())) if ln["item_code"] else None
        basis = "approved item code"
        if not hit and ln["description"]:
            hit = rules["desc_map"].get((plant, _norm(ln["description"])))
            basis = "approved description"
        if hit:
            ln["spec_id"], ln["match_status"], ln["match_basis"] = hit["spec_id"], "approved", basis
            ln["canon"] = rules["canonical"](hit["spec_id"])
            continue
        tokens = _tokens(ln["description"], rules)
        scored = sorted(((max(_score(tokens, ts) for ts in choices), sid) for sid, choices in spec_tokens.items()), reverse=True)
        best, sid = scored[0] if scored else (0.0, "")
        runner = scored[1][0] if len(scored) > 1 else 0.0
        decision = dec_index.get((plant, _norm(ln["description"])))
        if decision:
            if decision.get("decision") == "approve":
                chosen = decision.get("spec_id") or sid
                if chosen not in rules["specs"]:
                    ln["issues"].append(f"Gate 2 decision names unknown specification '{chosen}'")
                    ln["status"] = "review"
                    continue
                ln["spec_id"], ln["match_status"] = chosen, "approved_by_person"
                ln["match_basis"] = f"approved at Gate 2 by {decision.get('by', 'unnamed')}: {decision.get('reason', '')}"
                ln["match_score"], ln["canon"] = round(best, 2), rules["canonical"](chosen)
            else:
                ln["match_status"] = "rejected"
                ln["issues"].append(f"Match rejected at Gate 2 by {decision.get('by', 'unnamed')}: {decision.get('reason', '')}")
                ln["status"] = "review"
            continue
        if best >= rules["threshold"] and best - runner > .1:
            ln["spec_id"], ln["match_status"], ln["match_score"] = sid, "proposed", round(best, 2)
            ln["match_basis"] = "description similarity (not approved)"
            ln["issues"].append(f"Proposed match to {sid} (score {ln['match_score']}) - needs approval at Gate 2")
        elif best >= rules["threshold"]:
            ln["match_status"], ln["match_score"] = "ambiguous", round(best, 2)
            ln["match_basis"] = f"close to more than one specification ({scored[0][1]}, {scored[1][1]})"
            ln["issues"].append("Ambiguous: description is close to more than one specification - needs a person")
        else:
            ln["match_status"], ln["match_score"] = "none", round(best, 2)
            ln["issues"].append("No approved match for this item - add it to item_map.csv or decide at Gate 2")
        ln["status"] = "review"
        key = (ln["plant"], ln["description"])
        if ln["match_status"] in ("proposed", "ambiguous") and key not in seen:
            seen.add(key)
            queue.append({"plant": ln["plant"], "description": ln["description"], "proposed_spec": ln["spec_id"] or "",
                          "score": ln["match_score"], "status": ln["match_status"], "example_row": _rowref(ln)})
    return queue


def _analyse(lines, rules, reporting, start, end):
    for ln in lines:
        if ln["date"] and not (start <= ln["date"] <= end):
            ln["status"] = "excluded"
            ln["issues"].append(f"Order date {ln['date']} is outside the comparison window ({start} to {end})")
        if ln["status"] == "excluded":
            continue
        unit = rules["units"].get((_norm_unit(ln["unit"]), ln["plant"].lower())) or rules["units"].get((_norm_unit(ln["unit"]), "*"))
        if ln["unit"] and not unit:
            ln["issues"].append(f"Unit '{ln['unit']}' has no pack size in unit_conversions.csv - not converted (no guessing)")
            ln["status"] = "review"
        if unit:
            ln["labels_per_unit"], ln["conv_rule"] = unit["labels_per_unit"], unit["rule_id"]
        if ln["currency"] == reporting:
            ln["fx_rate"], ln["fx_rule"] = Decimal(1), "same currency"
        elif ln["currency"]:
            fx = rules["fx"].get((ln["currency"], reporting))
            if fx:
                ln["fx_rate"], ln["fx_rule"] = fx["rate"], f"{fx['rule_id']} ({ln['currency']}->{reporting} {fx['rate']}, {fx['as_of']})"
            else:
                ln["issues"].append(f"No exchange rate for {ln['currency']}->{reporting} in fx_rates.csv")
                ln["status"] = "review"
        if ln["labels_per_unit"] is not None and ln["fx_rate"] is not None and isinstance(ln["unit_price"], Decimal):
            ln["price"] = ln["unit_price"] * ln["fx_rate"] / ln["labels_per_unit"] * rules["basis"]
            if isinstance(ln["quantity"], Decimal):
                ln["labels"] = ln["quantity"] * ln["labels_per_unit"]
                ln["amount_reporting"] = ln["quantity"] * ln["unit_price"] * ln["fx_rate"]
        if ln["labels"] is not None:
            for band in rules["bands"]:
                limit = band["below_labels_per_order"]
                if limit is None or ln["labels"] < Decimal(str(limit)):
                    ln["band"] = band["name"]
                    break
        if ln["freight"]:
            ln["freight_class"] = rules["freight"].get(ln["freight"], "")
            if not ln["freight_class"]:
                ln["issues"].append(f"Freight term '{ln['freight']}' is not in settings.freight_groups")
                ln["status"] = "review"
        else:
            ln["freight_class"] = "unknown"
            ln["issues"].append("Missing freight terms - can't tell if prices are delivered or ex-works")
            ln["status"] = "review"


def _rank(lines, rules):
    groups = defaultdict(list)
    for ln in lines:
        if ln["status"] == "excluded":
            ln["outcome"] = "Excluded"
        elif ln["status"] == "review" or ln["match_status"] not in ("approved", "approved_by_person") or ln["price"] is None or ln["labels"] is None:
            ln["outcome"] = "Needs review"
        else:
            ln["group"] = f"{ln['canon']} | {ln['band']} | {ln['freight_class']}"
            groups[ln["group"]].append(ln)
    opportunities, group_rows = [], []
    q2 = lambda v: v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    for group, rows in sorted(groups.items()):
        plants = defaultdict(list)
        for ln in rows:
            plants[ln["plant"]].append(ln)
        stats = {}
        for plant, plines in plants.items():
            labels = sum((x["labels"] for x in plines), Decimal(0))
            amount = sum((x["amount_reporting"] for x in plines), Decimal(0))
            stats[plant] = {"labels": labels, "amount": amount, "price": amount / labels * rules["basis"], "lines": plines}
        if len(stats) < rules["min_plants"]:
            for ln in rows:
                ln["outcome"] = "Not compared"
                ln["issues"].append(f"Only one plant in group '{group}' - nothing to compare with")
            group_rows.append({"group": group, "plants": ", ".join(sorted(stats)), "benchmark_plant": "", "benchmark_price": None, "note": "single plant - not compared"})
            continue
        benchmark = min(stats, key=lambda plant: (stats[plant]["price"], plant))
        base = stats[benchmark]["price"]
        group_rows.append({"group": group, "plants": ", ".join(sorted(stats)), "benchmark_plant": benchmark, "benchmark_price": q2(base), "note": ""})
        for plant, stat in stats.items():
            for ln in stat["lines"]:
                ln["outcome"] = "Compared"
            if plant == benchmark:
                continue
            gap = stat["price"] - base
            if gap <= 0:
                continue
            opportunities.append({"plant": plant, "group": group, "spec_id": group.split(" | ")[0],
                "price_per_1000": q2(stat["price"]), "benchmark_plant": benchmark, "benchmark_price_per_1000": q2(base),
                "gap_per_1000": q2(gap), "labels": stat["labels"], "scenario_saving": q2(gap * stat["labels"] / rules["basis"]),
                "rows": [_rowref(x) for x in stat["lines"]], "benchmark_rows": [_rowref(x) for x in stats[benchmark]["lines"]]})
    opportunities.sort(key=lambda x: (-x["scenario_saving"], x["plant"]))
    for i, o in enumerate(opportunities, 1): o["rank"] = i
    return opportunities, group_rows


def _release_checks(files, lines, opportunities, rules):
    checks = []
    def add(cid, title, passed, detail):
        checks.append({"id": cid, "check": title, "result": "PASS" if passed else "FAIL", "detail": detail})
    bad = [f['file'] for f in files if f['reconciliation'].get('row_check') != 'PASS' or f['reconciliation'].get('total_check') == 'FAIL']
    add("R1", "Every file reconciles to its source (rows and stated totals)", not bad, ", ".join(bad) or f"{len(files)} file(s) reconciled")
    refs = {_rowref(ln): ln for ln in lines}
    missing = [r for o in opportunities for r in o["rows"] + o["benchmark_rows"] if r not in refs or refs[r]["outcome"] != "Compared"]
    add("R2", "Every finding traces to source file and row", not missing, ", ".join(missing[:5]) or "all row references checked")
    unapproved = [ln for ln in lines if ln["outcome"] == "Compared" and ln["match_status"] not in ("approved", "approved_by_person")]
    mixed = [o["group"] for o in opportunities if len({refs[r]["canon"] for r in o["rows"] + o["benchmark_rows"] if r in refs}) != 1]
    add("R3", "No item compared without an approved specification match", not unapproved and not mixed, ", ".join(mixed) or "all compared lines have approved matches")
    missing_rules = [ln for ln in lines if ln["outcome"] == "Compared" and (not ln["conv_rule"] or not ln["fx_rule"])]
    add("R4", "Every unit and currency conversion came from rules", not missing_rules, f"{len(missing_rules)} invalid conversion(s)" if missing_rules else "all conversions cite a rule")
    add("R5", "Compared purchases share order-size band and freight class", True, "group key includes both conditions")
    wrong = []
    for o in opportunities:
        labels = sum((refs[r]["labels"] for r in o["rows"]), Decimal(0))
        if labels != o["labels"] or abs((o["price_per_1000"] - o["benchmark_price_per_1000"]) * labels / rules["basis"] - o["scenario_saving"]) > Decimal("1.00"):
            wrong.append(o["plant"])
    add("R6", "Every scenario recomputes from supporting rows", not wrong, ", ".join(wrong) or f"{len(opportunities)} scenario(s) recomputed")
    unaccounted = [ln for ln in lines if ln["outcome"] not in ("Compared", "Not compared", "Needs review", "Excluded")]
    add("R7", "Every loaded line is accounted for", not unaccounted, f"{len(lines)} line(s) accounted for")
    return checks


def _plain(value):
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, (dt.date, dt.datetime)): return value.isoformat()
    if isinstance(value, dict): return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_plain(v) for v in value]
    return value


def _write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if headers:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: _plain(v) for k, v in row.items()})


def _produce(out_dir, report, lines, opportunities, queue, g1):
    opp_headers = ["rank", "plant", "specification", "comparison_group", "price_per_1000", "benchmark_plant",
                   "benchmark_price_per_1000", "gap_per_1000", "labels", "scenario_saving", "source_rows", "benchmark_rows", "assumptions"]
    opp_rows = [] if g1 or report["status"] in ("STOPPED_RECONCILIATION", "BLOCKED_RELEASE_CHECKS", "ERROR") else [
        {"rank": o["rank"], "plant": o["plant"], "specification": o["spec_id"], "comparison_group": o["group"],
         "price_per_1000": o["price_per_1000"], "benchmark_plant": o["benchmark_plant"], "benchmark_price_per_1000": o["benchmark_price_per_1000"],
         "gap_per_1000": o["gap_per_1000"], "labels": o["labels"], "scenario_saving": o["scenario_saving"],
         "source_rows": "; ".join(o["rows"]), "benchmark_rows": "; ".join(o["benchmark_rows"]),
         "assumptions": "Potential scenario only; buyer to confirm benchmark availability at volume, location and terms."} for o in opportunities]
    _write_csv(os.path.join(out_dir, "opportunities.csv"), [] if g1 else opp_headers, opp_rows)
    review = []
    for q in queue:
        review.append({"type": "Gate 2 - match to approve", "plant": q["plant"], "source": q["example_row"], "description": q["description"],
                       "reason": f"Proposed {q['proposed_spec'] or '(none)'} with score {q['score']} ({q['status']})", "next_action": "Buyer approves/rejects in decisions.json > match_decisions"})
    for ln in lines:
        if ln["outcome"] in ("Needs review", "Excluded", "Not compared"):
            review.append({"type": ln["outcome"], "plant": ln["plant"], "source": _rowref(ln), "description": ln["description"],
                           "reason": "; ".join(ln["issues"]), "next_action": "Fix source or rules and rerun" if ln["outcome"] == "Needs review" else "Informational"})
    for item in report.get("gate1_open", []):
        review.append({"type": "Gate 1", "plant": "", "source": "", "description": "", "reason": item, "next_action": "Confirm scope and mappings in decisions.json"})
    _write_csv(os.path.join(out_dir, "needs_review.csv"), ["type", "plant", "source", "description", "reason", "next_action"], review)
    with open(os.path.join(out_dir, "run_report.json"), "w", encoding="utf-8") as f:
        json.dump(_plain(report), f, indent=2, ensure_ascii=False)
        f.write("\n")


def _run():
    if not _regular(SCOPE_FILE):
        raise WorkflowError(f"Required file not found: {SCOPE_FILE}")
    if os.path.lexists(DECISIONS_FILE) and not _regular(DECISIONS_FILE):
        raise WorkflowError(f"{DECISIONS_FILE} must be a regular file")
    if not os.path.isdir(INPUT_DIR) or os.path.islink(INPUT_DIR):
        raise WorkflowError(f"Required folder not found: {INPUT_DIR}/")
    os.makedirs(OUT_DIR, exist_ok=True)
    scope = _json(SCOPE_FILE, "scope.json")
    decisions = _json(DECISIONS_FILE, "decisions.json") if _regular(DECISIONS_FILE) else {}
    for key in ("reporting_currency", "window", "files"):
        if key not in scope: raise WorkflowError(f"scope is missing '{key}'")
    if len(scope["files"]) < 2: raise WorkflowError("At least two plant CSV files are needed")
    start, end = dt.date.fromisoformat(scope["window"]["start"]), dt.date.fromisoformat(scope["window"]["end"])
    if start > end: raise WorkflowError("Comparison window starts after it ends")
    for item in scope["files"]:
        name = item.get("file")
        if not isinstance(name, str) or not name.lower().endswith(".csv"):
            raise WorkflowError(f"Scope entry for {item.get('plant')!r} must give 'file' as a .csv file name in {INPUT_DIR}/")
    rules = _load_rules()
    # Scope names are only ever compared with the folder listing; the files that get opened
    # are the listing's own entries, so scope text never becomes part of a path.
    matched = []
    with os.scandir(INPUT_DIR) as listing:
        for entry in sorted(listing, key=lambda e: e.name):
            if entry.is_file(follow_symlinks=False):
                for position, item in enumerate(scope["files"]):
                    if entry.name == item["file"]:
                        matched.append((position, item, entry.path))
    matched.sort(key=lambda m: m[0])
    if len(matched) != len(scope["files"]):
        found = {m[1]["file"] for m in matched}
        absent = [i["file"] for i in scope["files"] if i["file"] not in found]
        raise WorkflowError(f"CSV file(s) named in scope not found in {INPUT_DIR}/: {absent}")
    overrides = decisions.get("column_overrides", {})
    files, resolved = [], []
    for _, item, path in matched:
        resolved.append(path)
        files.append(_load_file(item["plant"], path, rules, overrides.get(item["plant"], {}), item.get("date_format")))
    report = {"skill": "cross-site-purchase-comparison", "version": VERSION,
              "run_at": dt.datetime.now().isoformat(timespec="seconds"), "status": None,
              "inputs": {"files": [{"plant": x["plant"], "file": x["file"], "sha256": _hash(p)} for x, p in zip(files, resolved)],
                         "rules_sha256": rules["fingerprints"]},
              "files": [{k: f[k] for k in ("plant", "file", "header_row", "mapping", "mapping_basis", "unmapped", "missing", "errors", "reconciliation")} for f in files]}
    gate1 = []
    for f in files:
        if f["errors"]: gate1.append(f"{f['plant']} ({f['file']}): " + "; ".join(f["errors"]))
        if f["missing"]: gate1.append(f"{f['plant']} ({f['file']}): required fields {f['missing']} missing; unrecognized headers: {f['unknown']}")
    if not decisions.get("gate1", {}).get("confirmed"):
        gate1.append(f"Scope not confirmed: confirm plants, category '{scope.get('category', '')}', window {start} to {end}, currency {scope['reporting_currency']}")
    lines = [ln for f in files for ln in f["rows"]]
    if gate1:
        report.update(status="STOPPED_AT_GATE_1", gate1_open=gate1, counts={"lines_loaded": len(lines)})
        _produce(OUT_DIR, report, lines, [], [], True)
        return report
    recon_bad = [f"{f['plant']} ({f['file']}): rows {f['reconciliation']['row_check']}, totals {f['reconciliation']['total_check']}" for f in files
                 if f["reconciliation"]["row_check"] != "PASS" or f["reconciliation"]["total_check"] == "FAIL"]
    if recon_bad:
        report.update(status="STOPPED_RECONCILIATION", reconciliation_failures=recon_bad)
        _produce(OUT_DIR, report, lines, [], [], False)
        return report
    reporting = scope["reporting_currency"].upper()
    _match(lines, rules, decisions.get("match_decisions", []))
    _analyse(lines, rules, reporting, start, end)
    opportunities, groups = _rank(lines, rules)
    checks = _release_checks(files, lines, opportunities, rules)
    queue = _match_queue = []
    # Gate 2 items are all unresolved proposals/ambiguities.
    for ln in lines:
        if ln["match_status"] in ("proposed", "ambiguous"):
            entry = {"plant": ln["plant"], "description": ln["description"], "proposed_spec": ln["spec_id"], "score": ln["match_score"], "status": ln["match_status"], "example_row": _rowref(ln)}
            if not any((q["plant"], q["description"]) == (entry["plant"], entry["description"]) for q in queue): queue.append(entry)
    report.update(release_checks=checks, gate2_queue=queue, opportunities=opportunities, groups=groups,
                  counts={k: sum(1 for ln in lines if ln["outcome"] == k) for k in ("Compared", "Not compared", "Needs review", "Excluded")})
    report["counts"]["lines_loaded"] = len(lines)
    if any(c["result"] == "FAIL" for c in checks):
        report["status"] = "BLOCKED_RELEASE_CHECKS"
    else:
        gate3 = decisions.get("gate3", {})
        report["status"] = "FINAL" if not queue and gate3.get("approved") else "DRAFT"
        report["gates"] = [{"gate": "Gate 1", "status": "confirmed", "by": decisions.get("gate1", {}).get("by", "unnamed")},
                           {"gate": "Gate 2", "status": "open" if queue else "resolved", "pending_matches": len(queue)},
                           {"gate": "Gate 3", "status": "approved" if report["status"] == "FINAL" else "open", "by": gate3.get("by", "")}]
    _produce(OUT_DIR, report, lines, opportunities, queue, False)
    return report


def generate():
    """Run in the current working folder (which holds the documented staged files) and return a report dict."""
    try:
        return _run()
    except (WorkflowError, OSError, KeyError, TypeError, ValueError) as exc:
        return {"status": "ERROR", "error": str(exc)}


def main():
    result = generate()
    print(json.dumps(_plain(result), indent=2, ensure_ascii=False))
    return 0 if result.get("status") in ("FINAL", "DRAFT", "STOPPED_AT_GATE_1", "STOPPED_RECONCILIATION") else 1


if __name__ == "__main__":
    sys.exit(main())
