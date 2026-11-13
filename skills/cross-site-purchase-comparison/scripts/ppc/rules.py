"""Loads and validates the rules pack (a folder of CSV/JSON files). Fails loudly on bad rules."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from decimal import Decimal, InvalidOperation


class RulesError(Exception):
    """The rules pack is missing a file or contains something unusable."""


def norm_text(s) -> str:
    """Lower-case, punctuation -> spaces. Used for headers and descriptions."""
    return re.sub(r"[^a-z0-9]+", " ", str(s if s is not None else "").lower()).strip()


def norm_unit(s) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "").strip().lower())


def dec(v, what: str) -> Decimal:
    try:
        return Decimal(str(v).strip())
    except InvalidOperation:
        raise RulesError(f"{what}: '{v}' is not a number")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Rules:
    FILES = ["columns.json", "settings.json", "specs.csv", "item_map.csv",
             "spec_equivalences.csv", "abbreviations.csv", "unit_conversions.csv", "fx_rates.csv"]

    def __init__(self, folder: str):
        if not os.path.isdir(folder):
            raise RulesError(f"Rules folder not found: {folder}")
        self.folder = folder
        for name in self.FILES:
            if not os.path.isfile(os.path.join(folder, name)):
                raise RulesError(f"Rules pack is missing '{name}' (folder: {folder})")
        self.fingerprints = {n: sha256_file(os.path.join(folder, n)) for n in self.FILES}

        # ---- columns
        cols = self._json("columns.json")
        self.required = list(cols["required"])
        self.optional = list(cols.get("optional", []))
        self.fields = self.required + self.optional
        self.alias_index: dict[str, str] = {}
        for fld, aliases in cols["aliases"].items():
            if fld not in self.fields:
                raise RulesError(f"columns.json: alias list for unknown field '{fld}'")
            for a in aliases:
                k = norm_text(a)
                if self.alias_index.get(k, fld) != fld:
                    raise RulesError(f"columns.json: alias '{a}' maps to two fields")
                self.alias_index[k] = fld

        # ---- settings
        s = self._json("settings.json")
        self.basis = dec(s.get("price_basis_labels", 1000), "settings.price_basis_labels")
        self.propose_threshold = float(s.get("matching", {}).get("propose_threshold", 0.6))
        self.bands = s["volume_bands"]
        if not self.bands or self.bands[-1].get("below_labels_per_order") is not None:
            raise RulesError("settings.json: the last volume band must have below_labels_per_order = null")
        self.freight_groups = {k.strip().upper(): v for k, v in s["freight_groups"].items()}
        self.min_plants = int(s.get("min_plants_per_group", 2))
        self.tol = dec(s.get("amount_tolerance", "0.01"), "settings.amount_tolerance")

        # ---- specs, item map, equivalences
        self.specs = {r["spec_id"]: r["spec_name"] for r in self._csv("specs.csv", ["spec_id", "spec_name"])}
        self.item_map = self._csv("item_map.csv", ["plant", "item_code", "description", "spec_id", "status"])
        for r in self.item_map:
            if r["spec_id"] not in self.specs:
                raise RulesError(f"item_map.csv: unknown spec_id '{r['spec_id']}'")
        self.map_by_code, self.map_by_desc = {}, {}
        for r in self.item_map:
            if r["status"].lower() != "approved":
                continue
            p = r["plant"].strip().lower()
            if r["item_code"]:
                self.map_by_code[(p, r["item_code"].strip().lower())] = r
            if r["description"]:
                self.map_by_desc[(p, norm_text(r["description"]))] = r
        self._parent = {sid: sid for sid in self.specs}
        for r in self._csv("spec_equivalences.csv", ["spec_a", "spec_b", "status"]):
            for k in ("spec_a", "spec_b"):
                if r[k] not in self.specs:
                    raise RulesError(f"spec_equivalences.csv: unknown spec '{r[k]}'")
            if r["status"].lower() == "approved":
                self._union(r["spec_a"], r["spec_b"])

        # ---- abbreviations
        self.abbr = {}
        for r in self._csv("abbreviations.csv", ["short", "long"]):
            self.abbr[norm_text(r["short"])] = norm_text(r["long"])

        # ---- unit conversions
        self.unit_rules = {}
        for r in self._csv("unit_conversions.csv", ["rule_id", "unit_alias", "plant", "labels_per_unit"]):
            lpu = dec(r["labels_per_unit"], f"unit_conversions {r['rule_id']}")
            if lpu <= 0:
                raise RulesError(f"unit_conversions {r['rule_id']}: labels_per_unit must be > 0")
            key = (norm_unit(r["unit_alias"]), r["plant"].strip().lower() or "*")
            if key in self.unit_rules:
                raise RulesError(f"unit_conversions: duplicate rule for unit '{r['unit_alias']}' / plant '{r['plant']}'")
            self.unit_rules[key] = {"rule_id": r["rule_id"], "labels_per_unit": lpu, "note": r.get("note", "")}

        # ---- fx
        self.fx_rules = {}
        for r in self._csv("fx_rates.csv", ["rule_id", "currency", "rate_to_reporting", "reporting_currency"]):
            rate = dec(r["rate_to_reporting"], f"fx_rates {r['rule_id']}")
            if rate <= 0:
                raise RulesError(f"fx_rates {r['rule_id']}: rate must be > 0")
            key = (r["currency"].upper(), r["reporting_currency"].upper())
            if key in self.fx_rules:
                raise RulesError(f"fx_rates: duplicate rate for {key[0]}->{key[1]}")
            self.fx_rules[key] = {"rule_id": r["rule_id"], "rate": rate, "as_of": r.get("as_of", ""),
                                  "note": r.get("source_note", "")}

    # ------------------------------------------------------------------ helpers
    def _json(self, name):
        try:
            with open(os.path.join(self.folder, name), encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, KeyError) as e:
            raise RulesError(f"{name} is not valid: {e}")

    def _csv(self, name, required_cols):
        path = os.path.join(self.folder, name)
        with open(path, newline="", encoding="utf-8-sig") as f:
            rd = csv.DictReader(f)
            missing = [c for c in required_cols if c not in (rd.fieldnames or [])]
            if missing:
                raise RulesError(f"{name}: missing column(s) {missing}")
            return [{k: (v or "").strip() for k, v in r.items() if k}
                    for r in rd if any((v or "").strip() for v in r.values())]

    def _find(self, x):
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def _union(self, a, b):
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)

    # ------------------------------------------------------------------ lookups
    def canonical_spec(self, spec_id: str) -> str:
        return self._find(spec_id)

    def unit_rule(self, unit: str, plant: str):
        u, p = norm_unit(unit), plant.strip().lower()
        return self.unit_rules.get((u, p)) or self.unit_rules.get((u, "*"))

    def fx_rule(self, currency: str, reporting: str):
        return self.fx_rules.get((currency.upper(), reporting.upper()))

    def band_for(self, labels: Decimal) -> str:
        for b in self.bands:
            lim = b["below_labels_per_order"]
            if lim is None or labels < Decimal(str(lim)):
                return b["name"]
        return self.bands[-1]["name"]
