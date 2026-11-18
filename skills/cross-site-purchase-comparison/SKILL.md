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
metadata:
  version: 0.3.0
---

# cross-site-purchase-comparison (v0.3.0)

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
| `scope.json` (or `scope_csv.json`) | Category, reporting currency, comparison window, and per plant a `file` name (bare `.csv` name, no folders) plus optional `date_format` | Yes |
| Rules pack | One `rules.json` with a section per table below, or a `rules/` folder holding the eight files | Yes |
| ↳ `columns.json` | Which column headers mean which standard field | Add the client's header names |
| ↳ `specs.csv`, `item_map.csv` | Approved specifications, and which plant item code or description is which specification | Yes (a buyer approves) |
| ↳ `spec_equivalences.csv` | Specifications a buyer has approved as interchangeable. **Empty by default.** | Yes (a buyer approves) |
| ↳ `unit_conversions.csv` | Purchase unit to labels per unit, optionally per plant | Yes |
| ↳ `fx_rates.csv` | Currency to reporting currency, with date and source | Yes (client's approved rate) |
| ↳ `settings.json` | Order-size bands, freight groups, minimum plants per group | Tune per category |
| ↳ `abbreviations.csv` | Short forms used to propose matches | Optional |
| `decisions.json` (or a single `decisions_*.json`) | The human decisions: Gate 1, column overrides, Gate 2 match decisions, Gate 3 sign-off | Yes (a person writes it) |

The rules pack can be one `rules.json` with a section per table below (`columns`, `settings`, `specs`, `item_map`, `spec_equivalences`, `abbreviations`, `unit_conversions`, `fx_rates`), or a `rules/` folder holding the eight files. Build `rules.json` from the folder with `tools/make_rules_json.py` in the repo.

Templates and a worked example are in `examples/synthetic/`.

## Deterministic entry point

The deterministic entry script is `scripts/generate_cross_site_purchase_comparison.py`. If no scope file is staged it runs the built-in demo (mode 1). It is self-contained, uses only Python's standard library, accepts CSV source files only, and runs without `run.py`, `ppc/`, third-party packages or arbitrary command-line flags. For the documented staged-file convention, the current working directory contains `scope.json` or `scope_csv.json`, the rules pack as a single `rules.json` (preferred, fewer files to upload) or a `rules/` folder, one decisions file (`decisions.json` or a single `decisions_*.json`), and the plant CSVs named in scope placed either directly in the working directory (flat) or inside an `inputs_csv/` folder; the script writes `out/run_report.json`, `out/opportunities.csv`, and `out/needs_review.csv`. This convention is a practical documentation example, not a claim about Process Studio's undocumented argument, staging, or return contract. Process Studio execution must be invoked from chat and must not rely on CLI flags.

## Choose the mode first

| Situation | Mode | What runs |
|---|---|---|
| The user wants to **see the skill work**, or file staging is failing or unavailable | **1. Built-in demo** | The script, on embedded synthetic data. Nothing to stage. |
| The user uploaded their own files **and staging works** | **2. Script run on their files** | The script, on the staged files (the flow below). |
| The user uploaded files **but staging fails or is unavailable** | **3. Chat-only review** | The assistant reads the uploads as text. **No script runs.** |

Never silently drop from mode 2 to mode 3, and never present mode 3 results as script-verified.

## Mode 1: built-in demo (no files needed)

Call `run_skill_script` for `scripts/generate_cross_site_purchase_comparison.py` **with no arguments and no staged files**. With no scope file present the script runs the embedded synthetic example through all three decision stages, entirely in memory, and prints one JSON result. It reads no uploads, writes no files, and needs no `write_temp_file`.

Present the result like this, quoting the script's own output and not paraphrasing numbers:
1. **Stage 0, `STOPPED_AT_GATE_1`:** the run refuses to report savings until a person confirms the scope.
2. **Stage 1, `DRAFT`:** Gate 1 is confirmed. Plant A pays $70.00 per 1,000 labels against Plant B's $60.00. The scenario is $20,000 on 2,000,000 labels. Plant D's abbreviated description is held in the Gate 2 queue because nobody has approved the match. Plant C's specification Y is never compared with X.
3. **Stage 2, `FINAL`:** all three gates are signed off. Plant D's approved match adds a $2,112 scenario. All seven release checks show PASS.

State clearly: **this is synthetic data, the savings are scenarios to confirm with a buyer and not results, and the three sign-offs in the demo are pre-recorded, not real approvals.** If the script returns anything other than `"result": "PASS"`, show the exact output and stop.

## Mode 2: running the script on the user's files

### Chat-invoked flow and staged files

When this skill is invoked from chat, ask the user to upload one CSV per plant, the scope file, the rules pack as one `rules.json`, and one decisions file. That is the plant CSVs plus three files, which keeps staging small. A `rules/` folder of eight files is also accepted. Excel users must export as CSV. Verify the plant, category, date window, currency, source files and mapping before asking the user to confirm Gate 1; invocation is not approval. If rules or decisions are missing, request them or explain that the workflow cannot safely proceed. Do not infer mappings, units, FX, or equivalence.

Files should be staged flat in the working directory (or inside `inputs_csv/` if subdirectories are supported): `scope.json`, `rules.json` (or `rules/`), optional `decisions.json`, plus the plant CSVs (`plant_A_purchases.csv`, etc.). Scope names each file by bare file name only; the script inspects the working directory (or `inputs_csv/`) and opens only CSV files it finds there, so no path from user input reaches a file operation. Run `python3 /path/to/scripts/generate_cross_site_purchase_comparison.py` with that directory as the current working directory; it accepts no arbitrary flags. This staged-file example does not document or imply Process Studio's runtime argument contract. Python 3 standard library only; no package installation required. The script writes results under `out/` within the working directory.

## Mode 3: chat-only review (fallback when staging fails)

Use only when the user has uploaded files, staging has failed, and the user agrees to proceed. Say first, in plain words: **"No script ran. This is my reading of your files, not code-verified results."**

- Read the uploaded scope, rules and plant CSVs as text, and follow the same flow, rules and gates as below. Obey the same limits: no cross-specification comparison unless the rules pack lists an approved equivalence; no unit, pack size or currency conversion without a rule; a cheaper price on different terms is not a benchmark; uncertain matches go to the person.
- Show your working: for each finding, the source file, row, quantity, unit, price and the conversion rule used.
- Label every figure **"model-calculated, not code-verified"**. Never say "reconciled", "release checks passed" or "FINAL". The most you may produce is a **DRAFT**.
- Offer to re-run in mode 2 once staging works, and stop and ask the user at each gate; never confirm a gate for them.

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

## Limits of v0.3.0 (be honest with the client)
- One category (packaging labels), one price basis (per 1,000 labels), CSV input only. Excel files must be exported to CSV first.
- Savings are scenarios. They don't confirm that a price is available at another plant's volume, location or terms. A buyer must confirm.
- Column mapping uses exact aliases plus a person; there is no fuzzy header guessing.
- Match *proposals* use simple word overlap. They are suggestions for Gate 2, never decisions.
- Single reporting currency, one FX rate per currency, no inflation or time-weighting.
- The built-in demo (mode 1) uses only the embedded synthetic data, and its gate sign-offs are pre-recorded. It proves the code runs on the platform; it doesn't process a client's files.
- Chat-only review (mode 3) is the model reading files, not the script. It carries none of the code guarantees above.
- Variance analysis itself isn't new (procurement suites offer it). What's new here is the comparability method for inconsistent exports and the guarantees above. **A search of existing Process Studio skills and public assets has not been done yet.**

## Files
`SKILL.md` (chat-invoked workflow instructions) · `scripts/generate_cross_site_purchase_comparison.py` (self-contained standard-library entry point; contains the embedded synthetic demo) · `examples/synthetic/` (synthetic CSV plant files, rules pack, decision stages and expected results). The legacy local CLI and workbook code are not part of the Process Studio workflow path.
