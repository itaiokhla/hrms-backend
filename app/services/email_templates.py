"""Branded transactional email templates (01-white-label §5.6, §7.3).

Every email uses one shared layout (HTML plus a plain-text alternative): the tenant's
email logo on a white header above a strip in the exact brand colour (a deviation from
01 §5.6's coloured header bar: logos are designed for light backgrounds and vanished on
a bar of their own colour), the content, then a footer with the legal footer, support
contact and the optional "Powered by" line.

Tenants may change the subject, intro paragraph and signature per template and
locale, never the layout. Their text is rendered in a sandboxed Jinja2 environment
with autoescaping, and may contain only plain ``{{ name.attr }}`` placeholders from
the template's allow-list: no filters, calls, loops, conditions or index access. A
template that fails at send time falls back to the default wording; a broken
template never blocks an email.

Privacy: no template carries salary amounts, bank details or tax identifiers.
"""

import html
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from jinja2 import StrictUndefined, TemplateSyntaxError, nodes
from jinja2.sandbox import SandboxedEnvironment

from app.core.errors import bad_request

logger = logging.getLogger(__name__)

_env = SandboxedEnvironment(autoescape=True, undefined=StrictUndefined)
_text_env = SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)
_ALLOWED_NODES = (nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name, nodes.Getattr)

COMMON = ("company.name", "product.name", "recipient.first_name", "recipient.name")


@dataclass(frozen=True)
class TemplateDef:
    key: str
    label: str
    subject: str
    intro: str
    signature: str
    cta_label: Optional[str]
    placeholders: Tuple[str, ...]
    sample: Dict[str, dict] = field(default_factory=dict)
    # Fixed content after the intro (not editable by tenants), e.g. a one-time code.
    detail: Optional[str] = None


TEMPLATES: Dict[str, TemplateDef] = {
    t.key: t
    for t in (
        TemplateDef(
            key="auth.invite",
            label="Invitation",
            subject="You're invited to {{ company.name }}",
            intro="Hello {{ recipient.first_name }}, you have been invited to join {{ company.name }} on "
                  "{{ product.name }}. Set your password to get started.",
            signature="The {{ company.name }} team",
            cta_label="Accept invitation",
            placeholders=COMMON + ("invite.expires_hours",),
            detail="This link expires in {{ invite.expires_hours }} hours. If you were not expecting this "
                   "invitation, you can ignore this email.",
            sample={"invite": {"expires_hours": 72, "url": "https://example.invalid/accept-invite"}},
        ),
        TemplateDef(
            key="auth.password_reset",
            label="Password reset code",
            subject="Your {{ product.name }} verification code",
            intro="Hello {{ recipient.first_name }}, use this code to reset your password.",
            signature="The {{ company.name }} team",
            cta_label=None,
            placeholders=COMMON + ("reset.expires_minutes",),
            detail="Code: {{ reset.code }}\nIt expires in {{ reset.expires_minutes }} minutes. If you did not "
                   "request a password reset, you can ignore this email.",
            sample={"reset": {"code": "123456", "expires_minutes": 10}},
        ),
        TemplateDef(
            key="payroll.payslip_published",
            label="Payslip published",
            subject="Your payslip for {{ period.label }} is ready",
            intro="Hello {{ recipient.first_name }}, your payslip for {{ period.label }} is now available in "
                  "{{ product.name }}.",
            signature="{{ company.name }} Payroll",
            cta_label="View payslip",
            placeholders=COMMON + ("period.label",),
            detail="For your privacy this email does not include amounts. Sign in to view and download your payslip.",
            sample={"period": {"label": "October 2026"}, "link": {"url": "https://example.invalid/payslips"}},
        ),
        TemplateDef(
            key="payroll.approval_requested",
            label="Payroll approval requested",
            subject="Payroll for {{ period.label }} needs your approval",
            intro="Hello {{ recipient.first_name }}, payroll for {{ period.label }} at {{ company.name }} is "
                  "waiting for your approval.",
            signature="{{ product.name }}",
            cta_label="Review payroll",
            placeholders=COMMON + ("period.label",),
            sample={"period": {"label": "October 2026"}, "link": {"url": "https://example.invalid/payroll"}},
        ),
    )
}

