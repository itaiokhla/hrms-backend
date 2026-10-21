"""Country-pack registry (3.2 §6, decision I-07).

Packs ship in code; the live status of each version is kept in ``country_pack_versions`` and
frozen into run snapshots. The registry answers which pack versions exist, which may be used
for a calculation and how (``usage``), the effective capability level, and the legal limits a
pack puts on company settings (``setting_constraints``, read by the settings resolver).

Usage modes:

    VERIFIED  a VERIFIED_FOR_PERIOD (or EXPIRED, for the periods it covers) non-fixture version:
              production-final statutory lines
    FIXTURE   a fixture version in development or test: lines badged "FIXTURE — not law"
    PREVIEW   a DRAFT / UNDER_REVIEW version in a preview or simulation: lines labelled
              "Compliance not verified", never finalizable
    REFUSED   anything else: no statutory lines (the engine reports STATUTORY_NOT_VERIFIED)

The fictional ``ZZ`` pack is test-only: it is invisible and refused outside the test environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from app.payroll.packs.base import (
    FIXTURE_LABEL,
    NOT_VERIFIED_LABEL,
    VERIFIED_STATUSES,
    Capability,
    CountryPack,
    PackVersion,
    TestVector,
)
from app.payroll.settings_registry import LegalConstraint
from app.payroll.state_machines import PackVersionStatus

PRODUCTION = "production"
ENVIRONMENTS = ("development", "test", "staging", "production")
FIXTURE_ENVIRONMENTS = frozenset({"development", "test"})   # staging behaves like production (fail closed)
TEST_ENVIRONMENTS = frozenset({"test"})

VERIFIED = "VERIFIED"
FIXTURE = "FIXTURE"
PREVIEW = "PREVIEW"
REFUSED = "REFUSED"


class PackUnavailable(LookupError):
    """A pack version that does not exist or may not be used here. ``code`` is stable for clients."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PackUsage:
    mode: str                       # VERIFIED | FIXTURE | PREVIEW | REFUSED
    reason: Optional[str]           # refusal or label code
    message: str
    status: PackVersionStatus

    @property
    def produces_lines(self) -> bool:
        return self.mode != REFUSED

    @property
    def final(self) -> bool:
        """Lines a run may finalize as statutory without an attestation."""
        return self.mode in (VERIFIED, FIXTURE)


_PACKS: Dict[Tuple[str, str], CountryPack] = {}


def _load() -> Dict[Tuple[str, str], CountryPack]:
    if not _PACKS:
        from app.payroll.packs import ae_draft, in_fixture, us_draft, zz_test

        for module in (zz_test, in_fixture, ae_draft, us_draft):
            pack = module.PACK
            key = (pack.code, pack.version)
            if key in _PACKS:
                raise RuntimeError(f"pack version {pack.version} is defined twice")
            _PACKS[key] = pack
    return _PACKS


def lookup(code: str, version: str) -> Optional[CountryPack]:
    """The pack version with no visibility or usage gate (the engine checks usage itself)."""
    return _load().get((code, version))


def environment_name(environment: Optional[str]) -> str:
    """Unknown or missing environments count as production (fail closed)."""
    return environment if environment in ENVIRONMENTS else PRODUCTION


def _visible(pack: CountryPack, environment: str) -> bool:
    return not pack.meta.test_only or environment_name(environment) in TEST_ENVIRONMENTS


def all_packs(*, environment: str = PRODUCTION) -> Tuple[CountryPack, ...]:
    """Every pack version visible in ``environment``, ordered by code and effective date."""
    return tuple(sorted((p for p in _load().values() if _visible(p, environment)),
                        key=lambda p: (p.code, p.meta.effective_from, p.version)))


def find_pack(code: str, version: Optional[str] = None, *, on: Optional[date] = None,
              environment: str = PRODUCTION) -> Optional[CountryPack]:
    """The pack version by exact version, or the one effective ``on`` (the latest without a date);
    no usage gate (lifecycle screens, replay checks)."""
    candidates = [p for p in all_packs(environment=environment) if p.code == code]
    if version is not None:
        return next((p for p in candidates if p.version == version), None)
    if on is not None:
        candidates = [p for p in candidates if p.meta.covers(on)]
    return candidates[-1] if candidates else None


def packs_for_country(alpha2: str, *, on: Optional[date] = None, environment: str = PRODUCTION
                      ) -> Tuple[CountryPack, ...]:
    """Pack versions for a country (national packs; subdivision rules live inside them)."""
    return tuple(p for p in all_packs(environment=environment)
                 if p.meta.country == alpha2 and (on is None or p.meta.covers(on)))


