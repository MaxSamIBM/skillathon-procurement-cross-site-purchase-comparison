---
name: cross-site-purchase-comparison
description: >
  Compare what different plants pay for the same packaging item, from CSV purchase exports. Decides
  which purchases can fairly be compared, converts them to a common price basis, and returns ranked
  price gaps as savings SCENARIOS with source rows behind every number. Anything uncertain goes to a
  person; nothing is guessed. Use before a supplier negotiation, sourcing review or cost workshop.
type: workflow
allowed-tools:
  - run_skill_script
  - Read
  - Write
  - Grep
metadata:
  version: 0.2.1
---

# cross-site-purchase-comparison (v0.2.1)

**Business question:** *Are different parts of the client paying different prices for the same thing, and which differences are worth investigating?*

**Who runs it:** a procurement consultant, before a supplier negotiation, sourcing review or cost-reduction workshop.

## When to use it
Trigger phrases: "compare packaging prices across plants", "price variance by plant", "are we paying different prices for the same label", "prepare the sourcing workshop price comparison", "normalise purchase prices across sites".

Invoke this workflow from chat when a user asks for one of these comparisons or describes the business question above. Collect and verify the required plant exports, scope, rules, and human decisions; do not treat invoking the workflow as approval of any decision gate.

Do **not** use it to claim achieved savings, to compare across specifications nobody has approved as equivalent, or for categories other than the one the rules pack describes.

## Inputs (all are files; the skill contains no client data)

