"""Criteria of shift assignment rules: AssignmentCriteria plus the X-M1 team dimensions (X 01 §12.1-§12.2; D-X-09).

``team_ids`` matches when the employee has an active membership (any role) in a listed team on the date;
``team_member_roles`` matches the member role in a matching team (with ``team_ids``, or alone for "any team").
Payroll policy types keep refusing these dimensions (``pt.parse_criteria``; JC-AS-22); shift rules never take
``shift_ids`` (A-12). A rule naming only team dimensions stores ``everyone: true`` beside them: the team filter is
applied on top of the placement match.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from pydantic import ValidationError

from app.models.teams import TEAM_MEMBER_ROLES
from app.payroll import policy_types as pt

TEAM_DIMENSIONS = ("team_ids", "team_member_roles")


class ShiftCriteria(pt.AssignmentCriteria):
    team_ids: pt.IdList | None = None
    team_member_roles: pt.CodeList | None = None

    @property
    def has_team_dimensions(self) -> bool:
        return self.team_ids is not None or self.team_member_roles is not None

    def team_match(self, teams: Optional[Mapping[int, str]]) -> bool:
        """``teams`` = {team_id: member_role} of the employee on the date."""
        if not self.has_team_dimensions:
            return True
        candidates = {t: r for t, r in (teams or {}).items() if self.team_ids is None or t in self.team_ids}
        if not candidates:
            return False
        if self.team_member_roles is not None:
            return any(role in self.team_member_roles for role in candidates.values())
        return True

    def may_overlap(self, other: "pt.AssignmentCriteria") -> bool:
        if not super().may_overlap(other):
            return False
        mine, theirs = self.team_ids, getattr(other, "team_ids", None)
        return mine is None or theirs is None or bool(set(mine) & set(theirs))


def parse_shift_criteria(raw: Mapping[str, Any] | pt.AssignmentCriteria) -> ShiftCriteria:
    if isinstance(raw, ShiftCriteria):
        return raw
    if isinstance(raw, pt.AssignmentCriteria):
        raw = raw.model_dump(exclude_none=True)
    if not isinstance(raw, Mapping):
        raise pt.PolicyValidationError("INVALID_CRITERIA", "criteria must be an object")
    roles = raw.get("team_member_roles")
    if roles is not None and (not isinstance(roles, (list, tuple)) or any(r not in TEAM_MEMBER_ROLES for r in roles)):
        raise pt.PolicyValidationError("INVALID_CRITERIA", "team_member_roles: use LEAD, DEPUTY_LEAD or MEMBER",
                                       field="team_member_roles")
    data = dict(raw)
    if any(data.get(k) is not None for k in TEAM_DIMENSIONS) and not any(
            data.get(name) is not None for name, _ in pt.CRITERIA_DIMENSIONS):
        data["everyone"] = True  # team-only criteria: everyone placed, filtered by the team dimensions
    try:
        return ShiftCriteria.model_validate(data)
    except ValidationError as error:
        first = error.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise pt.PolicyValidationError(
            "INVALID_CRITERIA", f"{loc or 'criteria'}: {str(first['msg']).removeprefix('Value error, ')}",
            field=loc or None) from None