def usage(meta: PackVersion, *, environment: str = PRODUCTION, preview: bool = False,
          status: Optional[str] = None, period: Optional[Tuple[date, date]] = None) -> PackUsage:
    """How a pack version may be used for a calculation. ``status`` is the live status (from the
    snapshot or ``country_pack_versions``); the shipped status applies when it is not given."""
    env = environment_name(environment)
    try:
        live = PackVersionStatus(status) if status is not None else meta.status
    except ValueError:
        return PackUsage(REFUSED, "PACK_STATUS_UNKNOWN", f"Pack {meta.version} has an unknown status {status!r}.",
                         meta.status)

    def refuse(reason: str, message: str) -> PackUsage:
        return PackUsage(REFUSED, reason, message, live)

    if meta.test_only and env not in TEST_ENVIRONMENTS:
        return refuse("PACK_TEST_ONLY", f"Pack {meta.version} exists only for tests.")
    if live == PackVersionStatus.DISABLED:
        return refuse("PACK_DISABLED", f"Pack {meta.version} is disabled (defect found); runs are blocked.")
    if period is not None and not meta.covers_period(*period):
        return refuse("PACK_NOT_EFFECTIVE", f"Pack {meta.version} does not cover {period[0]} to {period[1]}.")
    if meta.is_fixture:
        if env in FIXTURE_ENVIRONMENTS:
            return PackUsage(FIXTURE, "PACK_FIXTURE", f"{FIXTURE_LABEL}: pack {meta.version} is test data.", live)
        return refuse("PACK_FIXTURE_IN_PRODUCTION", f"Fixture pack {meta.version} never runs in {env}.")
    if live in VERIFIED_STATUSES:
        return PackUsage(VERIFIED, None, f"Pack {meta.version} is verified for the period.", live)
    if preview:
        return PackUsage(PREVIEW, "PACK_NOT_VERIFIED",
                         f"{NOT_VERIFIED_LABEL}: pack {meta.version} is {live.value}; preview only.", live)
    return refuse("PACK_NOT_VERIFIED", f"{NOT_VERIFIED_LABEL}: pack {meta.version} is {live.value}.")


def get_pack(code: str, version: Optional[str] = None, *, on: Optional[date] = None,
             environment: str = PRODUCTION, preview: bool = False, status: Optional[str] = None) -> CountryPack:
    """A pack version that may calculate here; raises ``PackUnavailable`` otherwise. A DRAFT or
    UNDER_REVIEW version is refused outside previews; a fixture outside development and test."""
    pack = find_pack(code, version, on=on, environment=environment)
    if pack is None:
        raise PackUnavailable("PACK_NOT_FOUND", f"No pack {code}{' ' + version if version else ''} is available.")
    verdict = usage(pack.meta, environment=environment, preview=preview, status=status,
                    period=(on, on) if on is not None else None)
    if not verdict.produces_lines:
        raise PackUnavailable(verdict.reason or "PACK_REFUSED", verdict.message)
    return pack


def capability(meta: PackVersion, *, environment: str = PRODUCTION, status: Optional[str] = None) -> Capability:
    """The capability level shown next to a legal entity (3.2 §6.2). It never upgrades itself:
    verified needs a verified status; a fixture is CALC_SUPPORTED only outside production."""
    verdict = usage(meta, environment=environment, preview=True, status=status)
    if verdict.mode == VERIFIED:
        return meta.capability if meta.capability.rank >= Capability.VERIFIED.rank else Capability.VERIFIED
    calc = min(meta.capability, Capability.CALC_SUPPORTED, key=lambda c: c.rank)
    if verdict.mode == FIXTURE:
        return calc
    if verdict.mode == PREVIEW and verdict.status == PackVersionStatus.UNDER_REVIEW:
        return calc if calc.rank >= Capability.CONFIG_ONLY.rank else Capability.CONFIG_ONLY
    return Capability.CONFIG_ONLY


def country_capability(alpha2: str, on: date, *, environment: str = PRODUCTION,
                       statuses: Optional[Mapping[str, str]] = None) -> Capability:
    """The best level among the country's packs effective ``on``; a known country without a pack is
    CONFIG_ONLY (non-statutory payroll only). ``statuses`` maps version → live status."""
    packs = packs_for_country(alpha2, on=on, environment=environment)
    if not packs:
        from app.payroll import countries

        return Capability.CONFIG_ONLY if countries.find(alpha2) is not None else Capability.UNSUPPORTED
    levels = [capability(p.meta, environment=environment, status=(statuses or {}).get(p.version)) for p in packs]
    return max(levels, key=lambda c: c.rank)


