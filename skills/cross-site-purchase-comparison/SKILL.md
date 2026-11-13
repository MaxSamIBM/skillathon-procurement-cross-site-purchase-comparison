---
name: cross-site-purchase-comparison
version: 0.1.0
description: >
  Compare what different plants pay for the same packaging item, from inconsistent Excel purchase
  exports. Decides which purchases can fairly be compared, converts them to a common price basis, and
  returns ranked price gaps as savings SCENARIOS with the source rows behind every number. Anything
  uncertain goes to a person; nothing is guessed. Use before a supplier negotiation, sourcing review or
  cost-reduction workshop.
---

# cross-site-purchase-comparison  (v0.1.0)

**Business question:** *Are different parts of the client paying different prices for the same thing, and which differences are worth investigating?*

**Who runs it:** a procurement consultant, before a supplier negotiation, sourcing review or cost-reduction workshop.

## When to use it
Trigger phrases: "compare packaging prices across plants", "price variance by plant", "are we paying different prices for the same label", "prepare the sourcing workshop price comparison", "normalise purchase prices across sites".

Do **not** use it to claim achieved savings, to compare across specifications nobody has approved as equivalent, or for categories other than the one the rules pack describes.

## Inputs (all are files; the skill contains no client data)

| Input | What it is | Changes per client? |
|---|---|---|
| Plant exports | One `.xlsx` per plant (header row anywhere in the first 30 rows; total rows allowed) | Yes |
| `scope.json` | Category, reporting currency, comparison window, list of files and plants, optional `date_format` per file | Yes |
| `rules/columns.json` | Which column headers mean which standard field | Add the client's header names |
| `rules/specs.csv`, `item_map.csv` | Approved specifications, and which plant item code or description is which specification | Yes (a buyer approves) |
| `rules/spec_equivalences.csv` | Specifications a buyer has approved as interchangeable. **Empty by default.** | Yes (a buyer approves) |
| `rules/unit_conversions.csv` | Purchase unit to labels per unit, optionally per plant | Yes |
| `rules/fx_rates.csv` | Currency to reporting currency, with date and source | Yes (client's approved rate) |
| `rules/settings.json` | Order-size bands, freight groups, minimum plants per group | Tune per category |
| `rules/abbreviations.csv` | Short forms used to propose matches | Optional |
| `decisions.json` | The human decisions: Gate 1, column overrides, Gate 2 match decisions, Gate 3 sign-off | Yes (a person writes it) |

Templates and a worked example are in `examples/synthetic/`.

## How to run

```
python3 scripts/run.py --scope scope.json --rules rules/ --decisions decisions.json --out out/
```
Needs Python 3.10+ and `openpyxl`. Exit codes: `0` workbook written | `1` input or rules error | `2` stopped at Gate 1 | `3` reconciliation failed | `4` release checks failed (no workbook).

## The flow  (code does everything except the three decisions)

1. **Intake and check inputs**. Lists files and rules; stops if anything required is missing. **Gate 1:** a person confirms plants, category, window, reporting currency, and maps any column header the skill doesn't recognise.
2. **Map columns**. Headers become the standard fields (date, item code, description, quantity, unit, price, currency, supplier, freight terms). Unrecognised columns are kept, not dropped.
3. **Load and reconcile**. Originals are never modified or saved (checked: file hashes are identical before and after a run). Row counts and spend are tied back to the file's own total rows; a mismatch **stops the run**. Missing quantity, price or unit goes to Needs review.
4. **Identify comparable items**. Exact approved item code or description matches automatically. Anything else is only a *proposal* and doesn't count until a person approves it (**Gate 2**). A different specification stays out unless `spec_equivalences.csv` says otherwise.
5. **Normalise prices**. Per 1,000 labels, in the reporting currency, using only the rules pack. An unknown unit or pack size is **not guessed**; the line goes to Needs review. Each conversion records its rule ID and rate.
6. **Check buying conditions**. Date window, order-size band and freight class. Purchases are compared only inside the same cell; a cheaper price on different terms never becomes the benchmark.
7. **Calculate and rank**. Gap against the lowest comparable price x affected volume = a potential saving, shown with its basis and assumptions. Always a scenario to confirm.
8. **Release checks**. Seven checks; any failure blocks the workbook. **Gate 3:** a person signs off; without it the output is a **DRAFT**.
9. **Write the workbook**. Tabs: *Summary*, *Opportunities*, *Supporting transactions*, *Comparability checks*, *Needs review*. A machine-readable `run_report.json` is written alongside.

## What the code guarantees that a plain AI chat cannot
- Row counts and spend reconcile to each source file, or the run stops.
- No two specifications are compared unless a named buyer approved them as equivalent.
- No unit, pack size or currency is converted without a rule in the rules pack; none is guessed.
- A cheaper price bought on different terms (volume band, freight) is never used as a benchmark.
- Every saving recomputes from its supporting rows, and every row traces to file, sheet and row number.
- Unapproved matches don't count; without Gate 3 the workbook says DRAFT.

## Outputs
- `price_comparison_final.xlsx` (or `_draft.xlsx`), `run_report.json`.
- Each finding is worded so a buyer can act on it, e.g. *"Plant A purchased specification X at $70.00 per 1,000 labels. Plant B purchased the same specification at $60.00 during the comparison period. Confirm whether Plant B's pricing can apply to Plant A (volume, location, terms) before including the difference in a savings plan."*

## Ready for another client
Change only: the plant exports, `scope.json`, the rules pack (headers, specifications and item map, unit conversions, FX, bands) and `decisions.json`. No code changes. Replace the synthetic rules with the client's; never put client data in this folder.

## Limits of v0.1.0  (be honest with the client)
- One category (packaging labels), one price basis (per 1,000 labels), `.xlsx` only.
- Savings are scenarios. They don't confirm that a price is available at another plant's volume, location or terms. A buyer must confirm.
- Column mapping uses exact aliases plus a person; there is no fuzzy header guessing.
- Match *proposals* use simple word overlap. They are suggestions for Gate 2, never decisions.
- Single reporting currency, one FX rate per currency, no inflation or time-weighting.
- Variance analysis itself isn't new (procurement suites offer it). What's new here is the comparability method for inconsistent exports and the guarantees above. **A search of existing Process Studio skills and public assets has not been done yet.**

## Files
`scripts/run.py` (CLI) · `scripts/ppc/` (loader, matching, compare, release, workbook, rules) · `tests/` (23 tests) · `examples/synthetic/` (4 messy plant files, rules pack, 3 decision stages, hand-calculated expected results, `TRAPS.md`).
