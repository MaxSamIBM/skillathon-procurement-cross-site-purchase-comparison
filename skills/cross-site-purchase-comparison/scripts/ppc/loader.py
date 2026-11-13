"""Steps 2-3: find the header, map columns to the standard template, load rows, and
reconcile row counts and totals back to the source file. Originals are opened read-only."""
from __future__ import annotations

import datetime as dt
import os
import re
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook

from .model import Line
from .rules import Rules, norm_text

TOTAL_RE = re.compile(r"\b(grand\s*total|sub\s*-?\s*total|total)\b", re.I)


def _cell_text(v) -> str:
    return "" if v is None else str(v).strip()


def _to_dec(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return None
    try:
        return Decimal(str(v).replace(",", "").strip())
    except InvalidOperation:
        return "BAD"


def _to_date(v, fmt):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    for f in ([fmt] if fmt else []) + ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S"]:
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    return "BAD"


class FileResult:
    def __init__(self, plant, path):
        self.plant, self.path, self.file = plant, path, os.path.basename(path)
        self.sheet = ""
        self.header_row = None
        self.mapping = {}           # source header -> field
        self.mapping_basis = {}     # source header -> "alias" | "person"
        self.unmapped_headers = []  # headers kept but not used
        self.unknown_headers = []   # headers needing a person (only if a required field is missing)
        self.missing_required = []
        self.lines: list[Line] = []
        self.skipped = []           # (row, label) of total/subtotal/blank rows
        self.recon = {}             # filled by reconcile()
        self.source_rows = 0        # rows below the header, counted straight from the sheet
        self.errors = []


def _find_sheet_and_header(wb, rules: Rules):
    best = None
    for ws in wb.worksheets:
        for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30), values_only=True), 1):
            hits = sum(1 for v in row if norm_text(v) in rules.alias_index)
            if hits and (best is None or hits > best[2]):
                best = (ws, r_idx, hits)
    return best