EDITABLE_PARTS = ("subject", "intro", "signature")
PART_LIMITS = {"subject": 200, "intro": 2000, "signature": 500}


def _placeholders(source: str) -> Tuple[str, ...]:
    """Dotted placeholders used by `source`; raises ValueError on anything but plain placeholders."""
    tree = _env.parse(source)
    found = []
    for node in tree.find_all(nodes.Node):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"Only {{{{ placeholders }}}} are allowed (found {type(node).__name__.lower()})")
    for output in tree.find_all(nodes.Output):
        for child in output.nodes:
            if isinstance(child, nodes.TemplateData):
                continue
            parts = []
            current = child
            while isinstance(current, nodes.Getattr):
                parts.append(current.attr)
                current = current.node
            if not isinstance(current, nodes.Name):
                raise ValueError("Only {{ placeholders }} are allowed")
            parts.append(current.name)
            found.append(".".join(reversed(parts)))
    return tuple(found)


def validate_override(template_key: str, part: str, source: Optional[str]) -> Optional[str]:
    """Reject unknown placeholders, logic or oversized text before an override is saved (EC-WL-24)."""
    template = TEMPLATES.get(template_key)
    if template is None:
        raise bad_request("Unknown email template.", "EMAIL_TEMPLATE_UNKNOWN")
    if source is None or not source.strip():
        return None
    source = source.strip()
    if len(source) > PART_LIMITS[part]:
        raise bad_request(f"The {part} can be at most {PART_LIMITS[part]} characters.", "EMAIL_TEMPLATE_INVALID")
    if part == "subject" and ("\n" in source or "\r" in source):
        raise bad_request("The subject must be a single line.", "EMAIL_TEMPLATE_INVALID")
    try:
        used = _placeholders(source)
    except (TemplateSyntaxError, ValueError) as exc:
        raise bad_request(f"The {part} is not valid: {exc}", "EMAIL_TEMPLATE_INVALID")
    unknown = sorted(set(used) - set(template.placeholders))
    if unknown:
        allowed = ", ".join("{{ " + p + " }}" for p in template.placeholders)
        raise bad_request(
            f"Unknown placeholder {', '.join('{{ ' + u + ' }}' for u in unknown)}. Allowed: {allowed}",
            "EMAIL_TEMPLATE_PLACEHOLDER",
        )
    return source


def _render(source: str, context: dict, *, escape: bool) -> str:
    if escape:
        # Autoescaping covers placeholder values only; the literal text around them is escaped
        # here, so wording typed by a tenant can never become HTML in an email.
        return _env.from_string(html.escape(source, quote=False)).render(**context)
    return _text_env.from_string(source).render(**context)


@dataclass
class RenderedEmail:
    subject: str
    html: str
    text: str
    used_fallback: bool = False


def render(template_key: str, context: dict, brand: dict, overrides: Optional[dict] = None) -> RenderedEmail:
    """Render a template with the brand. `overrides` may hold subject/intro/signature."""
    template = TEMPLATES[template_key]
    parts = {name: getattr(template, name) for name in EDITABLE_PARTS}
    used_fallback = False
    if overrides:
        try:
            custom = {name: overrides[name] for name in EDITABLE_PARTS if overrides.get(name)}
            for name, source in custom.items():
                _render(source, context, escape=True)  # must render with this context
            parts.update(custom)
        except Exception:
            logger.exception("Email template override %s failed to render; using the default wording", template_key)
            used_fallback = True
    subject = " ".join(_render(parts["subject"], context, escape=False).split())[:200]
    intro_html = _render(parts["intro"], context, escape=True)
    intro_text = _render(parts["intro"], context, escape=False)
    signature_html = _render(parts["signature"], context, escape=True)
    signature_text = _render(parts["signature"], context, escape=False)
    detail_html = _render(template.detail, context, escape=True) if template.detail else ""
    detail_text = _render(template.detail, context, escape=False) if template.detail else ""
    link = (context.get("link") or {}).get("url") or (context.get("invite") or {}).get("url")
    return RenderedEmail(
        subject=subject,
        html=_layout_html(brand, intro_html, detail_html, signature_html, template.cta_label, link),
        text=_layout_text(brand, intro_text, detail_text, signature_text, template.cta_label, link),
        used_fallback=used_fallback,
    )


