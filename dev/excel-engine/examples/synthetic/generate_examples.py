#!/usr/bin/env python3
"""Generates the four synthetic plant exports used by the example and the tests.

All data is invented. Each file is deliberately messy in a different way (see TRAPS.md).
Run:  python3 generate_examples.py
"""
import datetime as dt
import os

from openpyxl import Workbook
from openpyxl.styles import Font

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "inputs")


def d(s):
    return dt.datetime.strptime(s, "%Y-%m-%d")


def save(wb, name):
    os.makedirs(OUT, exist_ok=True)
    wb.save(os.path.join(OUT, name))


def bold(ws, row):
    for c in ws[row]:
        c.font = Font(bold=True)


# ---------------------------------------------------------------- Plant A
# Clean layout, price per ROLL-500, one order OUTSIDE the comparison window, a Total row.
def plant_a():
    wb = Workbook()
    ws = wb.active
    ws.title = "Purchases"
    ws.append(["PO Date", "Material", "Material Description", "Order Qty", "UoM", "Net Price",
               "Curr", "Vendor", "Incoterm", "Net Value"])
    bold(ws, 1)
    desc = "Product label, white, specification X"
    rows = [
        (d("2025-09-15"), "A-1001", desc, 500, "ROLL-500", 35.00),    # outside window
        (d("2025-12-05"), "A-1001", desc, 1000, "ROLL-500", 35.00),
        (d("2026-03-04"), "A-1001", desc, 1000, "ROLL-500", 35.00),
        (d("2026-06-10"), "A-1001", desc, 1000, "ROLL-500", 35.00),
        (d("2026-09-09"), "A-1001", desc, 1000, "ROLL-500", 35.00),
    ]
    total = 0
    for dte, code, ds, q, u, p in rows:
        val = q * p
        total += val
        ws.append([dte, code, ds, q, u, p, "USD", "Vendor 1", "DAP", val])
    ws.append(["Total", None, None, None, None, None, None, None, None, total])
    save(wb, "plant_A_purchases.xlsx")


# ---------------------------------------------------------------- Plant B
# Price per ROLL-1000. Traps: a tiny spot order (small band), an EXW order (other freight
# class), a line with a blank quantity, and a "Grand Total" row.
def plant_b():
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["Date", "Item No", "Item Text", "Quantity", "Unit", "Unit Price", "Currency",
               "Supplier Name", "Delivery Terms", "Line Total"])
    bold(ws, 1)
    desc = "White label, specification X"
    rows = [
        (d("2025-11-20"), 880021, desc, 500, "ROLL-1000", 60.00, "DAP"),
        (d("2026-01-12"), 880021, desc, None, "ROLL-1000", 60.00, "DAP"),   # blank quantity
        (d("2026-02-18"), 880021, desc, 500, "ROLL-1000", 60.00, "DAP"),
        (d("2026-04-01"), 880021, desc, 10, "ROLL-1000", 75.00, "DAP"),     # small spot order
        (d("2026-05-20"), 880021, desc, 500, "ROLL-1000", 60.00, "DAP"),
        (d("2026-07-07"), 880021, desc, 300, "ROLL-1000", 52.00, "EXW"),    # different freight terms
        (d("2026-08-19"), 880021, desc, 500, "ROLL-1000", 60.00, "DAP"),
    ]
    total = 0
    for dte, code, ds, q, u, p, inco in rows:
        val = None if q is None else q * p
        total += val or 0
        ws.append([dte, code, ds, q, u, p, "USD", "Vendor 2", inco, val])
    ws.append(["Grand Total", None, None, None, None, None, None, None, None, total])
    save(wb, "plant_B_export.xlsx")


# ---------------------------------------------------------------- Plant C
# Report-style layout: merged title and blank rows above the header, a Subtotal row in the
# middle, "RL (500)" unit spelling, a different specification (Y), and one line whose unit
# ("ROLL") has no pack size.
def plant_c():
    wb = Workbook()
    ws = wb.active
    ws.title = "Packaging"
    ws["A1"] = "Plant C - Packaging purchases"
    ws.merge_cells("A1:J1")
    ws["A2"] = "Exported 2026-11-02"
    ws.append([])
    ws.append(["Posting Date", "Mat Code", "Mat Desc", "Qty", "OUn", "Price", "Crcy", "Vendor",
               "Inco-terms", "Value"])
    bold(ws, 4)
    desc = "Label, specification Y"
    first = [
        (d("2026-01-15"), "C-77", desc, 800, "RL (500)", 40.00),
        (d("2026-05-05"), "C-77", desc, 100, "ROLL", 41.00),                # no pack size
    ]
    sub = 0
    for dte, code, ds, q, u, p in first:
        ws.append([dte, code, ds, q, u, p, "USD", "Vendor 3", "DAP", q * p])
        sub += q * p
    ws.append(["Subtotal H1", None, None, None, None, None, None, None, None, sub])
    ws.append([d("2026-07-15"), "C-77", desc, 800, "RL (500)", 40.00, "USD", "Vendor 3", "DAP", 800 * 40.0])
    ws.append(["Total", None, None, None, None, None, None, None, None, sub + 800 * 40.0])
    save(wb, "plant_C_report.xlsx")


# ---------------------------------------------------------------- Plant D
# EUR, dates stored as text (dd.mm.yyyy), price and quantity per thousand ("M"), abbreviated
# description with no item code, two column headers the alias list does not know, a Notes sheet.
def plant_d():
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Doc Date", "Material", "Short Text", "Order Qty (M)", "OUn", "Price per M", "Cur.",
               "Supplier", "IncoT", "Net Value"])
    bold(ws, 1)
    rows = [
        ("15.01.2026", None, "Lbl wht spec X", 400, "M", 58.00),
        ("03.03.2026", None, "Etikett weiss", 100, "M", 50.00),            # matches nothing
        ("20.06.2026", None, "Lbl wht spec X", 400, "M", 58.00),
    ]
    total = 0
    for dte, code, ds, q, u, p in rows:
        ws.append([dte, code, ds, q, u, p, "EUR", "Supplier 4", "DAP", q * p])
        total += q * p
    ws.append(["Total", None, None, None, None, None, None, None, None, total])
    notes = wb.create_sheet("Notes")
    notes["A1"] = "Extract from the plant ERP. Prices are per thousand labels (M)."
    save(wb, "plant_D_extract.xlsx")


if __name__ == "__main__":
    plant_a()
    plant_b()
    plant_c()
    plant_d()
    print("Wrote 4 synthetic plant files to", OUT)
