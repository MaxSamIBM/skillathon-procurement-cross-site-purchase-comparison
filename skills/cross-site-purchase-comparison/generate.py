#!/usr/bin/env python3
"""Best-effort conventional adapter for Process Studio-style staged inputs.

The platform's callable contract is not confirmed. This adapter accepts common
path collections and named paths, discovers conventional files below cwd, and
hands the resolved inputs to the existing validated pipeline.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.realpath(__file__))

if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run as runner  # noqa: E402
from ppc.rules import Rules  # noqa: E402
from ppc.rules import RulesError  # noqa: E402


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(root), os.path.realpath(path)]) == os.path.realpath(root)
    except ValueError:
        return False


def _parse_inputs(inputs: Any) -> tuple[dict[str, str], list[str]]:
    """Extract conventional named paths plus staged paths from common payload shapes."""
    named: dict[str, str] = {}
    staged: list[str] = []
    if isinstance(inputs, str):
        try:
            decoded = json.loads(inputs)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, (dict, list)):
            inputs = decoded
        elif inputs:
            staged.append(inputs)
    elif isinstance(inputs, os.PathLike):
        staged.append(os.fspath(inputs))

    aliases = {
        "scope": "scope_path", "scope_path": "scope_path",
        "rules": "rules_dir", "rules_dir": "rules_dir", "rules_path": "rules_dir",
        "decisions": "decisions_path", "decision": "decisions_path",
        "decisions_path": "decisions_path", "decision_path": "decisions_path",
        "out": "out_dir", "out_dir": "out_dir", "output_dir": "out_dir",
    }

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                canonical = aliases.get(str(child_key).lower())
                if canonical and isinstance(child, (str, os.PathLike)):
                    named[canonical] = os.fspath(child)
                elif isinstance(child, (dict, list, tuple)):
                    visit(child, str(child_key))
                elif isinstance(child, (str, os.PathLike)):
                    staged.append(os.fspath(child))
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
        elif isinstance(value, (str, os.PathLike)):
            staged.append(os.fspath(value))

    if isinstance(inputs, (dict, list, tuple)):
        visit(inputs)
    return named, staged


def _walk(root: str):
    """Walk without following symlinks or descending into generated/cache folders."""
    skipped = {".git", "__pycache__", ".pytest_cache", "out", "output"}
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in skipped and not os.path.islink(os.path.join(current, d))]
        yield current, files


def _discover(cwd: str, named: dict[str, str], staged: list[str]) -> dict[str, str]:
    # The core runner resolves transaction paths relative to scope.json and already
    # rejects paths escaping that folder. Keep all staged top-level inputs in cwd.
    paths = []
    for path in staged:
        candidate = os.path.realpath(os.path.join(cwd, path) if not os.path.isabs(path) else path)
        if not _within(candidate, cwd):
            raise ValueError(f"Staged input path is outside the working directory: {path}")
        paths.append(candidate)

    result = dict(named)
    for key in ("scope_path", "rules_dir", "decisions_path"):
        if key in result:
            path = os.path.realpath(os.path.join(cwd, result[key]) if not os.path.isabs(result[key]) else result[key])
            if not _within(path, cwd):
                raise ValueError(f"{key} is outside the working directory: {result[key]}")
            result[key] = path

    files_by_dir: dict[str, set[str]] = {}
    json_candidates: list[str] = []
    discovered_dirs: dict[str, list[str]] = {}
    for current, files in _walk(cwd):
        files_by_dir[current] = set(files)
        discovered_dirs[current] = files
        json_candidates.extend(os.path.join(current, f) for f in files if f.lower().endswith(".json"))

    for path in paths:
        if os.path.isdir(path):
            discovered_dirs.setdefault(path, os.listdir(path))
            files_by_dir.setdefault(path, set(os.listdir(path)))
        elif path.lower().endswith(".json") and path not in json_candidates:
            json_candidates.append(path)

    if "rules_dir" not in result:
        dirs = {os.path.dirname(p) for p in paths if os.path.isfile(p)}
        dirs.update(p for p in paths if os.path.isdir(p))
        dirs.update(discovered_dirs)
        valid = []
        for folder in dirs:
            try:
                Rules(folder)
                valid.append(os.path.realpath(folder))
            except Exception:
                continue
        valid = sorted(set(valid))
        staged_rules = [folder for folder in valid if any(os.path.realpath(p) == folder for p in paths if os.path.isdir(p))]
        valid = staged_rules if staged_rules else valid
        if len(valid) != 1:
            raise ValueError("Could not identify one unambiguous complete rules folder under the working directory")
        result["rules_dir"] = valid[0]

    if "scope_path" not in result:
        scopes = []
        for path in json_candidates:
            if not _within(path, cwd) or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as stream:
                    value = json.load(stream)
                if (isinstance(value, dict) and isinstance(value.get("files"), list)
                        and isinstance(value.get("window"), dict)
                        and "reporting_currency" in value):
                    scopes.append(path)
            except (OSError, ValueError):
                continue
        preferred = [p for p in scopes if os.path.basename(p).lower() == "scope.json"]
        scopes = preferred if preferred else scopes
        scopes = sorted(set(scopes))
        if len(scopes) != 1:
            raise ValueError("Could not identify one unambiguous scope JSON file under the working directory")
        result["scope_path"] = scopes[0]

    if "decisions_path" not in result:
        decisions = []
        for path in json_candidates:
            if not _within(path, cwd) or not os.path.isfile(path) or path == result["scope_path"]:
                continue
            basename = os.path.basename(path).lower()
            if "decision" not in basename:
                continue
            try:
                with open(path, encoding="utf-8") as stream:
                    value = json.load(stream)
                if isinstance(value, dict) and any(k in value for k in ("gate1", "gate3", "match_decisions", "column_overrides")):
                    decisions.append(path)
            except (OSError, ValueError):
                continue
        staged_decisions = [p for p in decisions if os.path.realpath(p) in paths]
        decisions = staged_decisions if staged_decisions else decisions
        preferred = [p for p in decisions if os.path.basename(p).lower() == "decisions.json"]
        decisions = preferred if preferred else decisions
        decisions = sorted(set(decisions))
        if len(decisions) == 1:
            result["decisions_path"] = decisions[0]
        elif len(decisions) > 1:
            raise ValueError("Multiple decisions JSON files found; pass decisions_path explicitly")

    result.setdefault("decisions_path", "")
    result.setdefault("out_dir", os.path.join(cwd, "out"))
    out = result["out_dir"]
    out = os.path.realpath(os.path.join(cwd, out) if not os.path.isabs(out) else out)
    if not _within(out, cwd):
        raise ValueError(f"Output directory is outside the working directory: {out}")
    result["out_dir"] = out

    # Enforce the core runner's own confinement rules as well. Scope-relative
    # transaction paths are validated by run.safe_input_path during execution.
    roots = [cwd, os.path.dirname(result["scope_path"])]
    result["scope_path"] = runner.confine(result["scope_path"], roots, "Scope file")
    result["rules_dir"] = runner.confine(result["rules_dir"], roots, "Rules folder")
    if result["decisions_path"]:
        result["decisions_path"] = runner.confine(result["decisions_path"], roots, "Decisions file")
    result["out_dir"] = runner.confine(result["out_dir"], roots, "Output folder")
    return result


def generate(inputs: Any = None, **kwargs: Any) -> str:
    """Run the validated comparison from staged paths and return a JSON summary.

    Accepted best-effort shapes include a list of paths, JSON text containing a
    path list/object, or a dict with scope/rules/decisions/output path keys. Any
    omitted input is discovered by conventional names/content below cwd.
    """
    named, staged = _parse_inputs(inputs)
    for key, value in kwargs.items():
        if key in {"scope", "scope_path", "rules", "rules_dir", "rules_path", "decisions",
                   "decision", "decisions_path", "decision_path", "out", "out_dir", "output_dir"}:
            if value is None:
                continue
            canonical = {
                "scope": "scope_path", "scope_path": "scope_path", "rules": "rules_dir",
                "rules_dir": "rules_dir", "rules_path": "rules_dir", "decisions": "decisions_path",
                "decision": "decisions_path", "decisions_path": "decisions_path",
                "decision_path": "decisions_path", "out": "out_dir", "out_dir": "out_dir",
                "output_dir": "out_dir",
            }[key]
            named[canonical] = os.fspath(value)
    cwd = os.path.realpath(os.getcwd())
    selected = _discover(cwd, named, staged)

    try:
        report = runner.run(selected["scope_path"], selected["rules_dir"],
                            selected["decisions_path"], selected["out_dir"])
    except (RulesError, OSError, ValueError) as exc:
        return json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False)
    summary = {
        "status": report.get("status"),
        "summary": report.get("gate1_open", []) + report.get("reconciliation_failures", []),
        "workbook": report.get("workbook"),
        "output_directory": selected["out_dir"],
        "opportunities": [
            {"rank": item.get("rank"), "plant": item.get("plant"),
             "specification": item.get("spec_id"), "price_per_1000": str(item.get("price_per_1000")),
             "benchmark_plant": item.get("benchmark_plant"),
             "benchmark_price_per_1000": str(item.get("benchmark_price_per_1000")),
             "labels": str(item.get("labels")), "scenario_saving": str(item.get("scenario_saving"))}
            for item in report.get("opportunities", [])
        ],
        "needs_review": report.get("gate2_queue", []),
    }
    summary["summary"].extend(
        f"{item['id']} {item['result']}: {item['check']}" for item in report.get("release_checks", [])
    )
    return json.dumps(summary, ensure_ascii=False, indent=2)
