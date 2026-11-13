"""Step 4: match each purchase line to an approved specification.

Exact item code or exact approved description -> approved automatically.
Anything else -> a *proposal* scored by token overlap after expanding abbreviations. A proposal
never counts until a person approves it in decisions.json (Gate 2)."""
from __future__ import annotations

from .model import Line
from .rules import Rules, norm_text


def _tokens(text: str, rules: Rules) -> set:
    out = set()
    for t in norm_text(text).split():
        out.add(rules.abbr.get(t, t))
    return out


def _score(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_lines(lines: list[Line], rules: Rules, decisions: list[dict]) -> list[dict]:
    """Returns the Gate 2 queue: proposals and ambiguous lines still waiting for a person."""
    dec_index = {}
    for d in decisions or []:
        key = (d["plant"].strip().lower(), norm_text(d["description"]))
        dec_index[key] = d

    spec_tokens = {}
    for r in rules.item_map:
        if r["status"].lower() == "approved" and r["description"]:
            spec_tokens.setdefault(r["spec_id"], []).append(_tokens(r["description"], rules))
    for sid, name in rules.specs.items():
        spec_tokens.setdefault(sid, []).append(_tokens(name, rules))

    queue, seen = [], set()
    for ln in lines:
        if ln.status == "excluded":
            continue
        p = ln.plant.strip().lower()
        hit = rules.map_by_code.get((p, ln.item_code.strip().lower())) if ln.item_code else None
        basis = "approved item code"
        if not hit and ln.description:
            hit = rules.map_by_desc.get((p, norm_text(ln.description)))
            basis = "approved description"
        if hit:
            ln.spec_id, ln.match_status, ln.match_basis, ln.match_score = hit["spec_id"], "approved", basis, 1.0
            ln.canon_spec = rules.canonical_spec(hit["spec_id"])
            continue

        toks = _tokens(ln.description, rules)
        scored = sorted(((max(_score(toks, t) for t in ts), sid) for sid, ts in spec_tokens.items()),
                        reverse=True)
        best_score, best_sid = scored[0] if scored else (0.0, "")
        runner = scored[1][0] if len(scored) > 1 else 0.0

        d = dec_index.get((p, norm_text(ln.description)))
        if d:
            if d.get("decision") == "approve":
                sid = d.get("spec_id") or best_sid
                if sid not in rules.specs:
                    ln.flag(f"Gate 2 decision names unknown specification '{sid}'")
                    ln.match_status = "none"
                    continue
                ln.spec_id, ln.match_status = sid, "approved_by_person"
                ln.match_basis = f"approved at Gate 2 by {d.get('by', 'unnamed')}: {d.get('reason', '')}".strip()
                ln.match_score = round(best_score, 2)
                ln.canon_spec = rules.canonical_spec(sid)
            else:
                ln.match_status = "rejected"
                ln.flag(f"Match rejected at Gate 2 by {d.get('by', 'unnamed')}: {d.get('reason', '')}")
            continue

        if best_score >= rules.propose_threshold and best_score - runner > 0.1:
            ln.spec_id, ln.match_status, ln.match_score = best_sid, "proposed", round(best_score, 2)
            ln.match_basis = "description similarity (not approved)"
            ln.flag(f"Proposed match to {best_sid} (score {ln.match_score}) - needs approval at Gate 2")
        elif best_score >= rules.propose_threshold:
            ln.match_status, ln.match_score = "ambiguous", round(best_score, 2)
            ln.match_basis = f"close to more than one specification ({scored[0][1]}, {scored[1][1]})"
            ln.flag("Ambiguous: description is close to more than one specification - needs a person")
        else:
            ln.match_status, ln.match_score = "none", round(best_score, 2)
            ln.flag("No approved match for this item - add it to item_map.csv or decide at Gate 2")

        key = (ln.plant, ln.description)
        if ln.match_status in ("proposed", "ambiguous") and key not in seen:
            seen.add(key)
            queue.append({"plant": ln.plant, "description": ln.description,
                          "proposed_spec": ln.spec_id or "", "score": ln.match_score,
                          "status": ln.match_status, "example_row": ln.ref})
    return queue
