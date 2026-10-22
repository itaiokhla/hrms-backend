"""Formula editor catalogue and country-pack tag requirements (F-43).

* ``GET /api/v1/payroll/formula/catalogue``: the functions (name, arity, signature, description), the
  variables (namespace, name, type) and the tables (company and pack-owned ``payroll_tables`` effective
  today) a formula may use, plus the component codes and tags of the catalogue, for autocomplete. Functions
  and variables are generated from ``app.payroll.formula`` (``FUNCTIONS``, ``VARIABLES``), so the editor never
  drifts from the language. Protected attributes (EC-CMP-19) are left out: company formulas can never read
  them.
* ``GET /api/v1/payroll/packs/tag-requirements?legal_entity_id=``: for each legal entity in the caller's
  salary-structure scope, the country-pack versions effective today and the component tags each one reads
  (statutory wage bases, taxable income), with the company components that carry each tag. Tags are
  discovered from the pack code itself (``statutory_bases`` / ``taxable_income`` are probed with a recording
  line), so a new pack needs no extra declaration. Every pack is labelled with its usage: a fixture version is
  "FIXTURE - not law", a draft or under-review version "Compliance not verified".

Both are read-only and need payroll:salary_structure:read; an entity outside the caller's scope is 404.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core.errors import not_found
from app.core.principal import Principal, require
from app.database.db import get_db
from app.models.org import LegalEntity
from app.models.payroll_config import PayrollTable
from app.payroll import formula as fm
from app.payroll import packs
from app.payroll.formula import functions as fm_functions
from app.payroll.formula import variables as fm_variables
from app.payroll.packs import base as pack_base
from app.payroll.services import components as comp
from app.payroll.services import structures

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll formula catalogue"])

STATUTORY_BASE = "STATUTORY_BASE"
TAXABLE_INCOME = "TAXABLE_INCOME"
_TAG = fm_functions.CODE


# --------------------------------------------------------------------------- formula catalogue
def _function_json(definition: fm.FunctionDef) -> dict[str, Any]:
    return {
        "name": definition.name, "group": definition.group, "signature": definition.signature,
        "description": definition.description,
        "arity": {"min": definition.min_args, "max": definition.max_args},
        "quoted_arguments": {str(index): kind.value for index, kind in sorted(definition.literals.items())},
        "operator": definition.keyword,
    }


def _variable_json(definition: fm.VariableDef) -> dict[str, Any]:
    namespace, _, name = definition.name.partition(".")
    return {
        "name": definition.name, "namespace": namespace, "short_name": name, "type": str(definition.type),
        "pay_currency": definition.type.is_money and definition.type.currency is None,
        "description": definition.description, "source_setting": definition.source,
        "may_be_null": definition.may_be_null,
    }


def _tables(db: Session, company_id: int, on: dt.date) -> list[dict[str, Any]]:
    rows = (db.query(PayrollTable)
            .filter(PayrollTable.company_id == company_id, PayrollTable.status == "ACTIVE",
                    PayrollTable.effective_from <= on)
            .order_by(PayrollTable.code, PayrollTable.effective_from).all())
    found: dict[str, PayrollTable] = {}
    for row in rows:
        if row.effective_to is None or row.effective_to >= on:
            found[row.code] = row
    return [{"code": t.code, "name": t.name, "kind": t.kind, "currency": t.currency, "owner": t.owner,
             "effective_from": t.effective_from, "effective_to": t.effective_to} for t in found.values()]


@router.get("/formula/catalogue")
def formula_catalogue(principal: Principal = Depends(require(comp.READ)), db: Session = Depends(get_db)):
    cid = principal.company_id
    on = comp.today(db, cid)
    entries = comp.catalogue(db, cid, on)
    variables = [_variable_json(v) for v in fm.VARIABLES.values() if not v.protected]
    tags = sorted({tag for entry in entries.values() for tag in entry.version.tags or ()})
    return {
        "as_of": on,
        "functions": [_function_json(f) for f in fm.FUNCTIONS.values()],
        "rounding_modes": list(fm.ROUNDING_MODES),
        "namespaces": list(fm.NAMESPACES),
        "variables": variables,
        "custom_attribute_prefix": fm_variables.CUSTOM_ATTRIBUTE_PREFIX,
        "tables": _tables(db, cid, on),
        "components": [{"code": code, "kind": entry.component.kind, "name": entry.version.name,
                        "stage": entry.stage, "tags": list(entry.version.tags or ())}
                       for code, entry in sorted(entries.items())],
        "tags": tags,
        "limits": {"max_length": fm.MAX_LENGTH, "max_depth": fm.MAX_DEPTH, "max_nodes": fm.MAX_NODES},
    }


# --------------------------------------------------------------------------- pack tag requirements
class _TagRecorder:
    """Stands in for a line's tags: records every tag a pack asks about and matches none."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __contains__(self, tag: object) -> bool:
        if isinstance(tag, str) and _TAG.fullmatch(tag) and tag not in self.seen:
            self.seen.append(tag)
        return False

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0


