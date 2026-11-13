# Planted traps in the synthetic example

All four files are invented. Each one is messy in a way real plant exports are, and each trap tests a specific rule.

| # | Plant | Trap | What the skill must do | Step |
|---|---|---|---|---|
| 1 | All | Different column names in every file | Map to the standard template from `columns.json`; ask a person for headers it doesn't know | 2 |
| 2 | A, B, C, D | `Total`, `Grand Total`, `Subtotal H1` rows inside the data | Skip them as data, and use them to check the totals reconcile | 3 |
| 3 | C | Title, merged cells and blank rows above the header | Find the real header row | 3 |
| 4 | D | Two headers not in the alias list (`Order Qty (M)`, `Price per M`) | Stop at Gate 1 until a person maps them | 1–2 |
| 5 | D | Dates stored as text, `dd.mm.yyyy` | Parse with the file's declared date format | 3 |
| 6 | D | A `Notes` sheet | Ignore it; read only the data sheet | 3 |
| 7 | A vs B | Roll of 500 vs roll of 1,000 | Compare per 1,000 labels: A is $70, B is $60 (B looks dearer per roll) | 5 |
| 8 | C | Specification Y | Keep it out of the specification X comparison | 4, 8 |
| 9 | C | Unit `ROLL` with no pack size | Don't guess; send to Needs review | 5 |
| 10 | D | Prices in EUR | Convert with `fx_rates.csv` and record the rate used | 5 |
| 11 | D | Abbreviated text `Lbl wht spec X`, no item code | Propose a match; it counts only after a person approves (Gate 2) | 4 |
| 12 | D | `Etikett weiss` matches nothing | Needs review; never forced into a group | 4 |
| 13 | A | One order before the comparison window | Exclude it, with the reason | 6 |
| 14 | B | Blank quantity | Needs review | 3 |
| 15 | B | A 10-roll spot order at $75 | Different volume band; not compared with the standard orders | 6 |
| 16 | B | An EXW order at $52 | Different freight class; not compared with delivered prices | 6 |

## Expected outcome (signed-off run)

| Rank | Plant | Price per 1,000 | Benchmark (Plant B) | Labels | Scenario saving |
|---|---|---|---|---|---|
| 1 | Plant A | $70.00 | $60.00 | 2,000,000 | $20,000 |
| 2 | Plant D | $62.64 | $60.00 | 800,000 | $2,112 |

Plant B's $52 EXW price is lower, but it isn't the benchmark because it was bought on different freight terms. That's the point of trap 16: a naive comparison would overstate the savings.

The figures are scenarios for a buyer to confirm, not achieved savings. The EUR rate is illustrative.