def _layout_html(brand: dict, intro: str, detail: str, signature: str, cta: Optional[str], link: Optional[str]) -> str:
    e = html.escape
    colour = brand.get("header_color") or "#4F46E5"
    button_bg, button_fg = brand.get("button_colors") or (colour, "#FFFFFF")
    logo = brand.get("email_logo_url")
    # Logos are made for light backgrounds (01 §5.2), so the header is white with a strip of the
    # exact brand colour below it, like the payslip; the name falls back to text in the button colour.
    header = (
        f'<img src="{e(logo)}" alt="{e(brand["display_name"])}" height="40" style="display:block;max-height:40px;border:0">'
        if logo else f'<span style="font-size:20px;font-weight:700;color:{button_bg}">{e(brand["display_name"])}</span>'
    )
    detail_block = "".join(
        f'<p dir="auto" style="margin:0 0 12px;color:#374151;font-size:14px;line-height:1.6">{line}</p>'
        for line in detail.split("\n") if line.strip()
    )
    button = (
        f'<p style="margin:24px 0"><a href="{e(link)}" style="background:{button_bg};color:{button_fg};'
        f'padding:12px 22px;border-radius:10px;text-decoration:none;font-weight:600;display:inline-block">{e(cta)}</a></p>'
        f'<p style="margin:0 0 12px;color:#6B7280;font-size:12px;word-break:break-all">{e(link)}</p>'
        if cta and link else ""
    )
    footer_lines = [e(brand["legal_footer"])] if brand.get("legal_footer") else []
    if brand.get("support_email"):
        footer_lines.append(f'Questions? <a href="mailto:{e(brand["support_email"])}" style="color:#6B7280">{e(brand["support_email"])}</a>')
    if brand.get("show_powered_by"):
        footer_lines.append(e(brand.get("powered_by_label") or ""))
    footer = "<br>".join(line for line in footer_lines if line)
    direction = brand.get("dir") or "ltr"
    return f"""<!doctype html>
<html lang="{e(brand.get('locale') or 'en')}" dir="{direction}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{e(brand['display_name'])}</title></head>
<body style="margin:0;padding:0;background:#F3F4F6;font-family:'Segoe UI',Roboto,'Noto Sans',Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#F3F4F6;padding:24px 0"><tr><td align="center">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#FFFFFF;border-radius:14px;overflow:hidden">
<tr><td style="background:#FFFFFF;padding:20px 28px;border-bottom:4px solid {colour}">{header}</td></tr>
<tr><td style="padding:28px">
<p dir="auto" style="margin:0 0 16px;color:#111827;font-size:15px;line-height:1.6">{intro}</p>
{detail_block}{button}
<p dir="auto" style="margin:24px 0 0;color:#111827;font-size:14px">{signature}</p>
</td></tr>
<tr><td style="padding:18px 28px;background:#F9FAFB;color:#6B7280;font-size:12px;line-height:1.6">{footer}</td></tr>
</table></td></tr></table></body></html>"""


def _layout_text(brand: dict, intro: str, detail: str, signature: str, cta: Optional[str], link: Optional[str]) -> str:
    lines = [brand["display_name"], "", intro.strip(), ""]
    if detail:
        lines += [detail.strip(), ""]
    if cta and link:
        lines += [f"{cta}: {link}", ""]
    lines += [signature.strip(), "", "--"]
    if brand.get("legal_footer"):
        lines.append(brand["legal_footer"])
    if brand.get("support_email"):
        lines.append(f"Questions? {brand['support_email']}")
    if brand.get("show_powered_by"):
        lines.append(brand.get("powered_by_label") or "")
    return "\n".join(lines).strip() + "\n"


def sample_context(template_key: str, company_name: str, product_name: str) -> dict:
    template = TEMPLATES[template_key]
    context = {
        "company": {"name": company_name},
        "product": {"name": product_name},
        "recipient": {"first_name": "Alex", "name": "Alex Morgan", "email": "alex@example.com"},
    }
    context.update({k: dict(v) for k, v in template.sample.items()})
    return context