def _probe(pack: Any, method: str) -> list[str]:
    """The tags `method` reads, found by calling it with one zero earning whose tags record the lookups."""
    recorder = _TagRecorder()
    line = SimpleNamespace(component="__TAG_PROBE__", kind="EARNING", category="BASE", stage=4,
                           amount=Decimal(0), currency=pack.meta.currency, tags=recorder, statutory=False,
                           trace={}, explanation="")
    ctx = SimpleNamespace(scratch={}, period_start=pack.meta.effective_from, period_end=pack.meta.effective_from)
    try:
        getattr(pack, method)([line], ctx)
    except Exception:  # a pack that needs more context than the probe gives keeps the tags read so far
        pass
    return recorder.seen


def pack_tags(pack: Any) -> list[tuple[str, str]]:
    """(tag, purpose) pairs the pack reads from company earnings."""
    found: list[tuple[str, str]] = [(tag, STATUTORY_BASE) for tag in _probe(pack, "statutory_bases")]
    withholds = any(c.stage == pack_base.STAGE_TAX for c in pack.components())
    if withholds:
        found += [(tag, TAXABLE_INCOME) for tag in _probe(pack, "taxable_income")
                  if tag not in {t for t, _ in found}]
    return found


def _label(usage: packs.PackUsage, pack: Any) -> Optional[str]:
    if pack.meta.is_fixture:
        return pack_base.FIXTURE_LABEL
    if usage.mode != packs.VERIFIED:
        return pack_base.NOT_VERIFIED_LABEL
    return None


def _schemes(pack: Any) -> list[dict[str, Any]]:
    return [{"code": s.code, "name": s.name, "mandatory": s.mandatory} for s in pack.schemes()]


def _entity_requirements(entity: LegalEntity, on: dt.date, tagged: dict[str, list[str]]) -> dict[str, Any]:
    environment = app_settings.environment
    items = []
    for pack in packs.packs_for_country(entity.country_code, on=on, environment=environment):
        usage = packs.usage(pack.meta, environment=environment, preview=True)
        requirements = [{"tag": tag, "used_for": purpose, "components": tagged.get(tag, []),
                         "satisfied": bool(tagged.get(tag))} for tag, purpose in pack_tags(pack)]
        items.append({
            "code": pack.meta.code, "version": pack.meta.version, "name": pack.meta.name,
            "country": pack.meta.country, "status": pack.meta.status.value, "is_fixture": pack.meta.is_fixture,
            "test_only": pack.meta.test_only,
            "capability": packs.capability(pack.meta, environment=environment).value,
            "usage": {"mode": usage.mode, "reason": usage.reason, "message": usage.message},
            "verified": usage.mode == packs.VERIFIED, "label": _label(usage, pack),
            "schemes": _schemes(pack), "requirements": requirements,
            "missing": [r["tag"] for r in requirements if not r["satisfied"]],
        })
    return {
        "legal_entity": {"id": entity.id, "code": entity.code, "name": entity.legal_name,
                         "country_code": entity.country_code},
        "capability": packs.country_capability(entity.country_code, on, environment=environment).value,
        "packs": items,
    }


@router.get("/packs/tag-requirements")
def tag_requirements(legal_entity_id: Optional[int] = Query(None, ge=1),
                     principal: Principal = Depends(require(comp.READ)), db: Session = Depends(get_db)):
    cid = principal.company_id
    targets = structures._targets(principal, comp.READ)
    query = db.query(LegalEntity).filter(LegalEntity.company_id == cid)
    if legal_entity_id is not None:
        query = query.filter(LegalEntity.id == legal_entity_id)
    entities = [e for e in query.order_by(LegalEntity.code).all() if targets is None or e.id in targets]
    if legal_entity_id is not None and not entities:
        raise not_found("Legal entity")
    on = comp.today(db, cid)
    tagged: dict[str, list[str]] = {}
    for code, entry in sorted(comp.catalogue(db, cid, on).items()):
        for tag in entry.version.tags or ():
            tagged.setdefault(tag, []).append(code)
    return {"as_of": on, "items": [_entity_requirements(e, on, tagged) for e in entities]}
