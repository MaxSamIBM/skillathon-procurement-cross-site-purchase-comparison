#!/usr/bin/env python3
"""Cross-site purchase comparison: deterministic engine (Python standard library only).

Process Studio contract (from the platform team): the script runs with the run's temp
 working directory as cwd. Parsed chat attachments are auto-staged there as
 uploaded_<name>, with .txt appended to anything that isn't .md/.txt. This script
 recognises those names as well as plain ones (scope.json, rules.json, decisions.json,
 plant CSVs, optionally inside inputs_csv/). Outputs go to cwd/out/.
 The name deliberately does not match generate_*: the agent orchestrates the gates.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

VERSION = "0.7.0"
RULE_FILES = ("columns.json", "settings.json", "specs.csv", "item_map.csv",
              "spec_equivalences.csv", "abbreviations.csv", "unit_conversions.csv", "fx_rates.csv")
RULES_FILE = "rules.json"
RULE_TABLES = {"specs": ("spec_id", "spec_name"),
               "item_map": ("plant", "item_code", "description", "spec_id", "status"),
               "spec_equivalences": ("spec_a", "spec_b", "status"),
               "abbreviations": ("short", "long"),
               "unit_conversions": ("rule_id", "unit_alias", "plant", "labels_per_unit"),
               "fx_rates": ("rule_id", "currency", "rate_to_reporting", "reporting_currency")}
TOTAL_RE = re.compile(r"\b(grand\s*total|sub\s*-?\s*total|total)\b", re.I)


class WorkflowError(ValueError):
    pass


SCOPE_NAMES = ("scope.json", "scope_csv.json")
DECISIONS_NAME = "decisions.json"
RULES_DIR = "rules"
INPUT_DIR = "inputs_csv"
OUT_DIR = "out"


# In-memory mode (used only by the built-in demo): _VFS maps names to text, _OUT collects outputs.
_VFS = None
_OUT = None


def _open_text(path, encoding="utf-8", newline=None):
    if _VFS is not None:
        return io.StringIO(_VFS[path], newline=newline)
    return open(path, newline=newline, encoding=encoding)


def _emit(path, text):
    """Write an output file, or keep it in memory during the built-in demo."""
    if _VFS is not None:
        _OUT[os.path.basename(path)] = text
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(text)


UPLOAD_PREFIX = "uploaded_"
KNOWN_EXTS = (".csv", ".json")


def _canonical(name):
    """Map a platform-staged name to the name a user attached.

    uploaded_plant_A.csv.txt -> plant_A.csv ; uploaded_rules.json.txt -> rules.json ;
    plain names are returned unchanged. Only ever applied to names from a folder listing.
    """
    if name.startswith(UPLOAD_PREFIX):
        name = name[len(UPLOAD_PREFIX):]
    if name.lower().endswith(".txt") and name[:-4].lower().endswith(KNOWN_EXTS):
        name = name[:-4]
    return name


def _raw_listing(folder):
    """(entry_name, path) for each regular, non-symlink file directly in a folder."""
    if _VFS is not None:
        if folder == os.curdir:
            return sorted((k, k) for k in _VFS if "/" not in k)
        prefix = folder + "/"
        return sorted((k[len(prefix):], k) for k in _VFS if k.startswith(prefix) and "/" not in k[len(prefix):])
    with os.scandir(folder) as listing:
        return sorted((e.name, e.path) for e in listing if e.is_file(follow_symlinks=False))


def _index(folder):
    """canonical name -> path. A plain name beats an uploaded_ alias; two aliases for one name is an error."""
    plain, alias = {}, {}
    for entry_name, path in _raw_listing(folder):
        canon = _canonical(entry_name)
        target = plain if canon == entry_name else alias
        if canon in target:
            raise WorkflowError(f"Two staged files both look like '{canon}'; attach it only once")
        target[canon] = path
    merged = dict(alias)
    merged.update(plain)
    return merged


def _input_listing(input_dir):
    """(canonical name, path) for each staged file in the input folder, sorted by name."""
    return sorted(_index(input_dir).items())


def _find_scope():
    found = _index(os.curdir)
    for name in SCOPE_NAMES:
        if name in found:
            return found[name]
    raise WorkflowError("Required file not found: scope.json (or scope_csv.json)")


def _find_decisions():
    """decisions.json, or the single decisions*.json present; none means no decisions yet."""
    found = _index(os.curdir)
    if DECISIONS_NAME in found:
        return found[DECISIONS_NAME]
    matches = sorted(n for n in found if n.startswith("decisions") and n.endswith(".json"))
    if len(matches) > 1:
        raise WorkflowError(f"More than one decisions file found {matches}; attach or write only the one for this stage")
    return found[matches[0]] if matches else None


def _find_setup():
    return _index(os.curdir).get("setup.json")


def _load_scope():
    """The scope built from setup.json when setup.json is present (one source of truth), otherwise scope.json."""
    if _find_setup():
        built = _build_setup()
        if built["questions"]:
            raise WorkflowError(f"setup.json is incomplete: {len(built['questions'])} question(s) still open. Run --mode setup.")
        return built["scope"]
    return _json(_find_scope(), "scope")


def _find_rules_file():
    return _index(os.curdir).get(RULES_FILE)


def _regular(path):
    """True for an existing regular file that is not a symlink."""
    if _VFS is not None:
        return path in _VFS
    return os.path.isfile(path) and not os.path.islink(path)


def _hash(path):
    h = hashlib.sha256()
    if _VFS is not None:
        h.update(_VFS[path].encode("utf-8"))
        return h.hexdigest()
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
        with _open_text(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise WorkflowError(f"{what} is not readable valid JSON: {e}")


_DECISION_KEYS = {"gate1", "gate2", "gate3", "column_overrides", "match_decisions", "_doc"}


def _check_decisions(decisions):
    """A decisions file with the wrong shape must fail loudly, never be read as 'nothing decided'."""
    if not isinstance(decisions, dict):
        raise WorkflowError("decisions must be a JSON object like the one in examples/synthetic/decisions_1_scope_confirmed.json")
    unknown = sorted(k for k in decisions if k not in _DECISION_KEYS)
    if unknown:
        raise WorkflowError(f"decisions has unrecognised key(s) {unknown}; allowed keys are {sorted(_DECISION_KEYS - {'_doc'})}. "
                            'Gate 1 looks like {"gate1": {"confirmed": true, "by": "<role>"}}')
    for gate in ("gate1", "gate3"):
        if gate in decisions and not isinstance(decisions[gate], dict):
            raise WorkflowError(f'"{gate}" must be an object such as {{"confirmed": true, "by": "<role>"}}')
    if "match_decisions" in decisions and not isinstance(decisions["match_decisions"], list):
        raise WorkflowError('"match_decisions" must be a list')
    if "column_overrides" in decisions and not isinstance(decisions["column_overrides"], dict):
        raise WorkflowError('"column_overrides" must be an object keyed by plant')


def _read_csv(path, required=()):
    with _open_text(path, "utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        missing = [c for c in required if c not in (rd.fieldnames or [])]
        if missing:
            raise WorkflowError(f"{os.path.basename(path)} missing column(s): {missing}")
        return [{k: (v or "").strip() for k, v in row.items() if k}
                for row in rd if any((v or "").strip() for v in row.values())]


def _table_rows(rows, name, required):
    """Rows of one rules table given as a JSON list of objects; same cleaning as _read_csv."""
    if not isinstance(rows, list):
        raise WorkflowError(f"rules.json: '{name}' must be a list of rows")
    out = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise WorkflowError(f"rules.json: '{name}' row {i} must be an object")
        missing = [c for c in required if c not in row]
        if missing:
            raise WorkflowError(f"rules.json: '{name}' row {i} missing field(s): {missing}")
        clean = {str(k): ("" if v is None else str(v)).strip() for k, v in row.items()}
        if any(clean.values()):
            out.append(clean)
    return out


def _rules_source(use_setup=True):
    """Rules come from setup.json answers (built here), one rules.json, or the eight files in rules/."""
    if use_setup and _find_setup():
        built = _build_setup()
        if built["questions"]:
            first = built["questions"][0]["ask"]
            raise WorkflowError(f"setup.json is incomplete: {len(built['questions'])} question(s) still open (first: {first}). Run --mode setup.")
        pack = built["pack"]
        tables = {k: _table_rows(pack[k], k, req) for k, req in RULE_TABLES.items()}
        fingerprints = {SETUP_NAME: _hash(_find_setup())}
        if _find_rules_file():
            fingerprints[RULES_FILE] = _hash(_find_rules_file())
        return pack["columns"], pack["settings"], tables, fingerprints
    rules_path = _find_rules_file()
    if rules_path:
        pack = _json(rules_path, RULES_FILE)
        if not isinstance(pack, dict):
            raise WorkflowError("rules.json must be a JSON object")
        missing = [k for k in ("columns", "settings", *RULE_TABLES) if k not in pack]
        if missing:
            raise WorkflowError(f"rules.json missing section(s): {missing}")
        tables = {k: _table_rows(pack[k], k, req) for k, req in RULE_TABLES.items()}
        return pack["columns"], pack["settings"], tables, {RULES_FILE: _hash(rules_path)}
    if not os.path.isdir(RULES_DIR) or os.path.islink(RULES_DIR):
        raise WorkflowError(f"Rules not found: attach {RULES_FILE} (or a {RULES_DIR}/ folder)")
    paths = {n: os.path.join(RULES_DIR, n) for n in RULE_FILES}
    for n, rule_path in paths.items():
        if not _regular(rule_path):
            raise WorkflowError(f"Required rules file not found: {RULES_DIR}/{n}")
    tables = {k: _read_csv(paths[k + ".csv"], req) for k, req in RULE_TABLES.items()}
    return (_json(paths["columns.json"], "columns.json"), _json(paths["settings.json"], "settings.json"),
            tables, {n: _hash(p) for n, p in paths.items()})


def _provenance(columns, settings, tables, decided=frozenset()):
    """Count rules by where they came from. Rows tagged source=agent_proposed are unconfirmed.
    A proposed item_map row that a person has since decided at Gate 2 counts as confirmed."""
    counts = {"user_confirmed": 0, "agent_proposed": 0, "untagged": 0}
    pending = []
    for name, rows in [("columns", [columns]), ("settings", [settings])] + list(tables.items()):
        for i, row in enumerate(rows, 1):
            tag = str(row.get("source", "")).strip().lower() if isinstance(row, dict) else ""
            if tag == UNCONFIRMED and name == "item_map" and (str(row.get("plant", "")).strip().lower(), _norm(row.get("description", ""))) in decided:
                counts["user_confirmed"] += 1
            elif tag == UNCONFIRMED:
                counts["agent_proposed"] += 1
                pending.append(f"{name} #{i}" if name not in ("columns", "settings") else name)
            elif tag == "user_confirmed":
                counts["user_confirmed"] += 1
            else:
                counts["untagged"] += 1
    return counts, pending


def _load_rules(decisions=None):
    columns, settings, tables, fingerprints = _rules_source()
    decided = {(str(d["plant"]).strip().lower(), _norm(d["description"])) for d in ((decisions or {}).get("match_decisions") or [])
               if isinstance(d, dict) and d.get("plant") and d.get("description")}
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
    bands = settings["volume_bands"]
    if not bands or bands[-1].get("below_labels_per_order") is not None:
        raise WorkflowError("settings.json: last volume band must have below_labels_per_order = null")
    prov_counts, prov_pending = _provenance(columns, settings, tables, decided)
    rules = {"provenance": prov_counts, "unconfirmed": prov_pending, "required": required, "fields": fields, "aliases": aliases,
             "basis": _decimal(settings.get("price_basis_labels", 1000), "price basis"),
             "threshold": float(settings.get("matching", {}).get("propose_threshold", .6)),
             "bands": bands, "freight": {k.strip().upper(): v for k, v in settings["freight_groups"].items()},
             "min_plants": int(settings.get("min_plants_per_group", 2)),
             "tolerance": _decimal(settings.get("amount_tolerance", "0.01"), "amount tolerance"),
             "specs": {}, "item_map": [], "code_map": {}, "desc_map": {}, "parent": {},
             "abbr": {}, "units": {}, "fx": {}, "fingerprints": fingerprints}
    for row in tables["specs"]:
        rules["specs"][row["spec_id"]] = row["spec_name"]
    for row in tables["item_map"]:
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

    for row in tables["spec_equivalences"]:
        for key in ("spec_a", "spec_b"):
            if row[key] not in rules["specs"]:
                raise WorkflowError(f"spec_equivalences.csv: unknown spec '{row[key]}'")
        if row["status"].lower() == "approved":
            a, b = root(row["spec_a"]), root(row["spec_b"])
            rules["parent"][max(a, b)] = min(a, b)
    for row in tables["abbreviations"]:
        rules["abbr"][_norm(row["short"])] = _norm(row["long"])
    for row in tables["unit_conversions"]:
        amount = _decimal(row["labels_per_unit"], f"unit_conversions {row['rule_id']}")
        if amount <= 0:
            raise WorkflowError(f"unit_conversions {row['rule_id']}: labels_per_unit must be > 0")
        key = (_norm_unit(row["unit_alias"]), row["plant"].strip().lower() or "*")
        if key in rules["units"]:
            raise WorkflowError(f"unit_conversions: duplicate rule for {key}")
        rules["units"][key] = {"rule_id": row["rule_id"], "labels_per_unit": amount}
    for row in tables["fx_rates"]:
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


def _load_file(plant, path, rules, overrides, date_format, label=None):
    # label is the name the user attached; path may be the platform-staged alias.
    result = {"plant": plant, "file": label or os.path.basename(path), "header_row": None, "mapping": {},
              "mapping_basis": {}, "unmapped": [], "missing": [], "unknown": [], "errors": [],
              "rows": [], "skipped": [], "reconciliation": {}}
    with _open_text(path, "utf-8-sig", newline="") as f:
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
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    if headers:
        writer.writeheader()
    for row in rows:
        writer.writerow({k: _plain(v) for k, v in row.items()})
    _emit(path, buf.getvalue())


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
    for item in report.get("gate0_open", []):
        review.append({"type": "Gate 0", "plant": "", "source": "", "description": "", "reason": item, "next_action": "Show rules.json and scope.json and record the approval"})
    for item in report.get("gate1_open", []):
        review.append({"type": "Gate 1", "plant": "", "source": "", "description": "", "reason": item, "next_action": "Confirm scope and mappings in decisions.json"})
    _write_csv(os.path.join(out_dir, "needs_review.csv"), ["type", "plant", "source", "description", "reason", "next_action"], review)
    _emit(os.path.join(out_dir, "run_report.json"), json.dumps(_plain(report), indent=2, ensure_ascii=False) + "\n")


def _sha_obj(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _case_text(plants, gate0, rules12, scope12, recorded, open_questions, decisions=None):
    """The case card as plain text. The agent prints it verbatim at the end of every reply, so the chat itself holds the whole case."""
    rows = [f"  - {x['plant']}: {x['file']} | {x['rows']} rows | fingerprint {x['sha256_12']}" for x in plants] or ["  - (no plant files attached)"]
    out = [f"CASE CARD (skill v{VERSION})", "Plant files on this case:", *rows,
           f"Gate 0 (rules and scope): {gate0}" + (f" | rules {rules12} | scope {scope12}" if rules12 else ""),
           f"Open questions: {open_questions}", "Answers recorded:"]
    out += [f"  - {r}" for r in recorded] or ["  - (none yet)"]
    d = decisions or {}
    gates = []
    if d.get("gate1", {}).get("confirmed"): gates.append(f"Gate 1 by {d['gate1'].get('by', '?')}")
    for m in d.get("match_decisions", []) or []:
        if isinstance(m, dict): gates.append(f"Gate 2: {m.get('plant', '?')} '{m.get('description', '?')}' {m.get('decision', '?')} by {m.get('by', '?')}")
    if d.get("gate3", {}).get("approved"): gates.append(f"Gate 3 signed by {d['gate3'].get('by', '?')}")
    out.append("Later decisions: " + ("; ".join(gates) if gates else "none yet"))
    return "\n".join(out)


def _plant_card(scan_rows):
    idx = _index(_input_dir())
    return [{"plant": r["plant"], "file": r["file"], "rows": r["rows"], "sha256_12": _hash(idx[r["file"]])[:12]} for r in scan_rows if r.get("file") in idx]


def _input_dir():
    return INPUT_DIR if (os.path.isdir(INPUT_DIR) and not os.path.islink(INPUT_DIR)) else os.curdir


def _scope_plant_hashes(scope):
    """sha256 of each plant file named in the scope that is present now. Names are only looked up in the folder listing."""
    idx = _index(_input_dir())
    return {item["file"]: _hash(idx[item["file"]]) for item in scope.get("files", []) if item.get("file") in idx}


def _approval_token(pack, scope, plant_hashes):
    return {"rules_sha256": _sha_obj(pack), "scope_sha256": _sha_obj(scope), "plant_files": dict(sorted(plant_hashes.items()))}


def _check_approval(built, plant_hashes):
    """Gate 0. Returns (approver or None, [what is open]). The approval is bound to the exact rules, scope and plant files."""
    ans = _setup_answers().get("approval")
    if not isinstance(ans, dict) or ans.get("approved") is not True:
        return None, ["Gate 0 not approved: show the person rules.json and scope.json built from their answers, then record their approval in setup.json"]
    by = str(ans.get("by", "")).strip()
    if not by:
        return None, ["Gate 0 approval has no 'by' (the approver's role)"]
    now = _approval_token(built["pack"], built["scope"], plant_hashes)
    problems = []
    if ans.get("rules_sha256") != now["rules_sha256"] or ans.get("scope_sha256") != now["scope_sha256"]:
        problems.append("The rules or scope changed after they were approved. Run --mode setup, show the new files and ask for approval again")
    old = ans.get("plant_files") if isinstance(ans.get("plant_files"), dict) else {}
    for name in sorted(set(old) | set(now["plant_files"])):
        if name not in now["plant_files"]:
            problems.append(f"Plant file {name} was approved but is not attached now. Attach it again")
        elif name not in old:
            problems.append(f"Plant file {name} was not part of the approved set")
        elif old[name] != now["plant_files"][name]:
            problems.append(f"Plant file {name} is not the file that was approved (its content differs)")
    return (None, problems) if problems else (by, [])


def _run():
    decisions_name = _find_decisions()
    if _VFS is not None:
        input_dir = INPUT_DIR
    else:
        input_dir = INPUT_DIR if (os.path.isdir(INPUT_DIR) and not os.path.islink(INPUT_DIR)) else os.curdir
        os.makedirs(OUT_DIR, exist_ok=True)
    scope = _load_scope()
    decisions = _json(decisions_name, "decisions") if decisions_name else {}
    _check_decisions(decisions)
    for key in ("reporting_currency", "window", "files"):
        if key not in scope: raise WorkflowError(f"scope is missing '{key}'")
    if len(scope["files"]) < 2: raise WorkflowError("At least two plant CSV files are needed")
    start, end = dt.date.fromisoformat(scope["window"]["start"]), dt.date.fromisoformat(scope["window"]["end"])
    if start > end: raise WorkflowError("Comparison window starts after it ends")
    for item in scope["files"]:
        name = item.get("file")
        if not isinstance(name, str) or not name.lower().endswith(".csv") or os.path.basename(name) != name:
            raise WorkflowError(f"Scope entry for {item.get('plant')!r} must give 'file' as a bare .csv file name")
    rules = _load_rules(decisions)
    # Scope names are only ever compared with the folder listing; the files that get opened
    # are the listing's own entries, so scope text never becomes part of a path.
    matched = []
    for entry_name, entry_path in _input_listing(input_dir):
        for position, item in enumerate(scope["files"]):
            if entry_name == item["file"]:
                matched.append((position, item, entry_path))
    matched.sort(key=lambda m: m[0])
    if len(matched) != len(scope["files"]):
        found = {m[1]["file"] for m in matched}
        absent = [i["file"] for i in scope["files"] if i["file"] not in found]
        dir_label = f"{input_dir}/" if input_dir != os.curdir else "working directory"
        raise WorkflowError(f"CSV file(s) named in scope not found in {dir_label}: {absent}")
    overrides = decisions.get("column_overrides", {})
    files, resolved = [], []
    for _, item, path in matched:
        resolved.append(path)
        files.append(_load_file(item["plant"], path, rules, overrides.get(item["plant"], {}), item.get("date_format"), item["file"]))
    report = {"skill": "cross-site-purchase-comparison", "version": VERSION,
              "run_at": dt.datetime.now().isoformat(timespec="seconds"), "status": None,
              "inputs": {"decisions_file": (decisions_name[2:] if decisions_name and decisions_name.startswith("./") else decisions_name) or "none (no decisions yet)",
                         "files": [{"plant": x["plant"], "file": x["file"], "sha256": _hash(p)} for x, p in zip(files, resolved)],
                         "rules_sha256": rules["fingerprints"]},
              "rules_provenance": rules["provenance"],
              "files": [{k: f[k] for k in ("plant", "file", "header_row", "mapping", "mapping_basis", "unmapped", "missing", "errors", "reconciliation")} for f in files]}
    lines = [ln for f in files for ln in f["rows"]]
    approval_by, gate0_open, built_setup = None, [], None
    if _VFS is None and _find_setup():
        built_setup = _build_setup()
        approval_by, gate0_open = _check_approval(built_setup, _scope_plant_hashes(built_setup["scope"]))
        report["inputs"]["gate0"] = {"source": SETUP_NAME, "approved_by": approval_by or "",
                                     "rules_sha256": _sha_obj(built_setup["pack"]), "scope_sha256": _sha_obj(built_setup["scope"])}
        if approval_by:
            for fname, content in (("rules.json", built_setup["pack"]), ("scope.json", built_setup["scope"])):
                _emit(os.path.join(OUT_DIR, fname), json.dumps(content, indent=2, ensure_ascii=False) + "\n")
    report["case_card"] = {
        "plant_files": [{"plant": x["plant"], "file": x["file"], "sha256_12": _hash(p)[:12], "rows_loaded": x["reconciliation"]["rows_loaded"],
                         "loaded_amount": x["reconciliation"]["loaded_amount"], "total_check": x["reconciliation"]["total_check"]}
                        for x, p in zip(files, resolved)],
        "gate0": ("approved by " + approval_by) if approval_by else ("open" if built_setup is not None else "not used (rules and scope were supplied)"),
        "rules_sha256_12": (_sha_obj(built_setup["pack"])[:12] if built_setup is not None else ""),
        "scope_sha256_12": (_sha_obj(built_setup["scope"])[:12] if built_setup is not None else ""),
        "answers_recorded": built_setup["recorded"] if built_setup is not None else [],
        "decisions_on_file": decisions,
        "setup_json_to_restore": _setup_answers() if built_setup is not None else None,
        "decisions_json_to_restore": decisions}
    report["case_card_text"] = _case_text(
        [{"plant": c["plant"], "file": c["file"], "rows": c["rows_loaded"], "sha256_12": c["sha256_12"]} for c in report["case_card"]["plant_files"]],
        report["case_card"]["gate0"], report["case_card"]["rules_sha256_12"], report["case_card"]["scope_sha256_12"],
        report["case_card"]["answers_recorded"], 0, decisions)
    if gate0_open:
        report.update(status="STOPPED_AT_GATE_0", gate0_open=gate0_open, counts={"lines_loaded": len(lines)})
        _produce(OUT_DIR, report, lines, [], [], True)
        return report
    gate1 = []
    for f in files:
        if f["errors"]: gate1.append(f"{f['plant']} ({f['file']}): " + "; ".join(f["errors"]))
        if f["missing"]: gate1.append(f"{f['plant']} ({f['file']}): required fields {f['missing']} missing; unrecognized headers: {f['unknown']}")
    gate1_by = approval_by or (decisions.get("gate1", {}).get("by") if decisions.get("gate1", {}).get("confirmed") else None)
    if not gate1_by:
        gate1.append(f"Scope not confirmed: confirm plants, category '{scope.get('category', '')}', window {start} to {end}, currency {scope['reporting_currency']}")
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
        unconfirmed = rules["unconfirmed"]
        report["status"] = "FINAL" if not queue and not unconfirmed and gate3.get("approved") else "DRAFT"
        report["gates"] = [{"gate": "Gate 1", "status": "confirmed", "by": gate1_by or "unnamed", "via": "Gate 0 approval" if approval_by else "decisions.json"},
                           {"gate": "Gate 2", "status": "open" if queue else "resolved", "pending_matches": len(queue)},
                           {"gate": "Gate 3", "status": "approved" if report["status"] == "FINAL" else "open", "by": gate3.get("by", "")}]
        if approval_by:
            report["gates"].insert(0, {"gate": "Gate 0", "status": "approved", "by": approval_by, "rules_sha256_12": _sha_obj(built_setup["pack"])[:12],
                                       "scope_sha256_12": _sha_obj(built_setup["scope"])[:12], "held_for_gate2": unconfirmed[:20]})
        elif unconfirmed:
            report["gates"].insert(0, {"gate": "Gate 0", "status": "open", "unconfirmed_rules": len(unconfirmed), "which": unconfirmed[:20],
                                       "fix": 'set "source": "user_confirmed" on each rule once the person has agreed to it'})
    _produce(OUT_DIR, report, lines, opportunities, queue, False)
    return report


def generate():
    """Run in the current working folder (which holds the documented staged files) and return a report dict."""
    try:
        return _run()
    except (WorkflowError, OSError, KeyError, TypeError, ValueError) as exc:
        return {"status": "ERROR", "error": str(exc)}


# ---------------------------------------------------------------------------------------------
# Gate 0: propose the setup from the plant files themselves. Reads only; writes nothing.
# Facts only a person can give (pack sizes, exchange rates, unknown freight terms) become
# blocking questions. Things the data shows (column mapping, date format, abbreviations,
# candidate products, scope) become proposals tagged "agent_proposed" for a person to confirm.
# ---------------------------------------------------------------------------------------------
UNCONFIRMED = "agent_proposed"
DEFAULT_REQUIRED = ["date", "description", "quantity", "purchase_unit", "unit_price", "currency"]
DEFAULT_OPTIONAL = ["item_code", "supplier", "freight_terms", "line_amount"]
DEFAULT_ALIASES = {
    "date": ["po date", "date", "posting date", "order date", "doc date"],
    "item_code": ["material", "item no", "mat code", "item code", "material number", "mat no"],
    "description": ["material description", "item text", "mat desc", "description", "short text", "item description"],
    "quantity": ["order qty", "order qty m", "quantity", "qty", "po quantity"],
    "purchase_unit": ["uom", "unit", "oun", "order unit", "purchase unit"],
    "unit_price": ["net price", "unit price", "price", "price per m"],
    "currency": ["curr", "currency", "crcy", "cur"],
    "supplier": ["vendor", "supplier name", "supplier", "vendor name"],
    "freight_terms": ["incoterm", "delivery terms", "inco terms", "incoterms", "incot"],
    "line_amount": ["net value", "line total", "value", "amount"]}
DEFAULT_SETTINGS = {
    "price_basis_labels": 1000, "matching": {"propose_threshold": 0.6},
    "volume_bands": [{"name": "small", "below_labels_per_order": 100000}, {"name": "standard", "below_labels_per_order": None}],
    "freight_groups": {"DAP": "delivered", "DDP": "delivered", "CPT": "delivered", "CIP": "delivered",
                       "EXW": "ex-works", "FCA": "ex-works", "FOB": "ex-works"},
    "min_plants_per_group": 2, "amount_tolerance": 0.01}
# Order matters: the first match wins ("unit price" is a price, not a unit).
FIELD_KEYWORDS = [("unit_price", ("price",)), ("line_amount", ("value", "amount", "total")),
                  ("quantity", ("qty", "quantity")), ("purchase_unit", ("uom", "oun", "unit")),
                  ("currency", ("curr", "crcy", "cur")), ("freight_terms", ("inco", "incot", "delivery terms", "freight")),
                  ("supplier", ("vendor", "supplier")), ("description", ("desc", "text", "name")),
                  ("item_code", ("material", "item", "code", "mat")), ("date", ("date",))]
DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d")
ISO_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")
NON_PLANT_CSV = set(RULE_FILES)


def _guess_field(header, aliases):
    key = _norm(header)
    if not key:
        return None, None
    if key in aliases:
        return aliases[key], "alias"
    tokens = key.split()
    for field, words in FIELD_KEYWORDS:
        for w in words:
            if (" " in w and w in key) or any(t.startswith(w) for t in tokens):
                return field, "keyword"
    return None, None


def _lenient_rules():
    """Whatever rules exist, section by section. Missing sections are fine here (that is the point)."""
    out = {"present": False, "sections": {}, "columns": None, "settings": None, "problems": [], "files": []}
    path = _find_rules_file()
    if path:
        out["present"] = True
        out["files"] = [RULES_FILE]
        try:
            pack = _json(path, RULES_FILE)
            if not isinstance(pack, dict):
                raise WorkflowError("rules.json must be a JSON object")
            out["columns"], out["settings"] = pack.get("columns"), pack.get("settings")
            for name, required in RULE_TABLES.items():
                if name in pack:
                    out["sections"][name] = _table_rows(pack[name], name, required)
        except WorkflowError as e:
            out["problems"].append(str(e))
        return out
    if os.path.isdir(RULES_DIR) and not os.path.islink(RULES_DIR):
        try:
            columns, settings, tables, _ = _rules_source(use_setup=False)
            out.update(present=True, columns=columns, settings=settings, sections=tables, files=list(RULE_FILES))
        except WorkflowError as e:
            out["problems"].append(str(e))
    return out


def _plant_name_guess(file_name):
    stem = re.sub(r"\.csv$", "", file_name, flags=re.I)
    m = re.search(r"plant[\s_-]*([a-z0-9]+)", stem, re.I)
    if m:
        return "Plant " + m.group(1).upper()
    return re.sub(r"[_-]+", " ", stem).strip().title() or "Plant"


def _date_candidates(values):
    """Formats that read every non-empty date value. Returns [(format, [dates])]."""
    vals = [v.strip() for v in values if v and v.strip()]
    found = []
    if not vals:
        return found
    for f in DATE_FORMATS:
        try:
            found.append((f, [dt.datetime.strptime(v, f).date() for v in vals]))
        except ValueError:
            continue
    return found


def _scan_file(plant, name, path, aliases):
    info = {"plant": plant, "file": name, "header_row": None, "rows": 0, "mapping": {}, "mapping_basis": {},
            "unmapped_headers": [], "missing_required": [], "problems": [], "date_format": None, "date_ambiguous": [],
            "date_min": None, "date_max": None, "units": {}, "currencies": {}, "freight_terms": {},
            "item_codes": [], "descriptions": []}
    with _open_text(path, "utf-8-sig", newline="") as f:
        source = list(csv.reader(f))
    width = max((len(r) for r in source), default=0)
    source = [r + [""] * (width - len(r)) for r in source]
    best = None
    for i, row in enumerate(source[:30]):
        hits = sum(1 for v in row if _guess_field(v, aliases)[0])
        if hits >= 2 and (best is None or hits > best[1]):
            best = (i, hits)
    if best is None:
        info["problems"].append("Could not find a header row (fewer than 2 recognisable column names in the first 30 rows)")
        return info
    hdr = best[0]
    info["header_row"] = hdr + 1
    col = {}
    for i, header in enumerate(source[hdr]):
        header = str(header).strip()
        if not header:
            continue
        field, basis = _guess_field(header, aliases)
        if field is None:
            info["unmapped_headers"].append(header)
        elif field in col:
            info["problems"].append(f"Two columns look like '{field}': '{source[hdr][col[field]].strip()}' and '{header}'")
        else:
            col[field] = i
            info["mapping"][header], info["mapping_basis"][header] = field, basis
    info["missing_required"] = [f for f in DEFAULT_REQUIRED if f not in col]

    def cell(row, field):
        i = col.get(field)
        return row[i].strip() if i is not None and i < len(row) else ""

    units, currencies, freight, codes, descs, dates = {}, {}, {}, set(), {}, []
    for row in source[hdr + 1:]:
        vals = [v.strip() for v in row]
        if not any(vals):
            continue
        first = next((x for x in vals if x), "")
        if TOTAL_RE.search(first) and not cell(row, "quantity"):
            continue
        info["rows"] += 1
        for store, field in ((units, "purchase_unit"), (currencies, "currency"), (freight, "freight_terms")):
            v = cell(row, field)
            if v:
                key = v.upper() if field != "purchase_unit" else _norm_unit(v)
                entry = store.setdefault(key, {"text": v, "count": 0})
                entry["count"] += 1
        if cell(row, "item_code"):
            codes.add(cell(row, "item_code"))
        d = cell(row, "description")
        if d:
            descs.setdefault((cell(row, "item_code"), d), 0)
            descs[(cell(row, "item_code"), d)] += 1
        dates.append(cell(row, "date"))
    info["units"] = {k: v for k, v in sorted(units.items())}
    info["currencies"] = {k: v["count"] for k, v in sorted(currencies.items())}
    info["freight_terms"] = {k: v["count"] for k, v in sorted(freight.items())}
    info["item_codes"] = sorted(codes)[:40]
    info["descriptions"] = [{"item_code": c, "description": d, "lines": n} for (c, d), n in sorted(descs.items())][:60]
    cands = _date_candidates(dates)
    iso = [c for c in cands if c[0] in ISO_FORMATS]
    if "date" not in col or not cands:
        if "date" in col and any(dates):
            info["problems"].append("Dates could not be read with any common format")
    elif iso:
        info["date_format"] = None
        parsed = iso[0][1]
        info["date_min"], info["date_max"] = str(min(parsed)), str(max(parsed))
    elif len(cands) == 1:
        info["date_format"] = cands[0][0]
        info["date_min"], info["date_max"] = str(min(cands[0][1])), str(max(cands[0][1]))
    else:
        info["date_ambiguous"] = [c[0] for c in cands]
    return info


def _is_subsequence(short, word):
    it = iter(word)
    return all(ch in it for ch in short)


def _abbreviation_candidates(token_pool, known):
    """Short tokens that are an unambiguous shortening of exactly one longer token in the data."""
    out = {}
    for t in sorted(token_pool):
        if len(t) < 3 or len(t) > 5 or t.isdigit() or any(ch.isdigit() for ch in t) or t in known:
            continue
        longer = sorted(w for w in token_pool if w != t and len(w) > len(t) and w[0] == t[0] and _is_subsequence(t, w))
        if len(longer) == 1:
            out[t] = longer[0]
    return out


def _codey(tokens):
    return all(len(t) <= 2 or any(ch.isdigit() for ch in t) for t in tokens)


def _kept_apart(a, b):
    """True when two descriptions differ only in a short code (X vs Y, 40mm vs 50mm): never merge those."""
    only_a, only_b = a - b, b - a
    return bool(only_a) and bool(only_b) and _codey(only_a) and _codey(only_b)


def _cluster_descriptions(items, abbr, threshold):
    """Group distinct (plant, item_code, description) into candidate products. Proposals only."""
    clusters, kept_apart = [], []
    for it in items:
        toks = {abbr.get(t, t) for t in _norm(it["description"]).split()}
        it["_tokens"] = toks
        scored = []
        for ci, cl in enumerate(clusters):
            blocked = any(_kept_apart(toks, m["_tokens"]) and _score(toks, m["_tokens"]) >= threshold for m in cl)
            sims = [_score(toks, m["_tokens"]) for m in cl]
            if blocked:
                kept_apart.append({"description": it["description"], "plant": it["plant"], "kept_apart_from": cl[0]["description"],
                                   "why": "they differ only in a short code - treated as different specifications until a buyer says otherwise"})
                continue
            if max(sims) >= threshold:
                scored.append((max(sims), ci))
        scored.sort(reverse=True)
        if scored:
            if len(scored) > 1 and scored[0][0] - scored[1][0] <= 0.1:
                it["ambiguous"] = True
            clusters[scored[0][1]].append(it)
        else:
            clusters.append([it])
    return clusters, kept_apart


def propose():
    """Gate 0. Reads the staged plant files (and any rules/scope already present) and says what is missing."""
    try:
        return _propose()
    except (WorkflowError, OSError, KeyError, TypeError, ValueError) as exc:
        return {"status": "ERROR", "error": str(exc)}


def _propose(reporting_override=None):
    rules = _lenient_rules()
    input_dir = INPUT_DIR if (os.path.isdir(INPUT_DIR) and not os.path.islink(INPUT_DIR)) else os.curdir
    scope, scope_problem = None, None
    try:
        scope = _json(_find_scope(), "scope")
    except WorkflowError as e:
        if "not found" not in str(e):
            scope_problem = str(e)
    scope_names = {}
    if isinstance(scope, dict):
        for item in scope.get("files", []) or []:
            if isinstance(item, dict) and isinstance(item.get("file"), str):
                scope_names[item["file"]] = item
    aliases = {}
    columns = rules["columns"] if isinstance(rules["columns"], dict) else None
    if columns and isinstance(columns.get("aliases"), dict):
        for field, names in columns["aliases"].items():
            for n in names:
                aliases[_norm(n)] = field
    else:
        for field, names in DEFAULT_ALIASES.items():
            for n in names:
                aliases[_norm(n)] = field
    candidates = [(n, p) for n, p in _input_listing(input_dir)
                  if n.lower().endswith(".csv") and n not in NON_PLANT_CSV and not n.startswith(("decisions", "scope"))]
    needs, proposals = [], []

    def need(kind, ask, blocking=True, **extra):
        needs.append({"id": f"N{len(needs) + 1}", "kind": kind, "blocking": blocking, "ask": ask, **extra})

    def propose_item(kind, what, **extra):
        proposals.append({"id": f"P{len(proposals) + 1}", "kind": kind, "source": UNCONFIRMED, "what": what, **extra})

    if len(candidates) < 2:
        need("plant_files", "Attach at least two plant purchase CSV files (one per site) so prices can be compared.",
             found=[n for n, _ in candidates])
    scans = []
    for name, path in candidates:
        plant = scope_names.get(name, {}).get("plant") or _plant_name_guess(name)
        scans.append(_scan_file(plant, name, path, aliases))
    for problem in rules["problems"]:
        need("rules_problem", "The rules file could not be used and was ignored: " + problem + " Ask the person to fix or resend it.")
    for s in scans:
        if s["problems"]:
            need("file_problem", f"{s['plant']} ({s['file']}): " + "; ".join(s["problems"]), file=s["file"])
        if s["header_row"] and s["missing_required"]:
            need("column_mapping", f"{s['plant']} ({s['file']}): which column holds {s['missing_required']}?",
                 file=s["file"], unmapped_headers=s["unmapped_headers"], missing=s["missing_required"],
                 write_to='decisions.json > column_overrides > "' + s["plant"] + '"')
        if s["date_ambiguous"]:
            need("date_format", f"{s['plant']} ({s['file']}): dates fit more than one format {s['date_ambiguous']}. Which is it (day first or month first)?",
                 file=s["file"], options=s["date_ambiguous"], write_to="scope.json > files > date_format")
    # Reporting currency: scope if present, else the most common currency in the data (a proposal).
    totals = {}
    for s in scans:
        for cur, n in s["currencies"].items():
            totals[cur] = totals.get(cur, 0) + n
    reporting = None
    if isinstance(scope, dict) and scope.get("reporting_currency"):
        reporting = str(scope["reporting_currency"]).upper()
    elif reporting_override:
        reporting = str(reporting_override).strip().upper()
        propose_item("reporting_currency", f"Report prices in {reporting} (your answer)", value=reporting)
    elif totals:
        reporting = max(sorted(totals), key=lambda c: totals[c])
        propose_item("reporting_currency", f"Report prices in {reporting} (the most common currency in the files)", value=reporting)
    # Unit pack sizes: only a person can say how many labels are in a roll.
    unit_rows = rules["sections"].get("unit_conversions") or []
    have_units = {(_norm_unit(r["unit_alias"]), r["plant"].strip().lower() or "*") for r in unit_rows}
    seen_units = {}
    for s in scans:
        for key, v in s["units"].items():
            seen_units.setdefault(key, {"text": v["text"], "plants": {}})["plants"][s["plant"]] = v["count"]
    for key, v in sorted(seen_units.items()):
        covered = (key, "*") in have_units or all((key, p.lower()) in have_units for p in v["plants"])
        if covered:
            continue
        number = re.findall(r"\d+", v["text"])
        hint = f"the name contains {number[0]}; that may be the labels per unit - confirm" if len(number) == 1 else None
        need("unit_pack_size", f"How many labels are in one '{v['text']}'? Used by: " + ", ".join(f"{p} ({n} lines)" for p, n in sorted(v["plants"].items())) + ".",
             unit=v["text"], hint=hint,
             write_to="rules.json > unit_conversions",
             template_row={"rule_id": "UC-<next>", "unit_alias": v["text"], "plant": "*", "labels_per_unit": "<ask>", "note": "", "source": "user_confirmed"})
    # Exchange rates: never invented.
    fx_rows = rules["sections"].get("fx_rates") or []
    have_fx = {(r["currency"].upper(), r["reporting_currency"].upper()) for r in fx_rows}
    if reporting:
        for cur in sorted(totals):
            if cur != reporting and (cur, reporting) not in have_fx:
                need("fx_rate", f"What exchange rate should be used for {cur} to {reporting}, and for what date?", currency=cur,
                     write_to="rules.json > fx_rates",
                     template_row={"rule_id": "FX-<next>", "currency": cur, "rate_to_reporting": "<ask>", "reporting_currency": reporting, "as_of": "<ask>", "source_note": "", "source": "user_confirmed"})
    # Freight terms.
    groups = {}
    if isinstance(rules["settings"], dict) and isinstance(rules["settings"].get("freight_groups"), dict):
        groups = {k.strip().upper(): v for k, v in rules["settings"]["freight_groups"].items()}
    else:
        groups = dict(DEFAULT_SETTINGS["freight_groups"])
    seen_freight = {}
    for s in scans:
        for term, n in s["freight_terms"].items():
            seen_freight.setdefault(term, []).append(s["plant"])
    for term, plants in sorted(seen_freight.items()):
        if term not in groups:
            need("freight_term", f"Is freight term '{term}' (used by {', '.join(sorted(set(plants)))}) delivered or ex-works?", term=term,
                 write_to="rules.json > settings > freight_groups")
    # Abbreviations and candidate products.
    all_items = []
    for s in scans:
        for d in s["descriptions"]:
            all_items.append({"plant": s["plant"], "item_code": d["item_code"], "description": d["description"], "lines": d["lines"]})
    have_abbr = {r["short"].strip().lower(): r["long"].strip().lower() for r in (rules["sections"].get("abbreviations") or [])}
    pool = {t for it in all_items for t in _norm(it["description"]).split()}
    new_abbr = {k: v for k, v in _abbreviation_candidates(pool, set(have_abbr)).items() if k not in have_abbr}
    for short, long_ in sorted(new_abbr.items()):
        propose_item("abbreviation", f"'{short}' probably means '{long_}'", short=short, long=long_)
    abbr_all = dict(have_abbr)
    abbr_all.update(new_abbr)
    threshold = 0.6
    if isinstance(rules["settings"], dict):
        threshold = float(rules["settings"].get("matching", {}).get("propose_threshold", 0.6))
    specs_rules = rules["sections"].get("specs") or []
    kept_apart, clusters = [], []
    if not specs_rules and all_items:
        clusters, kept_apart = _cluster_descriptions([dict(i) for i in all_items], abbr_all, threshold)
        for n, cl in enumerate(clusters, 1):
            members = [{"plant": m["plant"], "item_code": m["item_code"], "description": m["description"], "lines": m["lines"]} for m in cl]
            plants = sorted({m["plant"] for m in cl})
            alone = len(plants) < 2
            propose_item("candidate_product", f"Candidate product SPEC-{n}: " + ("; ".join(f"{m['plant']}: '{m['description']}'" for m in members)),
                         spec_id=f"SPEC-{n}", spec_name=sorted((m["description"] for m in cl), key=len)[0], members=members,
                         note=("Only one plant buys this, so it cannot be compared until another plant's item is matched to it." if alone else "Confirm these are the same product."),
                         ambiguous=any(m.get("ambiguous") for m in cl))
        for k in kept_apart:
            propose_item("kept_apart", f"'{k['description']}' ({k['plant']}) is kept apart from '{k['kept_apart_from']}': {k['why']}")
    elif specs_rules and all_items:
        approved = {(r["plant"].strip().lower(), r["item_code"].strip().lower()) for r in (rules["sections"].get("item_map") or []) if r["status"].lower() == "approved" and r["item_code"]}
        approved_d = {(r["plant"].strip().lower(), _norm(r["description"])) for r in (rules["sections"].get("item_map") or []) if r["status"].lower() == "approved" and r["description"]}
        unmapped = [i for i in all_items if not ((i["plant"].lower(), i["item_code"].lower()) in approved or (i["plant"].lower(), _norm(i["description"])) in approved_d)]
        if unmapped:
            need("unmapped_items", f"{len(unmapped)} item(s) have no approved match to a product yet. They will land in Needs review / Gate 2 unless you map them now.",
                 blocking=False, items=[{"plant": i["plant"], "item_code": i["item_code"], "description": i["description"]} for i in unmapped[:25]])
    # Missing rule sections the engine needs.
    if not columns:
        propose_item("columns", "Use the column mapping found in the files", write_to="rules.json > columns",
                     value={"required": DEFAULT_REQUIRED, "optional": DEFAULT_OPTIONAL, "aliases": _alias_proposal(scans)})
    if not isinstance(rules["settings"], dict):
        propose_item("settings", "Use standard settings: price per 1,000 labels, orders under 100,000 labels count as 'small', standard Incoterm groups, at least 2 plants per comparison",
                     write_to="rules.json > settings", value=DEFAULT_SETTINGS)
    # Scope proposal.
    mins = [s["date_min"] for s in scans if s["date_min"]]
    maxs = [s["date_max"] for s in scans if s["date_max"]]
    scope_proposal = None
    if not isinstance(scope, dict):
        files = []
        for s in scans:
            entry = {"plant": s["plant"], "file": s["file"]}
            if s["date_format"]:
                entry["date_format"] = s["date_format"]
            files.append(entry)
        scope_proposal = {"project": "<name>", "category": "<ask>", "reporting_currency": reporting or "<ask>",
                          "window": {"start": min(mins) if mins else "<ask>", "end": max(maxs) if maxs else "<ask>"}, "files": files}
        propose_item("scope", "Compare these plants and files over the date range found in them (a person should confirm the window and category)",
                     write_to="scope.json", value=scope_proposal)
        need("category", "What product category is this (for example packaging-labels)?", blocking=False, write_to="scope.json > category")
    elif scope_problem:
        need("scope_problem", scope_problem)
    # Provenance and an item_map patch for the candidate products.
    patch = {}
    if clusters:
        specs, item_map = [], []
        for n, cl in enumerate(clusters, 1):
            specs.append({"spec_id": f"SPEC-{n}", "spec_name": sorted((m["description"] for m in cl), key=len)[0], "source": UNCONFIRMED})
            for m in cl:
                item_map.append({"plant": m["plant"], "item_code": m["item_code"], "description": m["description"], "spec_id": f"SPEC-{n}",
                                 "status": "proposed", "approved_by": "", "source": UNCONFIRMED})
        patch.update(specs=specs, item_map=item_map, spec_equivalences=[])
    if new_abbr:
        patch["abbreviations"] = [{"short": k, "long": v, "source": UNCONFIRMED} for k, v in sorted(new_abbr.items())]
    blocking = [n for n in needs if n["blocking"]]
    status = "GATE_0_NEEDS_INPUT" if blocking else ("GATE_0_CONFIRM_PROPOSALS" if (proposals or needs) else "GATE_0_READY")
    out = {"skill": "cross-site-purchase-comparison", "version": VERSION, "mode": "propose", "status": status,
           "wrote_files": False, "plain_summary": f"{len(blocking)} question(s) only a person can answer, {len(proposals)} proposal(s) to confirm.",
           "found": {"plant_files": [s["file"] for s in scans], "rules_files": rules["files"], "rules_sections": sorted(rules["sections"]),
                     "scope_present": isinstance(scope, dict), "rules_problems": rules["problems"]},
           "scan": [{k: v for k, v in s.items() if k not in ("descriptions", "item_codes")} | {"distinct_descriptions": len(s["descriptions"])} for s in scans],
           "needs": needs, "proposals": proposals}
    if patch:
        out["rules_patch"] = patch
    out["next_step"] = ("Prefer --mode setup: it returns the same questions plus a setup_template. Record the person's answers in setup.json "
                        "and the script builds rules.json and scope.json, tagging every answered rule user_confirmed.")
    return out


# ---------------------------------------------------------------------------------------------
# Gate 0 setup: one file of answers (setup.json) in, finished rules.json + scope.json out.
# The agent only records what the person said in setup.json. This code turns the answers into the
# rules pack and scope, tags every answered or confirmed rule "user_confirmed", and lists exactly
# what is still open. Nothing here ever invents a pack size, an exchange rate or a match approval.
# ---------------------------------------------------------------------------------------------
SETUP_NAME = "setup.json"
ASK = "<ask>"
_SETUP_KEYS = {"_doc", "confirmed_by", "category", "project", "reporting_currency", "window", "date_formats",
               "unit_pack_sizes", "fx_rates", "freight_terms", "confirm", "products", "approval"}
_SKIP_WORDS = {"unknown", "skip", "leave out", "leave_out", "none", "n/a", "dont know", "don't know"}


def _setup_answers():
    path = _find_setup()
    if not path:
        return {}
    data = _json(path, "setup")
    if not isinstance(data, dict):
        raise WorkflowError("setup.json must be a JSON object")
    bad = sorted(set(data) - _SETUP_KEYS)
    if bad:
        raise WorkflowError(f"setup.json has unrecognised key(s) {bad}. Allowed: {sorted(_SETUP_KEYS - {'_doc'})}. "
                            "Run --mode setup and copy setup_template.")
    return data


def _answered(v):
    return v is not None and not (isinstance(v, str) and v.strip() in ("", ASK))


def _positive_number(v):
    try:
        d = Decimal(str(v).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 else None


def _num_text(d):
    return format(d.normalize(), "f")


def _iso(v):
    try:
        return str(dt.date.fromisoformat(str(v).strip()))
    except ValueError:
        return None


def _build_setup():
    ans = _setup_answers()
    reporting_ans = str(ans["reporting_currency"]).strip().upper() if _answered(ans.get("reporting_currency")) else None
    base = _propose(reporting_ans)
    if base.get("status") == "ERROR":
        raise WorkflowError(base["error"])
    rules = _lenient_rules()
    questions, recorded, blockers = [], [], []
    tpl = {"_doc": "Answers for Gate 0. Replace each \"<ask>\" with the person's answer. For a pack size the person does not know, "
                   "write null: that unit then stays out of the comparison and its lines go to Needs review."}

    def ask(topic, text, key, **extra):
        questions.append({"id": f"Q{len(questions) + 1}", "topic": topic, "ask": text, "answer_key": key, **extra})

    who = str(ans["confirmed_by"]).strip() if _answered(ans.get("confirmed_by")) else None
    tpl["confirmed_by"] = who or ASK
    if not who:
        ask("who", "What role should be recorded as having confirmed these answers (for example 'Procurement consultant')?", "confirmed_by")
    by = who or "unnamed"
    confirm_in = ans.get("confirm") if isinstance(ans.get("confirm"), dict) else {}
    confirm_out = {}

    def confirmed(name, active):
        if not active:
            return True
        ok = confirm_in.get(name) is True
        confirm_out[name] = True if ok else ASK
        return ok

    for need in base["needs"]:
        if need["kind"] in ("plant_files", "file_problem", "rules_problem", "scope_problem"):
            blockers.append(need["ask"])
            ask(need["kind"], need["ask"], "(fix the files, then rerun)")

    # ---- scope -------------------------------------------------------------------------------
    scope_prop = next((p["value"] for p in base["proposals"] if p["kind"] == "scope"), None)
    if base["found"]["scope_present"]:
        scope = _json(_find_scope(), "scope")
        recorded.append("scope: used the attached scope file")
    else:
        scope = json.loads(json.dumps(scope_prop)) if scope_prop else {"files": []}
        scan_lo, scan_hi = (scope_prop or {}).get("window", {}).get("start"), (scope_prop or {}).get("window", {}).get("end")
        if _answered(ans.get("category")):
            scope["category"] = str(ans["category"]).strip()
            recorded.append(f"category: {scope['category']}")
        else:
            ask("category", "What product category is this (for example packaging-labels)?", "category")
        tpl["category"] = scope["category"] if _answered(ans.get("category")) else ASK
        scope["project"] = str(ans["project"]).strip() if _answered(ans.get("project")) else "Cross-site purchase comparison"
        tpl["project"] = scope["project"]
        guess = (scope_prop or {}).get("reporting_currency")
        if reporting_ans:
            scope["reporting_currency"] = reporting_ans
            recorded.append(f"reporting currency: {reporting_ans}")
        else:
            ask("reporting_currency", f"Which currency should prices be reported in? The files mostly use {guess}.", "reporting_currency", suggest=guess)
        tpl["reporting_currency"] = reporting_ans or ASK
        win = ans.get("window") if isinstance(ans.get("window"), dict) else {}
        w0, w1 = (_iso(win.get("start")) if _answered(win.get("start")) else None), (_iso(win.get("end")) if _answered(win.get("end")) else None)
        if w0 and w1 and w0 <= w1:
            scope["window"] = {"start": w0, "end": w1}
            recorded.append(f"window: {w0} to {w1}")
        else:
            ask("window", f"Which purchase dates should be compared (start and end, YYYY-MM-DD)? The files hold purchases from {scan_lo} to {scan_hi}. "
                          "Use the client's agreed window, not just the range in the data.", "window", suggest={"start": scan_lo, "end": scan_hi})
        tpl["window"] = {"start": w0 or ASK, "end": w1 or ASK}
        if not (w0 and w1):
            scope["window"] = {"start": scan_lo, "end": scan_hi}
        fmts = ans.get("date_formats") if isinstance(ans.get("date_formats"), dict) else {}
        tpl_fmts = {}
        for need in base["needs"]:
            if need["kind"] != "date_format":
                continue
            plant = need["ask"].split(" (", 1)[0]
            chosen = fmts.get(plant)
            tpl_fmts[plant] = chosen if _answered(chosen) else ASK
            if _answered(chosen):
                for entry in scope["files"]:
                    if entry["plant"] == plant:
                        entry["date_format"] = str(chosen)
                recorded.append(f"{plant} date format: {chosen}")
            else:
                ask("date_format", need["ask"], f"date_formats.{plant}", options=need.get("options"))
        if tpl_fmts:
            tpl["date_formats"] = tpl_fmts
        if not confirmed("scope", True):
            ask("confirm_scope", "These plants and files will be compared: " + "; ".join(f"{e['plant']} ({e['file']})" for e in scope["files"]) + ". Is that right?", "confirm.scope")
        else:
            recorded.append("plants and files: confirmed")

    # ---- rules -------------------------------------------------------------------------------
    tables = {k: [dict(r) for r in rules["sections"].get(k, [])] for k in RULE_TABLES}
    patch = base.get("rules_patch", {})
    # Pack sizes
    ups = ans.get("unit_pack_sizes") if isinstance(ans.get("unit_pack_sizes"), dict) else {}
    ups_n = {_norm_unit(k): (k, v) for k, v in ups.items()}
    tpl_units = {}
    for need in [n for n in base["needs"] if n["kind"] == "unit_pack_size"]:
        unit = need["unit"]
        hit = ups_n.get(_norm_unit(unit))
        if hit is None or (_answered(hit[1]) is False and hit[1] is not None):
            tpl_units[unit] = ASK
            ask("unit_pack_size", need["ask"], f"unit_pack_sizes.{unit}", unit=unit, hint=need.get("hint"))
            continue
        value = hit[1]
        tpl_units[unit] = value
        if value is None or (isinstance(value, str) and value.strip().lower() in _SKIP_WORDS):
            tpl_units[unit] = None
            recorded.append(f"unit '{unit}': pack size not known - left out, its lines go to Needs review")
            continue
        number = _positive_number(value)
        if number is None:
            tpl_units[unit] = ASK
            ask("unit_pack_size", f"'{value}' is not a positive number. How many labels are in one '{unit}'?", f"unit_pack_sizes.{unit}", unit=unit)
            continue
        tables["unit_conversions"].append({"rule_id": f"UC-{len(tables['unit_conversions']) + 1:03d}", "unit_alias": unit, "plant": "*",
                                           "labels_per_unit": _num_text(number), "note": f"Confirmed by {by}", "source": "user_confirmed"})
        recorded.append(f"unit '{unit}': {_num_text(number)} labels")
    if tpl_units:
        tpl["unit_pack_sizes"] = tpl_units
    # Exchange rates
    fxs = ans.get("fx_rates") if isinstance(ans.get("fx_rates"), dict) else {}
    tpl_fx = {}
    reporting_final = (scope.get("reporting_currency") if isinstance(scope, dict) else None) or reporting_ans
    for need in [n for n in base["needs"] if n["kind"] == "fx_rate"]:
        cur = need["currency"]
        a = fxs.get(cur) if isinstance(fxs.get(cur), dict) else {}
        rate = _positive_number(a.get("rate")) if _answered(a.get("rate")) else None
        as_of = _iso(a.get("as_of")) if _answered(a.get("as_of")) else None
        tpl_fx[cur] = {"rate": _num_text(rate) if rate else ASK, "as_of": as_of or ASK, "note": a.get("note", "")}
        if rate and as_of and reporting_final:
            tables["fx_rates"].append({"rule_id": f"FX-{len(tables['fx_rates']) + 1:03d}", "currency": cur, "rate_to_reporting": _num_text(rate),
                                       "reporting_currency": reporting_final, "as_of": as_of, "source_note": a.get("note") or f"Confirmed by {by}", "source": "user_confirmed"})
            recorded.append(f"exchange rate {cur} to {reporting_final}: {_num_text(rate)} as of {as_of}")
        else:
            ask("fx_rate", need["ask"] + " Give the rate, its date (YYYY-MM-DD) and where it comes from.", f"fx_rates.{cur}", currency=cur)
    if tpl_fx:
        tpl["fx_rates"] = tpl_fx
    # Freight terms and settings
    settings = json.loads(json.dumps(rules["settings"])) if isinstance(rules["settings"], dict) else None
    settings_new = settings is None
    if settings_new:
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    fr_in = ans.get("freight_terms") if isinstance(ans.get("freight_terms"), dict) else {}
    tpl_fr = {}
    for need in [n for n in base["needs"] if n["kind"] == "freight_term"]:
        term = need["term"]
        v = str(fr_in.get(term, "")).strip().lower()
        if v in ("delivered", "ex-works"):
            settings.setdefault("freight_groups", {})[term] = v
            tpl_fr[term] = v
            recorded.append(f"freight term {term}: {v}")
        else:
            tpl_fr[term] = ASK
            ask("freight_term", need["ask"] + " Answer 'delivered' or 'ex-works'.", f"freight_terms.{term}", term=term)
    if tpl_fr:
        tpl["freight_terms"] = tpl_fr
    if settings_new:
        if confirmed("settings", True):
            settings["source"] = "user_confirmed"
            recorded.append("settings (price per 1,000 labels, order-size bands, freight groups): confirmed")
        else:
            settings["source"] = UNCONFIRMED
            ask("confirm_settings", "Use the standard settings: price per 1,000 labels, orders under 100,000 labels count as 'small', standard Incoterm groups, at least 2 plants per comparison. OK?", "confirm.settings")
    # Columns
    columns = rules["columns"] if isinstance(rules["columns"], dict) else None
    if columns is None:
        col_prop = next((p["value"] for p in base["proposals"] if p["kind"] == "columns"), None)
        columns = json.loads(json.dumps(col_prop)) if col_prop else {"required": DEFAULT_REQUIRED, "optional": DEFAULT_OPTIONAL, "aliases": {}}
        pairs = "; ".join(f"{s['plant']}: " + ", ".join(f"'{h}' = {f}" for h, f in s["mapping"].items()) for s in base["scan"])
        if confirmed("columns", True):
            columns["source"] = "user_confirmed"
            recorded.append("column mapping: confirmed")
        else:
            columns["source"] = UNCONFIRMED
            ask("confirm_columns", f"The column headers map like this: {pairs}. Is that right?", "confirm.columns")
    # Abbreviations
    new_abbr = patch.get("abbreviations", [])
    if new_abbr:
        if confirmed("abbreviations", True):
            for r in new_abbr:
                tables["abbreviations"].append({"short": r["short"], "long": r["long"], "source": "user_confirmed"})
            recorded.append("abbreviations: " + ", ".join(f"{r['short']} = {r['long']}" for r in new_abbr))
        else:
            ask("confirm_abbreviations", "These abbreviations look right: " + ", ".join(f"'{r['short']}' = '{r['long']}'" for r in new_abbr) + ". OK?", "confirm.abbreviations")
    # Candidate products
    if patch.get("specs"):
        prod = ans.get("products") if isinstance(ans.get("products"), dict) else {}
        approve = [str(x) for x in (prod.get("approve") or []) if _answered(x)]
        leave = [str(x) for x in (prod.get("leave_out") or []) if _answered(x)]
        hold = {(str(h.get("plant", "")).strip().lower(), _norm(h.get("description", ""))) for h in (prod.get("hold_for_gate2") or []) if isinstance(h, dict)}
        spec_ids = [s["spec_id"] for s in patch["specs"]]
        unknown = [x for x in approve + leave if x not in spec_ids]
        if unknown:
            ask("products", f"Unknown product id(s) {unknown}; the candidates are {spec_ids}.", "products")
        open_ids = [s for s in spec_ids if s not in approve and s not in leave]
        tpl["products"] = {"approve": approve, "leave_out": leave, "hold_for_gate2": [dict(plant=h["plant"], description=h["description"]) for h in (prod.get("hold_for_gate2") or []) if isinstance(h, dict)]}
        if open_ids:
            members = {}
            for r in patch["item_map"]:
                members.setdefault(r["spec_id"], []).append(f"{r['plant']}: '{r['description']}'")
            lines = " | ".join(f"{sid} = " + "; ".join(members.get(sid, [])) for sid in open_ids)
            ask("products", "Which of these candidate products are really the same item across plants (approve), and which should be left out? "
                            "Name any single item you want a buyer to review at Gate 2 (hold). Candidates: " + lines, "products",
                open_products=open_ids)
        spec_name = {s["spec_id"]: s["spec_name"] for s in patch["specs"]}
        for sid in spec_ids:
            if sid in leave:
                recorded.append(f"product {sid}: left out")
                continue
            if sid in approve:
                tables["specs"].append({"spec_id": sid, "spec_name": spec_name[sid], "source": "user_confirmed"})
                recorded.append(f"product {sid} ({spec_name[sid]}): approved by {by}")
            else:
                tables["specs"].append({"spec_id": sid, "spec_name": spec_name[sid], "source": UNCONFIRMED})
            for r in [x for x in patch["item_map"] if x["spec_id"] == sid]:
                key = (r["plant"].strip().lower(), _norm(r["description"]))
                row = dict(r)
                if sid in approve and key not in hold:
                    row.update(status="approved", approved_by=by, source="user_confirmed")
                elif sid in approve:
                    row.update(status="proposed", approved_by="", source=UNCONFIRMED)
                    recorded.append(f"'{r['description']}' ({r['plant']}): held for the buyer at Gate 2")
                tables["item_map"].append(row)
    if confirm_out:
        tpl["confirm"] = confirm_out
    pack = {"columns": columns, "settings": settings, **tables}
    return {"questions": questions, "recorded": recorded, "blockers": blockers, "pack": pack, "scope": scope,
            "template": tpl, "base": base}


def setup():
    """Gate 0, guided. Reads setup.json (if any) and the plant files; returns what is open, the template to fill, and finished files."""
    try:
        built = _build_setup()
    except (WorkflowError, OSError, KeyError, TypeError, ValueError) as exc:
        return {"status": "ERROR", "error": str(exc)}
    qs = built["questions"]
    out = {"skill": "cross-site-purchase-comparison", "version": VERSION, "mode": "setup",
           "status": "SETUP_NEEDS_INPUT" if qs else "SETUP_READY",
           "plain_summary": (f"{len(qs)} question(s) still open." if qs else "Everything is answered. rules.json and scope.json are built and tagged user_confirmed."),
           "questions": qs, "recorded": built["recorded"], "setup_template": built["template"], "wrote_files": []}
    plants = _plant_card(built["base"]["scan"])
    out["case_card_text"] = _case_text(plants, "open", "", "", built["recorded"], len(qs), None)
    if qs:
        out["next_step"] = ("Ask ALL open questions in one message, in plain language. When the person answers, put every answer into setup.json "
                            "(start from setup_template, write the whole file once) and run --mode setup once more.")
        out["found"] = built["base"]["found"]
        out["scan"] = built["base"]["scan"]
    else:
        out["files"] = {"rules.json": built["pack"], "scope.json": built["scope"]}
        written = []
        if _VFS is None:
            os.makedirs(OUT_DIR, exist_ok=True)
        for name, content in (("rules.json", built["pack"]), ("scope.json", built["scope"])):
            _emit(os.path.join(OUT_DIR, name), json.dumps(content, indent=2, ensure_ascii=False) + "\n")
            written.append(f"{OUT_DIR}/{name}")
        out["wrote_files"] = written
        tok = _approval_token(built["pack"], built["scope"], _scope_plant_hashes(built["scope"]))
        out["approval_template"] = {"approved": True, "by": built["template"].get("confirmed_by", ""), **tok}
        out["case_card_text"] = _case_text(plants, "built, waiting for the person's approval", tok["rules_sha256"][:12], tok["scope_sha256"][:12], built["recorded"], 0, None)
        out["next_step"] = ("Print rules.json and scope.json in full in the chat, then ask: 'Do you approve these as the official rules and scope?'. "
                            "On yes, copy approval_template into setup.json under the key 'approval' (change nothing in it), read setup.json back, and run --mode run once in the same turn. "
                            "Gate 0 approval also counts as the Gate 1 scope confirmation. Do not ask the questions again.")
    return out


def _alias_proposal(scans):
    aliases = {f: [] for f in DEFAULT_REQUIRED + DEFAULT_OPTIONAL}
    for s in scans:
        for header, field in s["mapping"].items():
            n = _norm(header)
            if n and n not in aliases[field]:
                aliases[field].append(n)
    return aliases


# BEGIN DEMO DATA
# Generated by tools/embed_demo.py from examples/synthetic/. Synthetic data only; edit the examples, then re-run the tool.
DEMO_COMMON = {'inputs_csv/plant_A_purchases.csv': 'PO Date,Material,Material Description,Order Qty,UoM,Net '
                                     'Price,Curr,Vendor,Incoterm,Net Value\r\n'
                                     '2025-09-15,A-1001,"Product label, white, specification '
                                     'X",500,ROLL-500,35,USD,Vendor 1,DAP,17500\r\n'
                                     '2025-12-05,A-1001,"Product label, white, specification '
                                     'X",1000,ROLL-500,35,USD,Vendor 1,DAP,35000\r\n'
                                     '2026-03-04,A-1001,"Product label, white, specification '
                                     'X",1000,ROLL-500,35,USD,Vendor 1,DAP,35000\r\n'
                                     '2026-06-10,A-1001,"Product label, white, specification '
                                     'X",1000,ROLL-500,35,USD,Vendor 1,DAP,35000\r\n'
                                     '2026-09-09,A-1001,"Product label, white, specification '
                                     'X",1000,ROLL-500,35,USD,Vendor 1,DAP,35000\r\n'
                                     'Total,,,,,,,,,157500\r\n',
 'inputs_csv/plant_B_export.csv': 'Date,Item No,Item Text,Quantity,Unit,Unit Price,Currency,Supplier '
                                  'Name,Delivery Terms,Line Total\r\n'
                                  '2025-11-20,880021,"White label, specification '
                                  'X",500,ROLL-1000,60,USD,Vendor 2,DAP,30000\r\n'
                                  '2026-01-12,880021,"White label, specification X",,ROLL-1000,60,USD,Vendor '
                                  '2,DAP,\r\n'
                                  '2026-02-18,880021,"White label, specification '
                                  'X",500,ROLL-1000,60,USD,Vendor 2,DAP,30000\r\n'
                                  '2026-04-01,880021,"White label, specification '
                                  'X",10,ROLL-1000,75,USD,Vendor 2,DAP,750\r\n'
                                  '2026-05-20,880021,"White label, specification '
                                  'X",500,ROLL-1000,60,USD,Vendor 2,DAP,30000\r\n'
                                  '2026-07-07,880021,"White label, specification '
                                  'X",300,ROLL-1000,52,USD,Vendor 2,EXW,15600\r\n'
                                  '2026-08-19,880021,"White label, specification '
                                  'X",500,ROLL-1000,60,USD,Vendor 2,DAP,30000\r\n'
                                  'Grand Total,,,,,,,,,136350\r\n',
 'inputs_csv/plant_C_report.csv': 'Plant C - Packaging purchases,,,,,,,,,\r\n'
                                  'Exported 2026-11-02,,,,,,,,,\r\n'
                                  ',,,,,,,,,\r\n'
                                  'Posting Date,Mat Code,Mat '
                                  'Desc,Qty,OUn,Price,Crcy,Vendor,Inco-terms,Value\r\n'
                                  '2026-01-15,C-77,"Label, specification Y",800,RL (500),40,USD,Vendor '
                                  '3,DAP,32000\r\n'
                                  '2026-05-05,C-77,"Label, specification Y",100,ROLL,41,USD,Vendor '
                                  '3,DAP,4100\r\n'
                                  'Subtotal H1,,,,,,,,,36100\r\n'
                                  '2026-07-15,C-77,"Label, specification Y",800,RL (500),40,USD,Vendor '
                                  '3,DAP,32000\r\n'
                                  'Total,,,,,,,,,68100\r\n',
 'inputs_csv/plant_D_extract.csv': 'Doc Date,Material,Short Text,Order Qty (M),OUn,Price per '
                                   'M,Cur.,Supplier,IncoT,Net Value\r\n'
                                   '15.01.2026,,Lbl wht spec X,400,M,58,EUR,Supplier 4,DAP,23200\r\n'
                                   '03.03.2026,,Etikett weiss,100,M,50,EUR,Supplier 4,DAP,5000\r\n'
                                   '20.06.2026,,Lbl wht spec X,400,M,58,EUR,Supplier 4,DAP,23200\r\n'
                                   'Total,,,,,,,,,51400\r\n',
 'rules.json': '{\n'
               '  "_doc": "Single-file rules pack: the eight rules/ files combined. Attach this instead of '
               'the rules/ folder.",\n'
               '  "columns": {\n'
               '    "_doc": "Maps each plant\'s column headers to the standard fields. Headers are compared '
               'after lower-casing and replacing punctuation with spaces. Exact match only. Anything not '
               'listed must be mapped by a person in decisions.json > column_overrides at Gate 1. Header '
               'aliases do not define purchase-unit meaning; unit conversions are determined only by '
               'unit_conversions.csv.",\n'
               '    "required": [\n'
               '      "date",\n'
               '      "description",\n'
               '      "quantity",\n'
               '      "purchase_unit",\n'
               '      "unit_price",\n'
               '      "currency"\n'
               '    ],\n'
               '    "optional": [\n'
               '      "item_code",\n'
               '      "supplier",\n'
               '      "freight_terms",\n'
               '      "line_amount"\n'
               '    ],\n'
               '    "aliases": {\n'
               '      "date": [\n'
               '        "po date",\n'
               '        "date",\n'
               '        "posting date",\n'
               '        "order date",\n'
               '        "doc date"\n'
               '      ],\n'
               '      "item_code": [\n'
               '        "material",\n'
               '        "item no",\n'
               '        "mat code",\n'
               '        "item code",\n'
               '        "material number",\n'
               '        "mat no"\n'
               '      ],\n'
               '      "description": [\n'
               '        "material description",\n'
               '        "item text",\n'
               '        "mat desc",\n'
               '        "description",\n'
               '        "short text",\n'
               '        "item description"\n'
               '      ],\n'
               '      "quantity": [\n'
               '        "order qty",\n'
               '        "order qty m",\n'
               '        "quantity",\n'
               '        "qty",\n'
               '        "po quantity"\n'
               '      ],\n'
               '      "purchase_unit": [\n'
               '        "uom",\n'
               '        "unit",\n'
               '        "oun",\n'
               '        "order unit",\n'
               '        "purchase unit"\n'
               '      ],\n'
               '      "unit_price": [\n'
               '        "net price",\n'
               '        "unit price",\n'
               '        "price",\n'
               '        "price per m"\n'
               '      ],\n'
               '      "currency": [\n'
               '        "curr",\n'
               '        "currency",\n'
               '        "crcy",\n'
               '        "cur"\n'
               '      ],\n'
               '      "supplier": [\n'
               '        "vendor",\n'
               '        "supplier name",\n'
               '        "supplier",\n'
               '        "vendor name"\n'
               '      ],\n'
               '      "freight_terms": [\n'
               '        "incoterm",\n'
               '        "delivery terms",\n'
               '        "inco terms",\n'
               '        "incoterms",\n'
               '        "incot"\n'
               '      ],\n'
               '      "line_amount": [\n'
               '        "net value",\n'
               '        "line total",\n'
               '        "value",\n'
               '        "amount"\n'
               '      ]\n'
               '    }\n'
               '  },\n'
               '  "settings": {\n'
               '    "_doc": "Comparison conditions. Purchases are compared only inside the same cell of '
               '(specification, order-size band, freight class). Bands use labels per order line.",\n'
               '    "price_basis_labels": 1000,\n'
               '    "matching": {\n'
               '      "propose_threshold": 0.6\n'
               '    },\n'
               '    "volume_bands": [\n'
               '      {\n'
               '        "name": "small",\n'
               '        "below_labels_per_order": 100000\n'
               '      },\n'
               '      {\n'
               '        "name": "standard",\n'
               '        "below_labels_per_order": null\n'
               '      }\n'
               '    ],\n'
               '    "freight_groups": {\n'
               '      "DAP": "delivered",\n'
               '      "DDP": "delivered",\n'
               '      "CPT": "delivered",\n'
               '      "CIP": "delivered",\n'
               '      "EXW": "ex-works",\n'
               '      "FCA": "ex-works",\n'
               '      "FOB": "ex-works"\n'
               '    },\n'
               '    "min_plants_per_group": 2,\n'
               '    "amount_tolerance": 0.01\n'
               '  },\n'
               '  "specs": [\n'
               '    {\n'
               '      "spec_id": "SPEC-X",\n'
               '      "spec_name": "White label, specification X"\n'
               '    },\n'
               '    {\n'
               '      "spec_id": "SPEC-Y",\n'
               '      "spec_name": "Label, specification Y"\n'
               '    }\n'
               '  ],\n'
               '  "item_map": [\n'
               '    {\n'
               '      "plant": "Plant A",\n'
               '      "item_code": "A-1001",\n'
               '      "description": "Product label, white, specification X",\n'
               '      "spec_id": "SPEC-X",\n'
               '      "status": "approved",\n'
               '      "approved_by": "Category buyer (role)"\n'
               '    },\n'
               '    {\n'
               '      "plant": "Plant B",\n'
               '      "item_code": "880021",\n'
               '      "description": "White label, specification X",\n'
               '      "spec_id": "SPEC-X",\n'
               '      "status": "approved",\n'
               '      "approved_by": "Category buyer (role)"\n'
               '    },\n'
               '    {\n'
               '      "plant": "Plant C",\n'
               '      "item_code": "C-77",\n'
               '      "description": "Label, specification Y",\n'
               '      "spec_id": "SPEC-Y",\n'
               '      "status": "approved",\n'
               '      "approved_by": "Category buyer (role)"\n'
               '    }\n'
               '  ],\n'
               '  "spec_equivalences": [],\n'
               '  "abbreviations": [\n'
               '    {\n'
               '      "short": "lbl",\n'
               '      "long": "label"\n'
               '    },\n'
               '    {\n'
               '      "short": "wht",\n'
               '      "long": "white"\n'
               '    },\n'
               '    {\n'
               '      "short": "spec",\n'
               '      "long": "specification"\n'
               '    },\n'
               '    {\n'
               '      "short": "blk",\n'
               '      "long": "black"\n'
               '    },\n'
               '    {\n'
               '      "short": "clr",\n'
               '      "long": "clear"\n'
               '    }\n'
               '  ],\n'
               '  "unit_conversions": [\n'
               '    {\n'
               '      "rule_id": "UC-001",\n'
               '      "unit_alias": "ROLL-500",\n'
               '      "plant": "*",\n'
               '      "labels_per_unit": "500",\n'
               '      "note": "Roll of 500 labels"\n'
               '    },\n'
               '    {\n'
               '      "rule_id": "UC-002",\n'
               '      "unit_alias": "ROLL-1000",\n'
               '      "plant": "*",\n'
               '      "labels_per_unit": "1000",\n'
               '      "note": "Roll of 1000 labels"\n'
               '    },\n'
               '    {\n'
               '      "rule_id": "UC-003",\n'
               '      "unit_alias": "RL (500)",\n'
               '      "plant": "Plant C",\n'
               '      "labels_per_unit": "500",\n'
               '      "note": "Plant C writes the roll size in brackets"\n'
               '    },\n'
               '    {\n'
               '      "rule_id": "UC-004",\n'
               '      "unit_alias": "M",\n'
               '      "plant": "*",\n'
               '      "labels_per_unit": "1000",\n'
               '      "note": "M = one thousand labels; quantity and price are both per thousand"\n'
               '    }\n'
               '  ],\n'
               '  "fx_rates": [\n'
               '    {\n'
               '      "rule_id": "FX-001",\n'
               '      "currency": "EUR",\n'
               '      "rate_to_reporting": "1.08",\n'
               '      "reporting_currency": "USD",\n'
               '      "as_of": "2026-10-31",\n'
               '      "source_note": "Illustrative synthetic rate - replace with the client\'s approved '
               'rate"\n'
               '    }\n'
               '  ]\n'
               '}\n',
 'scope_csv.json': '{\n'
                   '  "project": "SYNTHETIC-DEMO (no client data)",\n'
                   '  "category": "packaging-labels",\n'
                   '  "reporting_currency": "USD",\n'
                   '  "window": {\n'
                   '    "start": "2025-11-01",\n'
                   '    "end": "2026-10-31"\n'
                   '  },\n'
                   '  "files": [\n'
                   '    {\n'
                   '      "plant": "Plant A",\n'
                   '      "file": "plant_A_purchases.csv"\n'
                   '    },\n'
                   '    {\n'
                   '      "plant": "Plant B",\n'
                   '      "file": "plant_B_export.csv"\n'
                   '    },\n'
                   '    {\n'
                   '      "plant": "Plant C",\n'
                   '      "file": "plant_C_report.csv"\n'
                   '    },\n'
                   '    {\n'
                   '      "plant": "Plant D",\n'
                   '      "file": "plant_D_extract.csv",\n'
                   '      "date_format": "%d.%m.%Y"\n'
                   '    }\n'
                   '  ]\n'
                   '}'}
DEMO_DECISIONS = {'0_start': '{\n'
            '  "_doc": "SYNTHETIC. Nothing decided yet. The first run stops at Gate 1 and lists what needs '
            'confirming."\n'
            '}\n',
 '1_scope_confirmed': '{\n'
                      '  "_doc": "SYNTHETIC. Gate 1 done: scope confirmed and Plant D\'s two unknown headers '
                      'mapped by a person. Gate 2 (uncertain matches) and Gate 3 (sign-off) still open, so '
                      'the run produces a DRAFT.",\n'
                      '  "gate1": { "confirmed": true, "by": "Procurement consultant (role)" },\n'
                      '  "column_overrides": {\n'
                      '    "Plant D": { "Order Qty (M)": "quantity", "Price per M": "unit_price" }\n'
                      '  }\n'
                      '}\n',
 '2_signed_off': '{\n'
                 '  "_doc": "SYNTHETIC. Gates 1-3 done. The category buyer approved Plant D\'s abbreviated '
                 'description as specification X. The procurement lead signed off.",\n'
                 '  "gate1": { "confirmed": true, "by": "Procurement consultant (role)" },\n'
                 '  "column_overrides": {\n'
                 '    "Plant D": { "Order Qty (M)": "quantity", "Price per M": "unit_price" }\n'
                 '  },\n'
                 '  "match_decisions": [\n'
                 '    {\n'
                 '      "plant": "Plant D",\n'
                 '      "description": "Lbl wht spec X",\n'
                 '      "decision": "approve",\n'
                 '      "spec_id": "SPEC-X",\n'
                 '      "by": "Category buyer (role)",\n'
                 '      "reason": "Confirmed with the plant buyer: same white label, specification X"\n'
                 '    }\n'
                 '  ],\n'
                 '  "gate3": { "approved": true, "by": "Procurement lead (role)" }\n'
                 '}\n'}
# END DEMO DATA

DEMO_STAGES = (("0_start", "STOPPED_AT_GATE_1"), ("1_scope_confirmed", "DRAFT"), ("2_signed_off", "FINAL"))


def _demo_stage(stage):
    """Run one decision stage on the embedded synthetic data, entirely in memory (no file reads or writes)."""
    global _VFS, _OUT
    _VFS = dict(DEMO_COMMON)
    _VFS["decisions.json"] = DEMO_DECISIONS[stage]
    _OUT = {}
    try:
        report = generate()
        return report, dict(_OUT)
    finally:
        _VFS, _OUT = None, None


def _summary(report):
    keep = {"status": report.get("status")}
    if report.get("gate1_open"):
        keep["gate1_open"] = report["gate1_open"]
    if "counts" in report:
        keep["counts"] = report["counts"]
    if report.get("gates"):
        keep["gates"] = report["gates"]
    if report.get("rules_provenance") and report["rules_provenance"].get("agent_proposed"):
        keep["rules_provenance"] = report["rules_provenance"]
    if report.get("release_checks"):
        keep["release_checks"] = [f"{c.get('id')} {c.get('result')}: {c.get('check')}" for c in report["release_checks"]]
    if report.get("gate2_queue"):
        keep["gate2_queue"] = [f"{q['plant']}: '{q['description']}' -> {q['proposed_spec']} ({q['status']})" for q in report["gate2_queue"]]
    if report.get("opportunities"):
        keep["opportunities"] = [
            {k: _plain(o.get(k)) for k in ("rank", "plant", "spec_id", "price_per_1000", "benchmark_plant",
                                          "benchmark_price_per_1000", "labels", "scenario_saving", "rows", "benchmark_rows")}
            for o in report["opportunities"]]
    if report.get("error"):
        keep["error"] = report["error"]
    return keep


def demo():
    """Built-in synthetic demo: runs all three gate stages in memory and checks each against its expected status."""
    stages, ok = [], True
    for stage, expected in DEMO_STAGES:
        report, outputs = _demo_stage(stage)
        passed = report.get("status") == expected
        ok = ok and passed
        entry = {"stage": stage, "expected_status": expected, "check": "PASS" if passed else "FAIL"}
        entry.update(_summary(report))
        if stage == DEMO_STAGES[-1][0]:
            entry["opportunities_csv"] = outputs.get("opportunities.csv", "")
            entry["needs_review_csv"] = outputs.get("needs_review.csv", "")
        stages.append(entry)
    return {"skill": "cross-site-purchase-comparison", "version": VERSION, "mode": "BUILT_IN_DEMO",
            "data": "SYNTHETIC ONLY - embedded copy of examples/synthetic; no files were read or written",
            "result": "PASS" if ok else "FAIL",
            "note": "Savings are illustrative scenarios on synthetic data, not client results.",
            "stages": stages}


def _has_staged_scope():
    found = _index(os.curdir)
    return any(name in found for name in SCOPE_NAMES) or "setup.json" in found


def _parse_mode():
    """Optional --mode {auto,demo,run,propose,setup}; declared in scripts/manifest.json. Never used as a path."""
    import argparse
    parser = argparse.ArgumentParser(description="Cross-site purchase comparison")
    parser.add_argument("--mode", choices=("auto", "demo", "run", "propose", "setup"), default="auto")
    return parser.parse_args().mode


def main():
    mode = _parse_mode()
    if mode in ("propose", "setup"):
        result = propose() if mode == "propose" else setup()
        print(json.dumps(_plain(result), indent=2, ensure_ascii=False))
        return 1 if result.get("status") == "ERROR" else 0
    try:
        use_demo = mode == "demo" or (mode == "auto" and not _has_staged_scope())
    except WorkflowError as e:
        print(json.dumps({"status": "ERROR", "error": str(e)}, indent=2))
        return 1
    # demo: embedded synthetic data, in memory. run: staged files. auto: run if a scope file is staged, else demo.
    if use_demo:
        result = demo()
        print(json.dumps(_plain(result), indent=2, ensure_ascii=False))
        return 0 if result["result"] == "PASS" else 1
    result = generate()
    print(json.dumps(_plain(result), indent=2, ensure_ascii=False))
    return 0 if result.get("status") in ("FINAL", "DRAFT", "STOPPED_AT_GATE_0", "STOPPED_AT_GATE_1", "STOPPED_RECONCILIATION") else 1


if __name__ == "__main__":
    sys.exit(main())
