"""Plan entitlements for white-label features (01-white-label §10).

Pricing is a business decision; this table is the product default. Tenants of a
partner get whatever that partner grants ("Partner decides"). Unknown or legacy
plan names are treated as the trial plan.
"""

from typing import FrozenSet, Mapping, Optional

PLANS = ("trial", "standard", "professional", "enterprise")

ENTITLEMENTS = {
    "branding.logo_colors": "Logo, colours and login page",
    "branding.product_name": "Custom product name",
    "branding.remove_powered_by": "Remove the platform credit",
    "domains.platform_subdomain": "Company address on the platform domain",
    "domains.custom": "Custom domain",
    "email.custom_domain": "Custom email sending domain",
    "email.template_overrides": "Edit email wording",
    "documents.entity_branding": "Document branding per legal entity",
}

_TRIAL = {"branding.logo_colors", "domains.platform_subdomain"}
_STANDARD = _TRIAL | {"email.template_overrides"}
_PROFESSIONAL = _STANDARD | {
    "branding.product_name", "branding.remove_powered_by", "domains.custom", "email.custom_domain",
}
_ENTERPRISE = _PROFESSIONAL | {"documents.entity_branding"}

PLAN_ENTITLEMENTS = {
    "trial": frozenset(_TRIAL),
    "standard": frozenset(_STANDARD),
    "professional": frozenset(_PROFESSIONAL),
    "enterprise": frozenset(_ENTERPRISE),
}

# Lowest plan that includes each entitlement (for "Available on Professional" hints).
MINIMUM_PLAN = {
    key: next(plan for plan in PLANS if key in PLAN_ENTITLEMENTS[plan]) for key in ENTITLEMENTS
}


def normalize_plan(plan: Optional[str]) -> str:
    plan = (plan or "").strip().lower()
    return plan if plan in PLAN_ENTITLEMENTS else "trial"


def entitlements_for(plan: Optional[str], partner_grants: Optional[Mapping[str, bool]] = None) -> FrozenSet[str]:
    if partner_grants is not None:
        granted = {key for key, on in partner_grants.items() if on and key in ENTITLEMENTS}
        # Every tenant may use its logo, colours and platform subdomain.
        return frozenset(granted | _TRIAL)
    return PLAN_ENTITLEMENTS[normalize_plan(plan)]
