# cross-site-purchase-comparison (v0.2.0)

CSV-first workflow to compare prices for the same approved packaging specifications across plants. It preserves source reconciliation, explicit rules, approved-equivalence controls and human gates. Results are scenarios for buyer review, not achieved or guaranteed savings.

Read [`skills/cross-site-purchase-comparison/SKILL.md`](skills/cross-site-purchase-comparison/SKILL.md) for chat invocation guidance, required uploads, gates, outputs and limitations.

## Workflow package

```
skills/cross-site-purchase-comparison/
  SKILL.md
  scripts/generate_cross_site_purchase_comparison.py  # self-contained stdlib entry point
  examples/synthetic/                                 # synthetic CSVs, rules, decisions
dev/skill-tests/   # tests for the skill (outside skills/ so the scanner skips them)
dev/excel-engine/  # legacy .xlsx engine, local development only
```

Run the tests with `python3 -m unittest discover -s dev/skill-tests`.

The Process Studio path is chat-invoked and must not rely on arbitrary CLI flags. The entry script does not import the legacy local `ppc` or `run` modules and has no third-party dependency. It reads CSV only; no XLSX or `openpyxl` support.

## Chat flow

Ask the user for one CSV purchase export per plant, a scope describing plant names/category/reporting currency/date window, the applicable rules pack, and any human decisions. Ask users with Excel workbooks to export CSV before uploading. Verify scope and source mappings with the user before Gate 1; similarity proposals require a person at Gate 2, and Gate 3 requires explicit final sign-off. Do not guess at data, mappings, conversions or equivalences.

For local testing (not a claim about Process Studio's undocumented staging contract), stage files under the current working directory:

```
working-dir/
  scope.json
  decisions.json        # optional until a human decision is recorded
  rules/                # complete rules pack
  inputs_csv/           # CSVs at paths referenced in scope.json
```

Run with that directory as current working directory:

```
python3 /path/to/skills/cross-site-purchase-comparison/scripts/generate_cross_site_purchase_comparison.py
```

No flags or package installation are required. All scope-relative input paths and the output directory are checked using real paths; symlink/path escapes fail closed. Outputs are written under `working-dir/out/`:

- `run_report.json` — status, file hashes, column mappings, row/totals reconciliation, gate state, release checks and traceable findings.
- `opportunities.csv` — ranked potential price gaps with traceable rows and assumptions; withheld until Gate 1 is confirmed and reconciliation passes.
- `needs_review.csv` — Gate 1 issues, unresolved matching proposals, excluded lines and other cases needing attention.

## Synthetic regression tests

From the repository root:

```
python3 -m unittest skills.cross-site-purchase-comparison.tests.test_csv_generate -v
```

Synthetic fixtures only; no client data is included. The older local CLI/workbook implementation is retained only for legacy tests/examples and is not used by the Process Studio entry point.

## Limitations

One category (packaging labels), one price basis (per 1,000 labels), single reporting currency, one FX rate per currency; input is CSV only. Similarity is a proposal, never approval. Scenario savings require a buyer to verify achievable volume, location and terms. Process Studio's exact script staging/invocation and return contracts have not been verified; this package's CWD staging convention is documentation for a practical local workflow only.

## Repository layout

- `skills/cross-site-purchase-comparison/` is the only thing Process Studio scans. It is stdlib-only and CSV-only.
- `dev/excel-engine/` is the earlier `.xlsx` engine (openpyxl). It is kept for local development only and is deliberately outside `skills/` so the BYOS scanner never sees it.