| Input | What it is | Changes per client? |
|---|---|---|
| Plant exports | One `.csv` per plant (header row anywhere in the first 30 rows; total rows allowed). Excel inputs must be exported to CSV before upload; `.xlsx` is not accepted. | Yes |
| `scope.json` | Category, reporting currency, comparison window, and per plant a `file` name (bare `.csv` name, no folders) plus optional `date_format` | Yes |
| `rules/columns.json` | Which column headers mean which standard field | Add the client's header names |
| `rules/specs.csv`, `item_map.csv` | Approved specifications, and which plant item code or description is which specification | Yes (a buyer approves) |
| `rules/spec_equivalences.csv` | Specifications a buyer has approved as interchangeable. **Empty by default.** | Yes (a buyer approves) |
| `rules/unit_conversions.csv` | Purchase unit to labels per unit, optionally per plant | Yes |
| `rules/fx_rates.csv` | Currency to reporting currency, with date and source | Yes (client's approved rate) |
| `rules/settings.json` | Order-size bands, freight groups, minimum plants per group | Tune per category |
| `rules/abbreviations.csv` | Short forms used to propose matches | Optional |
| `decisions.json` | The human decisions: Gate 1, column overrides, Gate 2 match decisions, Gate 3 sign-off | Yes (a person writes it) |

Templates and a worked example are in `examples/synthetic/`.

## Deterministic entry point

The deterministic entry script is `scripts/generate_cross_site_purchase_comparison.py`. It is self-contained, uses only Python's standard library, accepts CSV source files only, and runs without `run.py`, `ppc/`, third-party packages or arbitrary command-line flags. For the documented staged-file convention, the current working directory contains `scope.json`, `rules/`, `decisions.json`, and an `inputs_csv/` folder holding the plant CSVs named in scope; the script writes `out/run_report.json`, `out/opportunities.csv`, and `out/needs_review.csv`. This convention is a practical documentation example, not a claim about Process Studio's undocumented argument, staging, or return contract. Process Studio execution must be invoked from chat and must not rely on CLI flags.

## Chat-invoked flow and staged files

When this skill is invoked from chat, ask the user to upload one CSV per plant, the applicable scope and rules files, and any existing human decisions. Excel users must export as CSV. Verify the plant, category, date window, currency, source files and mapping before asking the user to confirm Gate 1; invocation is not approval. If rules or decisions are missing, request them or explain that the workflow cannot safely proceed. Do not infer mappings, units, FX, or equivalence.

For local testing only, stage these items in a working directory: `scope.json`, `rules/`, optional `decisions.json`, plus the plant CSVs inside `inputs_csv/`. Scope names each file by bare file name only; the script lists `inputs_csv/` and opens only CSV files it finds there, so no path from user input reaches a file operation. Run `python3 /path/to/scripts/generate_cross_site_purchase_comparison.py` with that directory as the current working directory; it accepts no arbitrary flags. This staged-file example does not document or imply Process Studio's runtime argument contract. Python 3 standard library only; no package installation required. The script writes results under `out/` within the working directory.

## The flow  (code does everything except the three decisions)

1. **Intake and check inputs**. Lists files and rules; stops if anything required is missing. **Gate 1:** a person confirms plants, category, window, reporting currency, and maps any column header the skill doesn't recognise.
2. **Map columns**. Headers become the standard fields (date, item code, description, quantity, unit, price, currency, supplier, freight terms). Unrecognized fields remain visible in the report; missing required fields stop at Gate 1.
3. **Load and reconcile**. Originals are never modified or saved (checked: file hashes are identical before and after a run). Row counts and spend are tied back to the file's own total rows; a mismatch **stops the run**. Missing quantity, price or unit goes to Needs review.
4. **Identify comparable items**. Exact approved item code or description matches automatically. Anything else is only a *proposal* and doesn't count until a person approves it (**Gate 2**). A different specification stays out unless `spec_equivalences.csv` says otherwise.
5. **Normalise prices**. Per 1,000 labels, in the reporting currency, using only the rules pack. An unknown unit or pack size is **not guessed**; the line goes to Needs review. Each conversion records its rule ID and rate. Interpret `M` as 1,000 labels only because the supplied `unit_conversions.csv` explicitly defines that conversion; the header text or alias alone does not establish a unit size.
6. **Check buying conditions**. Date window, order-size band and freight class. Purchases are compared only inside the same cell; a cheaper price on different terms never becomes the benchmark.
7. **Calculate and rank**. Gap against the lowest comparable price x affected volume = a potential saving, shown with its basis and assumptions. Always a scenario to confirm.
8. **Release checks**. Seven checks; any failure blocks opportunities. **Gate 3:** a person signs off; without it the report remains a **DRAFT**.
9. **Write traceable artifacts**: `out/run_report.json`, `out/opportunities.csv`, and `out/needs_review.csv`. The JSON report records run status, source hashes, mappings, reconciliation, gates, checks and trace references. Opportunities are withheld until Gate 1 is confirmed and reconciliation passes; a draft is not approval.

## What the code guarantees that a plain AI chat cannot
- Row counts and spend reconcile to each source file, or the run stops.
- No two specifications are compared unless a named buyer approved them as equivalent.
- No unit, pack size or currency is converted without a rule in the rules pack; none is guessed.
- A cheaper price bought on different terms (volume band, freight) is never used as a benchmark.
- Every scenario recomputes from its supporting rows, and every finding traces to source file and row number.
- Unapproved matches don't count; without Gate 3 the workbook says DRAFT.

## Outputs
- `run_report.json`, `opportunities.csv`, and `needs_review.csv` in `out/`. An unconfirmed Gate 1 leaves opportunities empty.
- Each finding is worded so a buyer can act on it, e.g. *"Plant A purchased specification X at $70.00 per 1,000 labels. Plant B purchased the same specification at $60.00 during the comparison period. Confirm whether Plant B's pricing can apply to Plant A (volume, location, terms) before including the difference in a savings plan."*

## Ready for another client
Change only: the plant exports, `scope.json`, the rules pack (headers, specifications and item map, unit conversions, FX, bands) and `decisions.json`. No code changes. Replace the synthetic rules with the client's; never put client data in this folder.

## Limits of v0.2.1 (be honest with the client)
- One category (packaging labels), one price basis (per 1,000 labels), CSV input only. Excel files must be exported to CSV first.
- Savings are scenarios. They don't confirm that a price is available at another plant's volume, location or terms. A buyer must confirm.
- Column mapping uses exact aliases plus a person; there is no fuzzy header guessing.
- Match *proposals* use simple word overlap. They are suggestions for Gate 2, never decisions.
- Single reporting currency, one FX rate per currency, no inflation or time-weighting.
- Variance analysis itself isn't new (procurement suites offer it). What's new here is the comparability method for inconsistent exports and the guarantees above. **A search of existing Process Studio skills and public assets has not been done yet.**

## Files
`SKILL.md` (chat-invoked workflow instructions) · `scripts/generate_cross_site_purchase_comparison.py` (self-contained standard-library entry point) · `examples/synthetic/` (synthetic CSV plant files, rules pack, decision stages and expected results). The legacy local CLI and workbook code are not part of the Process Studio workflow path.
