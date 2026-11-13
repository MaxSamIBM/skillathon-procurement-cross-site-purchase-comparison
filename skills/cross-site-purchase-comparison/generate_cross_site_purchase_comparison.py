#!/usr/bin/env python3
"""Process Studio auto-executable entry point for cross-site-purchase-comparison.

The callable `generate_cross_site_purchase_comparison` is kept at the skill root so
Process Studio can discover a generate_* entry point. The runner and ppc package
are also at the skill root so local modules resolve as part of the skill bundle.

Callable arguments are file paths relative to the Process Studio execution working
folder (or the scope file's folder):
  scope_path: required JSON scope file
  rules_dir: required rules-pack directory
  decisions_path: optional human decisions JSON
  out_dir: output directory, defaults to `out`

The CLI form remains available for local use. Process Studio's exact callable
argument/return contract should be confirmed with the platform owner.
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
from ppc.rules import RulesError  # noqa: E402


def generate_cross_site_purchase_comparison(
    scope_path: str,
    rules_dir: str,
    decisions_path: str = "",
    out_dir: str = "out",
) -> str:
    """Run the comparison and return a JSON summary for the calling agent."""
    try:
        scope_real = os.path.realpath(scope_path)
        roots = [os.getcwd(), os.path.dirname(scope_real)]
        safe_scope = runner.confine(scope_path, roots, "Scope file")
        safe_rules = runner.confine(rules_dir, roots, "Rules folder")
        safe_decisions = runner.confine(decisions_path, roots, "Decisions file") if decisions_path else ""
        safe_out = runner.confine(out_dir, roots, "Output folder")
        report = runner.run(safe_scope, safe_rules, safe_decisions, safe_out)
        result: dict[str, Any] = {
            "status": report.get("status"),
            "summary": [],
            "workbook": report.get("workbook"),
            "output_directory": safe_out,
        }
        result["summary"].extend(report.get("gate1_open", []))
        result["summary"].extend(report.get("reconciliation_failures", []))
        result["summary"].extend(
            f"{check['id']} {check['result']}: {check['check']}"
            for check in report.get("release_checks", [])
        )
        result["opportunities"] = [
            {
                "rank": item.get("rank"),
                "plant": item.get("plant"),
                "specification": item.get("spec_id"),
                "price_per_1000": str(item.get("price_per_1000")),
                "benchmark_plant": item.get("benchmark_plant"),
                "benchmark_price_per_1000": str(item.get("benchmark_price_per_1000")),
                "labels": str(item.get("labels")),
                "scenario_saving": str(item.get("scenario_saving")),
            }
            for item in report.get("opportunities", [])
        ]
        result["needs_review"] = report.get("gate2_queue", [])
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (RulesError, OSError, ValueError) as exc:
        return json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    """Preserve the existing CLI, including its bundled synthetic demo option."""
    return runner.main(argv)


if __name__ == "__main__":
    sys.exit(main())
