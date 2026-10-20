"""Brand and wording for one tenant's emails (sender name, logo, colours, overrides)."""

from typing import Optional

from sqlalchemy.orm import Session

from app.database.db import system_session
from app.models.branding import EmailTemplateOverride
from app.services import brand_service, brand_theme, domain_service, email_templates
from app.services.email_service import OutgoingEmail


def email_brand(company_id: Optional[int]) -> dict:
    brand = brand_service.effective_for_company(company_id)
    payload = brand_service.public_payload(brand, host_kind="TENANT" if company_id else "PLATFORM")
    values = brand.values
    base = domain_service.public_base_url(company_id)
    logo = payload["logos"].get("email")
    exact = (brand.tokens or {}).get("exact", {}).get("primary") or values.get("primary_color") or "#4F46E5"
    return {
        "display_name": values["display_name"],
        "product_name": values["product_name"],
        "sender_name": values.get("email_sender_name") or values["display_name"],
        "header_color": exact,
        "button_colors": brand_theme.button_colors(exact),
        "email_logo_url": f"{base}{logo}" if logo else None,
        "legal_footer": values.get("legal_footer"),
        "support_email": values.get("support_email"),
        "show_powered_by": payload["show_powered_by"],
        "powered_by_label": payload["powered_by_label"],
        "locale": payload["default_locale"],
        "dir": payload["dir"],
        "base_url": base,
    }


def load_override(db: Session, company_id: int, template_key: str, locale: str) -> Optional[dict]:
    row = (
        db.query(EmailTemplateOverride)
        .filter(EmailTemplateOverride.company_id == company_id, EmailTemplateOverride.template_key == template_key,
                EmailTemplateOverride.locale == locale)
        .first()
    )
    if row is None:
        return None
    return {"subject": row.subject, "intro": row.intro, "signature": row.signature, "version": row.version}


def compose(template_key: str, to: str, context: dict, company_id: Optional[int]) -> OutgoingEmail:
    brand = email_brand(company_id)
    full = {
        "company": {"name": brand["display_name"]},
        "product": {"name": brand["product_name"]},
        **context,
    }
    overrides = None
    if company_id is not None:
        with system_session() as db:
            # Most specific locale first (en-IN), then the language (en).
            overrides = load_override(db, company_id, template_key, brand["locale"]) or load_override(
                db, company_id, template_key, brand["locale"].split("-")[0]
            )
    rendered = email_templates.render(template_key, full, brand, overrides)
    return OutgoingEmail(
        to=to,
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
        from_name=brand["sender_name"],
        reply_to=brand.get("support_email"),
        template_key=template_key,
        company_id=company_id,
    )


def link(company_id: Optional[int], path: str) -> str:
    return f"{domain_service.public_base_url(company_id)}{path}"