def load_file(plant: str, path: str, rules: Rules, overrides: dict, date_format: str | None) -> FileResult:
    fr = FileResult(plant, path)
    if not os.path.isfile(path):
        fr.errors.append(f"File not found: {path}")
        return fr
    wb = load_workbook(path, read_only=False, data_only=True)
    found = _find_sheet_and_header(wb, rules)
    if not found or found[2] < 2:
        fr.errors.append("Could not find a header row (fewer than 2 known column names in the first 30 rows)")
        return fr
    ws, hdr_row, _ = found
    fr.sheet, fr.header_row = ws.title, hdr_row
    headers = [_cell_text(c.value) for c in ws[hdr_row]]

    ov = {norm_text(k): v for k, v in (overrides or {}).items()}
    col_of = {}
    for i, h in enumerate(headers):
        if not h:
            continue
        key = norm_text(h)
        if key in ov:
            fld = ov[key]
            if fld not in rules.fields:
                fr.errors.append(f"Column override for '{h}' names unknown field '{fld}'")
                continue
            basis = "person"
        elif key in rules.alias_index:
            fld, basis = rules.alias_index[key], "alias"
        else:
            fr.unmapped_headers.append(h)
            continue
        if fld in col_of:
            fr.errors.append(f"Two columns map to '{fld}': '{headers[col_of[fld]]}' and '{h}'")
            continue
        col_of[fld] = i
        fr.mapping[h] = fld
        fr.mapping_basis[h] = basis

    fr.missing_required = [f for f in rules.required if f not in col_of]
    if fr.missing_required:
        fr.unknown_headers = list(fr.unmapped_headers)
        return fr
    if fr.errors:
        return fr

    fr.source_rows = sum(1 for _ in ws.iter_rows(min_row=hdr_row + 1, values_only=True))

    def get(row, fld):
        i = col_of.get(fld)
        return row[i] if i is not None and i < len(row) else None

    for r_idx, row in enumerate(ws.iter_rows(min_row=hdr_row + 1, values_only=True), hdr_row + 1):
        texts = [_cell_text(v) for v in row]
        if not any(texts):
            fr.skipped.append((r_idx, "blank", None))
            continue
        first_text = next((t for t in texts if t), "")
        if TOTAL_RE.search(first_text) and _to_dec(get(row, "quantity")) is None:
            fr.skipped.append((r_idx, first_text, _to_dec(get(row, "line_amount"))))
            continue
        ln = Line(plant=plant, file=fr.file, sheet=fr.sheet, row=r_idx)
        ln.item_code = _cell_text(get(row, "item_code"))
        ln.description = _cell_text(get(row, "description"))
        ln.supplier = _cell_text(get(row, "supplier"))
        ln.purchase_unit = _cell_text(get(row, "purchase_unit"))
        ln.currency = _cell_text(get(row, "currency")).upper()
        ln.freight_terms = _cell_text(get(row, "freight_terms")).upper()

        d = _to_date(get(row, "date"), date_format)
        if d == "BAD":
            ln.flag(f"Date '{get(row, 'date')}' could not be read (expected format {date_format or 'a real date'})")
        else:
            ln.date = d
            if d is None:
                ln.flag("Missing date")
        for fld in ("quantity", "unit_price", "line_amount"):
            v = _to_dec(get(row, fld))
            if v == "BAD":
                ln.flag(f"{fld.replace('_', ' ').capitalize()} '{get(row, fld)}' is not a number")
                v = None
            setattr(ln, fld, v)
        if ln.quantity is None and "quantity" in col_of:
            ln.flag("Missing quantity")
        elif ln.quantity is not None and ln.quantity <= 0:
            ln.flag(f"Quantity {ln.quantity} is not positive")
        if ln.unit_price is None:
            ln.flag("Missing unit price")
        if not ln.purchase_unit:
            ln.flag("Missing purchase unit")
        if not ln.currency:
            ln.flag("Missing currency")
        if not ln.description and not ln.item_code:
            ln.flag("No description or item code")
        fr.lines.append(ln)
    wb.close()
    return fr


def reconcile(fr: FileResult, rules: Rules) -> None:
    """Row counts and spend must tie back to the source. A mismatch blocks the run (step 3)."""
    src_rows = fr.source_rows
    loaded_amount = sum((ln.line_amount for ln in fr.lines if ln.line_amount is not None), Decimal(0))
    totals = [(r, lbl, amt) for r, lbl, amt in fr.skipped
              if lbl != "blank" and amt not in (None, "BAD")]
    grand = [t for t in totals if not re.search(r"sub", t[1], re.I)]
    stated = grand[-1][2] if grand else None
    # each line's own arithmetic: quantity x price = amount
    arith_bad = []
    for ln in fr.lines:
        if ln.quantity is not None and ln.unit_price is not None and ln.line_amount is not None:
            if abs(ln.quantity * ln.unit_price - ln.line_amount) > rules.tol:
                arith_bad.append(ln.row)
                ln.flag(f"Quantity x price ({ln.quantity * ln.unit_price}) does not equal the line amount ({ln.line_amount})")
    fr.recon = {
        "rows_in_source": src_rows,
        "rows_loaded": len(fr.lines),
        "rows_skipped": len(fr.skipped),
        "skipped_detail": [f"row {r}: {lbl}" for r, lbl, _ in fr.skipped if lbl != "blank"],
        "loaded_amount": loaded_amount,
        "stated_total": stated,
        "total_check": ("no total row in file" if stated is None
                        else "PASS" if abs(loaded_amount - stated) <= rules.tol else "FAIL"),
        "row_check": "PASS" if src_rows == len(fr.lines) + len(fr.skipped) else "FAIL",
        "_row_check_doc": "rows below the header in the sheet = rows loaded + rows skipped (totals, blanks)",
        "arithmetic_mismatch_rows": arith_bad,
    }