def setting_constraints(country_code: str, on: date, *, environment: str = PRODUCTION,
                        statuses: Optional[Mapping[str, str]] = None) -> List[LegalConstraint]:
    """Legal limits on company settings for a legal entity in ``country_code`` on ``on``.

    A usable pack (verified, or a fixture in development/test) contributes its constraints. A pack
    that may not calculate here locks its scheme switches OFF with the reason (EC-STA-01): an
    unverified rule never constrains or enables anything. ``statuses`` maps version → live status.
    """
    result: List[LegalConstraint] = []
    for pack in packs_for_country(country_code, on=on, environment=environment):
        verdict = usage(pack.meta, environment=environment, status=(statuses or {}).get(pack.version),
                        period=(on, on))
        if verdict.final:
            result.extend(pack.setting_constraints())
            continue
        reason = f"{verdict.message} Statutory schemes stay off until a verified pack version covers the period."
        result.extend(LegalConstraint(scheme.switch_key, reason, required_value=False) for scheme in pack.schemes())
    return result


def register_settings(codes: Optional[Iterable[str]] = None, *, environment: str = PRODUCTION) -> Tuple[str, ...]:
    """Register the packs' employer choices (``payroll.statutory.<CC>.*``) in the settings registry.
    Idempotent; called at application start-up and by tests that need the keys."""
    from app.payroll.settings_registry import register_pack_settings

    wanted = None if codes is None else set(codes)
    added: List[str] = []
    seen: set = set()
    for pack in all_packs(environment=environment):
        if (wanted is not None and pack.code not in wanted) or pack.code in seen:
            continue
        seen.add(pack.code)
        added.extend(register_pack_settings(pack.employer_choices()))
    return tuple(added)


def pack_snapshot(pack: CountryPack, *, status: Optional[str] = None) -> Dict[str, Any]:
    """The pack reference a run snapshot freezes (3.5 §8.1); ``fingerprint`` detects a pack that
    changed between calculation and finalization (EC-STA-03); ``penalty_deductions_allowed`` is read
    by the late-penalty stage (EC-VAR-10)."""
    meta = pack.meta
    penalties = not any(c.target == "payroll.late_penalty.enabled" and c.required_value is False
                        for c in pack.setting_constraints())
    return {"code": meta.code, "version": meta.version, "country": meta.country,
            "status": (status or meta.status.value), "is_fixture": meta.is_fixture,
            "fingerprint": pack.fingerprint(), "penalty_deductions_allowed": penalties}


@dataclass(frozen=True)
class VectorResult:
    vector: TestVector
    passed: bool
    actual: Dict[str, str]
    issues: Tuple[str, ...]
    mismatches: Tuple[str, ...]


def run_vectors(pack: CountryPack) -> Tuple[VectorResult, ...]:
    """Run the pack's golden vectors (guard ALL_VECTORS_PASS before a version is verified)."""
    from app.payroll.engine.statutory import run_vector

    return tuple(run_vector(pack, vector) for vector in pack.test_vectors())


def manifest_problems(pack: CountryPack, *, for_verification: bool = False) -> List[str]:
    """Guard SOURCE_MANIFEST_COMPLETE (and the review fields for VERIFY): every rule named by the
    rule data or a vector has a source, and every source is complete."""
    return pack.manifest.problems(rules_used(pack), [v.id for v in pack.test_vectors()],
                                  for_verification=for_verification)


def rules_used(pack: CountryPack) -> Tuple[str, ...]:
    found: set = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            source = node.get("source")
            if isinstance(source, str) and source:
                found.add(source)
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(pack.rule_data())
    for vector in pack.test_vectors():
        found.update(vector.rules)
    return tuple(sorted(found))


__all__ = [
    "FIXTURE", "FIXTURE_ENVIRONMENTS", "PREVIEW", "PRODUCTION", "REFUSED", "TEST_ENVIRONMENTS", "VERIFIED",
    "PackUnavailable", "PackUsage", "VectorResult", "all_packs", "capability", "country_capability",
    "environment_name", "find_pack", "get_pack", "lookup", "manifest_problems", "pack_snapshot", "packs_for_country",
    "register_settings", "rules_used", "run_vectors", "setting_constraints", "usage",
]
