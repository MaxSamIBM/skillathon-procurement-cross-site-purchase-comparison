"""The one record type passed through every step."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class Line:
    plant: str
    file: str
    sheet: str
    row: int                                  # 1-based Excel row number in the source sheet
    date: Optional[dt.date] = None
    item_code: str = ""
    description: str = ""
    supplier: str = ""
    quantity: Optional[Decimal] = None
    purchase_unit: str = ""
    unit_price: Optional[Decimal] = None
    currency: str = ""
    freight_terms: str = ""
    line_amount: Optional[Decimal] = None

    issues: list = field(default_factory=list)
    status: str = "ok"                        # ok | review | excluded

    spec_id: str = ""                         # approved or proposed specification
    canon_spec: str = ""                      # set only when the match is approved
    match_status: str = ""                    # approved | approved_by_person | proposed | ambiguous | rejected | none
    match_basis: str = ""
    match_score: float = 0.0

    labels_per_unit: Optional[Decimal] = None
    conv_rule: str = ""
    labels: Optional[Decimal] = None
    fx_rate: Optional[Decimal] = None
    fx_rule: str = ""
    price_per_1000: Optional[Decimal] = None  # reporting currency
    amount_reporting: Optional[Decimal] = None

    band: str = ""
    freight_class: str = ""
    group: str = ""
    outcome: str = ""                         # Compared | Not compared | Needs review | Excluded

    def flag(self, text: str) -> None:
        self.issues.append(text)
        if self.status == "ok":
            self.status = "review"

    @property
    def ref(self) -> str:
        return f"{self.file} / {self.sheet} / row {self.row}"

    @property
    def match_ok(self) -> bool:
        return self.match_status in ("approved", "approved_by_person")
