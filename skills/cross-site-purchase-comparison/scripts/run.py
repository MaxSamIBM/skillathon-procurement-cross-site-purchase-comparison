#!/usr/bin/env python3
"""cross-site-purchase-comparison 0.1.0 - command-line runner.

    python scripts/run.py --scope SCOPE.json --rules RULES_DIR --decisions DECISIONS.json --out OUT_DIR

Exit codes: 0 FINAL or DRAFT written | 2 stopped at Gate 1 | 3 reconciliation failed |
            4 release checks failed (no workbook) | 1 input or rules error."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from ppc import __version__  # noqa: E402
from ppc.compare import apply_window, assign_conditions, build_groups, normalize, rank  # noqa: E402
from ppc.loader import load_file, reconcile  # noqa: E402
from ppc.matching import match_lines  # noqa: E402
from ppc.release import release_checks  # noqa: E402
from ppc.rules import Rules, RulesError, sha256_file  # noqa: E402
from ppc.safepath import PathError, child  # noqa: E402
from ppc import workbook  # noqa: E402

ALLOWED_INPUT_EXT = (".xlsx", ".xlsm", ".csv")


def safe_input_path(base: str, rel: str) -> str:
    """Resolve a file path from scope.json and refuse anything outside the scope file's folder.

    Absolute paths, '..' segments and symlinks that leave the folder are rejected, so a scope file
    cannot make the skill read arbitrary files."""
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        raise RulesError(f"Invalid file path in scope: {rel!r}")
    base_real = os.path.realpath(base)
    cand = os.path.realpath(os.path.join(base_real, rel))
    if os.path.isabs(rel) or os.path.commonpath([base_real, cand]) != base_real:
        raise RulesError(f"File path '{rel}' in scope is outside the scope file's folder; "
                         "put input files next to scope.json (or in a subfolder) and use a relative path")
    if not cand.lower().endswith(ALLOWED_INPUT_EXT):
        raise RulesError(f"File '{rel}' is not an Excel workbook or CSV (.xlsx, .xlsm or .csv)")
    return cand


def confine(path: str, roots: list, what: str) -> str:
    """Resolve a command-line path and require it to sit inside one of the allowed root folders
    (the working folder, or the folder holding scope.json). Rejects '..' escapes and symlinks that leave."""
    if not isinstance(path, str) or "\x00" in path:
        raise RulesError(f"Invalid {what} path")
    real = os.path.realpath(path)
    for root in roots:
        root_real = os.path.realpath(root)
        if os.path.commonpath([root_real, real]) == root_real:
            return real
    raise RulesError(f"{what} path '{path}' is outside the working folder and the scope file's folder")


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    raise TypeError(type(o))


def _dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=_json_default)


def _load_json(path, what):
    if not path:
        return {}
    if not os.path.isfile(path):
        raise RulesError(f"{what} not found: {path}")
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError as e:
            raise RulesError(f"{what} is not valid JSON: {e}")


def run(scope_path, rules_dir, decisions_path, out_dir) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    report = {"skill": "cross-site-purchase-comparison", "version": __version__,
              "run_at": dt.datetime.now().isoformat(timespec="seconds"), "status": None}

    # ---------------- Step 1: intake and check inputs
    scope = _load_json(scope_path, "Scope file")
    decisions = _load_json(decisions_path, "Decisions file")
    problems = []
    for k in ("reporting_currency", "window", "files"):
        if k not in scope:
            problems.append(f"scope is missing '{k}'")
    if problems:
        raise RulesError("; ".join(problems))
    if len(scope["files"]) < 2:
        raise RulesError("At least two plant files are needed to compare prices")
    start = dt.date.fromisoformat(scope["window"]["start"])
    end = dt.date.fromisoformat(scope["window"]["end"])
    if start > end:
        raise RulesError("Comparison window starts after it ends")
    rules = Rules(rules_dir)
    base = os.path.dirname(os.path.abspath(scope_path))
    reporting = scope["reporting_currency"].upper()

    # ---------------- Steps 2-3: map columns, load, reconcile
    files = []
    resolved_paths = []
    overrides = decisions.get("column_overrides", {})
    for f in scope["files"]:
        path = safe_input_path(base, f.get("path"))
        resolved_paths.append(path)
        fr = load_file(f["plant"], path, rules, overrides.get(f["plant"], {}), f.get("date_format"))
        if not fr.errors and not fr.missing_required:
            reconcile(fr, rules)
        files.append(fr)

    report["inputs"] = {
        "files": [{"plant": fr.plant, "file": fr.file,
                   "sha256": sha256_file(p) if os.path.isfile(p) else None}
                  for fr, p in zip(files, resolved_paths)],
        "rules_sha256": rules.fingerprints,
    }
    report["files"] = [{"plant": fr.plant, "file": fr.file, "sheet": fr.sheet, "header_row": fr.header_row,
                        "mapping": fr.mapping, "mapping_basis": fr.mapping_basis,
                        "unmapped_headers": fr.unmapped_headers, "missing_required": fr.missing_required,
                        "errors": fr.errors, "reconciliation": fr.recon} for fr in files]

    # ---------------- Gate 1: scope and column mapping confirmed by a person
    g1_open = []
    for fr in files:
        if fr.errors:
            g1_open.append(f"{fr.plant} ({fr.file}): " + "; ".join(fr.errors))
        if fr.missing_required:
            g1_open.append(f"{fr.plant} ({fr.file}): required field(s) {fr.missing_required} not found. "
                           f"Unrecognised headers: {fr.unknown_headers}. Map them in decisions.json > column_overrides")
    if not decisions.get("gate1", {}).get("confirmed"):
        g1_open.append("Scope not confirmed: set decisions.json > gate1.confirmed = true after checking plants, "
                       f"category '{scope.get('category', '')}', window {start} to {end}, currency {reporting}")
    if g1_open:
        report["status"] = "STOPPED_AT_GATE_1"
        report["gate1_open"] = g1_open
        _dump(child(out_dir, "run_report.json"), report)
        return report

    bad_recon = [f"{fr.plant} ({fr.file}): rows {fr.recon['row_check']}, totals {fr.recon['total_check']} "
                 f"(loaded {fr.recon['loaded_amount']}, stated {fr.recon['stated_total']})"
                 for fr in files if fr.recon["row_check"] != "PASS" or fr.recon["total_check"] == "FAIL"]
    if bad_recon:
        report["status"] = "STOPPED_RECONCILIATION"
        report["reconciliation_failures"] = bad_recon
        _dump(child(out_dir, "run_report.json"), report)
        return report

    lines = [ln for fr in files for ln in fr.lines]

    # ---------------- Steps 4-7
    apply_window(lines, start, end)                                    # 6 window, applied first
    queue = match_lines(lines, rules, decisions.get("match_decisions", []))  # 4 + Gate 2
    normalize(lines, rules, reporting)                                 # 5 no guessing
    assign_conditions(lines, rules)                                    # 6
    groups = build_groups(lines, rules)
    opps, group_rows = rank(groups, rules)                             # 7

    # ---------------- Step 8: release checks
    checks = release_checks(files, lines, opps, rules)
    report["release_checks"] = checks
    report["gate2_queue"] = queue
    report["opportunities"] = [{k: v for k, v in o.items()} for o in opps]
    report["groups"] = group_rows
    report["counts"] = {k: sum(1 for ln in lines if ln.outcome == k)
                        for k in ("Compared", "Not compared", "Needs review", "Excluded")}
    report["counts"]["lines_loaded"] = len(lines)
    if any(c["result"] == "FAIL" for c in checks):
        report["status"] = "BLOCKED_RELEASE_CHECKS"
        _dump(child(out_dir, "run_report.json"), report)
        return report

    g3 = decisions.get("gate3", {})
    gate_log = [("Gate 1 - scope and columns", f"confirmed by {decisions['gate1'].get('by', 'unnamed')}")]
    if queue:
        gate_log.append(("Gate 2 - uncertain matches", f"OPEN: {len(queue)} match(es) waiting; those lines are not compared"))
    else:
        gate_log.append(("Gate 2 - uncertain matches", "no open matches"))
    if queue or not g3.get("approved"):
        status = "DRAFT"
        gate_log.append(("Gate 3 - sign-off", "OPEN" if not queue else "OPEN - resolve Gate 2 first"))
    else:
        status = "FINAL"
        gate_log.append(("Gate 3 - sign-off", f"approved by {g3.get('by', 'unnamed')}"))
    report["status"] = status
    report["gates"] = gate_log

    # ---------------- Step 9: workbook
    name = f"price_comparison_{status.lower()}.xlsx"
    wb_path = child(out_dir, name)
    workbook.write(wb_path, status, scope, files, lines, opps, group_rows, queue, checks, gate_log, __version__)
    report["workbook"] = name
    _dump(child(out_dir, "run_report.json"), report)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", default="")
    ap.add_argument("--rules", default="")
    ap.add_argument("--decisions", default="")
    ap.add_argument("--out", default="out")
    ap.add_argument("--example", choices=["0", "1", "2", "0csv", "1csv", "2csv"], default="",
                    help="run the bundled synthetic demo at gate stage 0, 1 or 2 (suffix 'csv' uses the CSV copies)")
    a = ap.parse_args(argv)
    if a.example:
        ex = os.path.join(SKILL_DIR, "examples", "synthetic")
        stage = {"0": "decisions_0_start.json", "1": "decisions_1_scope_confirmed.json",
                 "2": "decisions_2_signed_off.json"}[a.example[0]]
        a.scope = os.path.join(ex, "scope_csv.json" if a.example.endswith("csv") else "scope.json")
        a.rules = os.path.join(ex, "rules")
        a.decisions = os.path.join(ex, stage)
    elif not (a.scope and a.rules):
        ap.error("give --scope and --rules, or use --example")
    try:
        scope_real = os.path.realpath(a.scope)
        roots = [os.getcwd(), os.path.dirname(scope_real)]
        if a.example:
            roots.append(os.path.join(SKILL_DIR, "examples"))  # the bundled synthetic demo may be read from here
        a.scope = confine(a.scope, roots, "Scope file")
        a.rules = confine(a.rules, roots, "Rules folder")
        a.decisions = confine(a.decisions, roots, "Decisions file") if a.decisions else ""
        a.out = confine(a.out, roots, "Output folder")
        rep = run(a.scope, a.rules, a.decisions, a.out)
    except (RulesError, PathError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    st = rep["status"]
    print(f"Status: {st}")
    for item in rep.get("gate1_open", []) + rep.get("reconciliation_failures", []):
        print(f"  - {item}")
    for c in rep.get("release_checks", []):
        print(f"  {c['id']} {c['result']}  {c['check']}")
    for o in rep.get("opportunities", []):
        print(f"  #{o['rank']} {o['plant']}: ${o['price_per_1000']} vs {o['benchmark_plant']} "
              f"${o['benchmark_price_per_1000']} per 1,000 x {o['labels']:,} labels = ${o['scenario_saving']:,} scenario")
    for q in rep.get("gate2_queue", []):
        print(f"  Gate 2: {q['plant']} '{q['description']}' -> {q['proposed_spec'] or '?'} ({q['status']}, {q['score']})")
    if rep.get("workbook"):
        print(f"Workbook: {a.out.rstrip('/')}/{rep['workbook']}")
    return {"STOPPED_AT_GATE_1": 2, "STOPPED_RECONCILIATION": 3, "BLOCKED_RELEASE_CHECKS": 4}.get(st, 0)


if __name__ == "__main__":
    sys.exit(main())
