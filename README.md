# cross-site-purchase-comparison  (v0.1.0, first pass)

Compares what different plants pay for the same packaging item. It takes messy purchase exports from several plants, works out which purchases can **fairly** be compared, and returns ranked price differences worth investigating. Every number traces to a source row. Anything uncertain goes to a person.

Read [`skills/cross-site-purchase-comparison/SKILL.md`](skills/cross-site-purchase-comparison/SKILL.md) for workflow metadata, chat invocation guidance, triggers, inputs, outputs, the flow and the limits.

## Repository layout

```
skills/cross-site-purchase-comparison/
  SKILL.md      workflow instructions and metadata
  scripts/      deterministic generate_* entry point
  run.py, ppc/  local runner and bundled modules
  examples/     synthetic plant files, rules pack, decision stages, expected results
  tests/        pipeline and entry-point tests
```

## Try it in a minute (synthetic data, no client data)

```
pip install openpyxl
cd skills/cross-site-purchase-comparison
python3 examples/synthetic/generate_examples.py        # creates the four messy plant files
E=examples/synthetic
python3 run.py --scope $E/scope.json --rules $E/rules --decisions $E/decisions_0_start.json         --out out0   # stops at Gate 1
python3 run.py --scope $E/scope.json --rules $E/rules --decisions $E/decisions_1_scope_confirmed.json --out out1   # DRAFT
python3 run.py --scope $E/scope.json --rules $E/rules --decisions $E/decisions_2_signed_off.json     --out out2   # FINAL
python3 -m unittest discover -s tests                  # 29 tests
```

## What the demo shows

| Run | Result |
|---|---|
| 0. Start | **Stops at Gate 1.** Plant D's two unfamiliar headers need a person to map them. |
| 1. Scope confirmed | **DRAFT.** Plant A is $70.00 per 1,000 labels against Plant B's $60.00, a $20,000 scenario on 2,000,000 labels. Plant D's abbreviated description is *proposed*, not counted. |
| 2. Signed off | **FINAL.** The buyer approved Plant D's match, adding a $2,112 scenario. All seven release checks pass. |

Planted traps (full list in `examples/synthetic/TRAPS.md`): different headers per plant, roll price vs per-label price, specification Y kept out, a roll with no pack size, EUR prices, an abbreviated description, an out-of-window order, a blank quantity, a small spot order and an ex-works price that must not set the benchmark.

## Status and honesty notes
- **Tested on synthetic data only.** 29 tests pass. Two rules were deliberately broken (a wrong pack size, a false specification equivalence) and the tests failed as they should.
- **Not yet checked:** that Process Studio can run this script, read uploaded `.xlsx` files and write a workbook; whether similar skills already exist in Process Studio; whether the name is free; whether the client agreement allows reusing the method.
- **Savings figures are illustrative scenarios**, not results. The EUR rate is made up. No real client numbers appear anywhere.
- No pilot has been run, so there are no measured time savings or accuracy figures. A pilot should measure analyst preparation time, buyer-validated findings, and false matches.
