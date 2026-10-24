"""Hosts, tenant-restricted login, custom domains, handoff and branded email (01 §6, §7)."""

import email
import glob
import os
from datetime import timedelta

import pytest

from app.core.security import utcnow
from tests.helpers import PASSWORD, create_employee, invite_and_activate, signup_company
from tests.white_label_helpers import (
    PLATFORM_HOST,
    HostClient,
    platform_admin,
    set_plan,
    tenant_host,
    write_dns,
)

ACME = tenant_host("acme-corp")
BETA = tenant_host("beta-industries")


@pytest.fixture
def tenants(app):
    a = signup_company(app, "Acme Corp", "admin@acme.io")
    b = signup_company(app, "Beta Industries", "admin@beta.io")
    return a, b


def _failed_count(email_address: str) -> int:
    from app.database.db import system_session
    from app.models.user import User

    with system_session() as db:
        return db.query(User.failed_login_count).filter(User.email == email_address).scalar()


# ---------------------------------------------------------------------------
# Tenant-restricted login (EC-WL-14, EC-WL-19, EC-WL-21)
# ---------------------------------------------------------------------------
def test_wrong_tenant_host_gets_generic_error_and_no_lockout(app, tenants):
    """EC-WL-14: generic error, the other tenant's failed-login counter untouched."""
    browser = HostClient(app, ACME)
    for _ in range(7):  # more than MAX_FAILED_LOGINS
        r = browser.login("admin@beta.io")
        assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password."
    assert _failed_count("admin@beta.io") == 0
    # Beta's admin can still sign in on Beta's host and on the platform host.
    assert HostClient(app, BETA).login("admin@beta.io").status_code == 200
    assert HostClient(app, PLATFORM_HOST).login("admin@beta.io").status_code == 200
    # Same response as a wrong password for an unknown account.
    assert browser.login("nobody@nowhere.io").json()["detail"] == "Invalid email or password."
    assert HostClient(app, ACME).login("admin@acme.io").status_code == 200


def test_unknown_host_cannot_sign_in_and_forged_host_is_platform(app, tenants):
    """EC-WL-19 and EC-WL-21."""
    assert HostClient(app, "evil.example.com").login("admin@acme.io").status_code == 401
    # Without the proxy key the host header is ignored: the request counts as the platform host.
    forged = HostClient(app, BETA, proxy_key=None)
    assert forged.login("admin@acme.io").status_code == 200


def test_session_is_bound_to_its_host(app, tenants):
    acme = HostClient(app, ACME)
    assert acme.login("admin@acme.io").status_code == 200
    assert acme.get("/api/v1/auth/me").status_code == 200
    # The same cookies presented on Beta's host (or another host) are refused.
    stolen = HostClient(app, BETA)
    stolen.http.cookies = acme.http.cookies
    assert stolen.get("/api/v1/auth/me").status_code == 401
    other_host_same_tenant = HostClient(app, PLATFORM_HOST)
    other_host_same_tenant.http.cookies = acme.http.cookies
    assert other_host_same_tenant.get("/api/v1/auth/me").status_code == 401
    assert other_host_same_tenant.post("/api/v1/auth/refresh").status_code == 401
    assert acme.post("/api/v1/auth/refresh").status_code == 200


def test_mfa_token_is_bound_to_host(app, tenants):
    import pyotp

    acme = HostClient(app, ACME)
    acme.login("admin@acme.io")
    secret = acme.post("/api/v1/auth/mfa/setup").json()["secret"]
    assert acme.post("/api/v1/auth/mfa/enable", json={"code": pyotp.TOTP(secret).now()}).status_code == 200
    first = HostClient(app, ACME).login("admin@acme.io").json()
    assert first["mfa_required"]
    import time

    elsewhere = HostClient(app, PLATFORM_HOST).post(
        "/api/v1/auth/login/mfa", json={"mfa_token": first["mfa_token"], "code": pyotp.TOTP(secret).at(time.time() + 30)})
    assert elsewhere.status_code == 401 and elsewhere.json()["code"] == "MFA_EXPIRED"


def test_platform_console_only_on_platform_host(app, tenants):
    ops = platform_admin(app)
    assert ops.get("/api/v1/platform/companies").status_code == 200
    on_tenant = HostClient(app, ACME)
    on_tenant.http.cookies = ops.http.cookies
    assert on_tenant.get("/api/v1/platform/companies").status_code == 404
    assert on_tenant.post("/api/v1/platform/auth/login", json={"email": "ops@platform.io", "password": "Platf0rmPass!"}).status_code == 404
    # Signup only on the platform host.
    r = HostClient(app, ACME).post("/api/v1/auth/signup", json={
        "company_name": "Sneaky Corp", "full_name": "Sneaky Person", "email": "s@sneaky.io", "password": PASSWORD})
    assert r.status_code == 404


def test_reset_and_invitation_are_tenant_restricted(app, tenants):
    a, b = tenants
    # Reset requested on Beta's host for an Acme user: same message, no code created.
    r = HostClient(app, BETA).post("/api/v1/auth/forgot-password", json={"email": "admin@acme.io"})
    assert r.status_code == 200
    from app.database.db import system_session
    from app.models.user import PasswordResetOTP

    with system_session() as db:
        assert db.query(PasswordResetOTP).count() == 0
    emp = create_employee(a, "Invitee")
    r = a.post("/api/v1/users/invite", json={"email": emp["email"], "full_name": "Invitee", "employee_id": emp["id"], "role_ids": []})
    invite_url = r.json()["invite_url"]
    # Links come from the verified primary host in the database, never the request Host.
    assert invite_url.startswith(f"http://{ACME}:3000/accept-invite?token=")
    token = invite_url.split("token=")[1]
    assert HostClient(app, BETA).get(f"/api/v1/auth/invitations/{token}").status_code == 400
    assert HostClient(app, ACME).get(f"/api/v1/auth/invitations/{token}").status_code == 200


def test_forged_host_header_never_changes_links(app, tenants):
    """EC-WL-19: links use only the verified primary host from the database."""
    a, _ = tenants
    emp = create_employee(a, "Victim")
    r = a.post("/api/v1/users/invite", headers={"Host": "evil.example.com", "X-Forwarded-Host": "evil.example.com",
                                                 "X-HRMS-Host": "evil.example.com"},
               json={"email": emp["email"], "full_name": "Victim", "employee_id": emp["id"], "role_ids": []})
    assert r.status_code == 200
    assert "evil" not in r.json()["invite_url"]


# ---------------------------------------------------------------------------
# Custom domains (EC-WL-16, EC-WL-17, EC-WL-18)
# ---------------------------------------------------------------------------
def _domain_records(domain: dict, cname: str = "tenants.test.local") -> dict:
    txt = next(r for r in domain["records"] if r["type"] == "TXT")
    return {txt["name"]: {"TXT": [txt["value"]]}, domain["hostname"]: {"CNAME": cname}}


def test_custom_domain_validation_and_entitlement(app, tenants):
    """EC-WL-16: claimed, reserved, platform and public-suffix domains are rejected with the reason."""
    a, b = tenants
    assert a.post("/api/v1/domains", json={"hostname": "hr.acme.com"}).status_code == 403  # trial plan
    ops = platform_admin(app)
    set_plan(ops, a.company_id, "professional")
    set_plan(ops, b.company_id, "professional")
    for bad, code in [
        ("https://hr.acme.com/path", "DOMAIN_INVALID"), ("10.0.0.1", "DOMAIN_INVALID"), ("localhost", "DOMAIN_INVALID"),
        ("app.test.local", "DOMAIN_PLATFORM"), ("x.test.local", "DOMAIN_PLATFORM"), ("co.uk", "DOMAIN_PUBLIC_SUFFIX"),
        ("www.com", "DOMAIN_RESERVED"),
    ]:
        r = a.post("/api/v1/domains", json={"hostname": bad})
        assert r.status_code == 400 and r.json()["code"] == code, (bad, r.text)
    r = a.post("/api/v1/domains", json={"hostname": "HR.Acme.COM."})
    assert r.status_code == 200 and r.json()["hostname"] == "hr.acme.com"
    idn = a.post("/api/v1/domains", json={"hostname": "hr.bücher.de"}).json()
    assert idn["hostname"] == "hr.xn--bcher-kva.de" and idn["unicode_hostname"] == "hr.bücher.de"
    # Another tenant cannot claim the same host (same message whoever holds it).
    r = b.post("/api/v1/domains", json={"hostname": "hr.acme.com"})
    assert r.status_code == 409 and r.json()["code"] == "DOMAIN_TAKEN"


def test_domain_lifecycle_dns_tls_broken_disabled(app, tenants):
    """EC-WL-17: DNS_BROKEN, DISABLED after 14 days, sessions revoked, neutral page."""
    a, _ = tenants
    ops = platform_admin(app)
    set_plan(ops, a.company_id, "professional")
    domain = a.post("/api/v1/domains", json={"hostname": "people.acme.com"}).json()
    assert domain["status"] == "PENDING_DNS"
    write_dns({})
    assert a.post(f"/api/v1/domains/{domain['id']}/verify").json()["status"] == "PENDING_DNS"
    # Wrong CNAME (dangling record pointing elsewhere) is not enough.
    write_dns(_domain_records(domain, cname="other.example.net"))
    checked = a.post(f"/api/v1/domains/{domain['id']}/verify").json()
    assert checked["status"] == "PENDING_DNS" and "CNAME" in checked["last_error"]
    write_dns(_domain_records(domain))
    active = a.post(f"/api/v1/domains/{domain['id']}/verify").json()
    assert active["status"] == "ACTIVE" and active["tls_status"] == "ISSUED" and active["tls_mode"] == "SIMULATED"

    host = "people.acme.com"
    brand = HostClient(app, host).get("/api/v1/public/brand").json()
    assert brand["host_kind"] == "TENANT" and brand["display_name"] == "Acme Corp"
    browser = HostClient(app, host)
    assert browser.login("admin@acme.io").status_code == 200
    assert a.post(f"/api/v1/domains/{domain['id']}/primary").status_code == 200
    emp = create_employee(a, "Later")
    link = a.post("/api/v1/users/invite", json={"email": emp["email"], "full_name": "Later Person", "employee_id": emp["id"],
                                               "role_ids": []}).json()["invite_url"]
    assert link.startswith("http://people.acme.com:3000/")

    # DNS breaks: DNS_BROKEN, admins notified; after 14 days DISABLED and sessions on the host revoked.
    write_dns({})
    broken = a.post(f"/api/v1/domains/{domain['id']}/verify").json()
    assert broken["status"] == "DNS_BROKEN"
    assert browser.get("/api/v1/auth/me").status_code == 200  # still served while broken
    from app.database.db import system_session
    from app.models.branding import TenantDomain

    with system_session() as db:
        row = db.get(TenantDomain, domain["id"])
        row.dns_broken_since = utcnow() - timedelta(days=15)
        db.commit()
    from app.services import domain_service

    domain_service.run_checks()
    assert a.get(f"/api/v1/domains/{domain['id']}").json()["status"] == "DISABLED"
    assert browser.get("/api/v1/auth/me").status_code == 401
    assert HostClient(app, host).get("/api/v1/public/brand").json()["host_kind"] == "UNKNOWN"
    # The platform subdomain became primary again.
    assert any(d["is_primary"] and d["kind"] == "PLATFORM_SUBDOMAIN" for d in a.get("/api/v1/domains").json()["domains"])


def test_domain_removal_revokes_sessions_and_needs_fresh_verification(app, tenants):
    """EC-WL-18: removal revokes the host's sessions; re-adding needs fresh verification."""
    a, b = tenants
    ops = platform_admin(app)
    set_plan(ops, a.company_id, "professional")
    set_plan(ops, b.company_id, "professional")
    domain = a.post("/api/v1/domains", json={"hostname": "hr.acme.com"}).json()
    write_dns(_domain_records(domain))
    a.post(f"/api/v1/domains/{domain['id']}/verify")
    browser = HostClient(app, "hr.acme.com")
    assert browser.login("admin@acme.io").status_code == 200
    assert a.delete(f"/api/v1/domains/{domain['id']}").status_code == 200
    assert browser.get("/api/v1/auth/me").status_code == 401
    # Takeover attempt: the old TXT record does not verify a new claim by Beta.
    claim = b.post("/api/v1/domains", json={"hostname": "hr.acme.com"}).json()
    assert claim["status"] == "PENDING_DNS"
    assert b.post(f"/api/v1/domains/{claim['id']}/verify").json()["status"] == "PENDING_DNS"


def test_pending_domain_fails_after_window(app, tenants):
    a, _ = tenants
    ops = platform_admin(app)
    set_plan(ops, a.company_id, "professional")
    domain = a.post("/api/v1/domains", json={"hostname": "late.acme.com"}).json()
    from app.database.db import system_session
    from app.models.branding import TenantDomain

    with system_session() as db:
        db.get(TenantDomain, domain["id"]).created_at = utcnow() - timedelta(hours=73)
        db.commit()
    write_dns({})
    assert a.post(f"/api/v1/domains/{domain['id']}/verify").json()["status"] == "FAILED"


# ---------------------------------------------------------------------------
# Handoff to an enforced custom domain (EC-WL-15)
# ---------------------------------------------------------------------------
def test_login_handoff_single_use_bound_to_host_and_agent(app, tenants):
    """EC-WL-15: handoff code exchanged on the primary host; replayed or expired codes are rejected."""
    a, _ = tenants
    ops = platform_admin(app)
    set_plan(ops, a.company_id, "professional")
    domain = a.post("/api/v1/domains", json={"hostname": "hr.acme.com"}).json()
    write_dns(_domain_records(domain))
    a.post(f"/api/v1/domains/{domain['id']}/verify")
    a.post(f"/api/v1/domains/{domain['id']}/primary")
    assert a.patch(f"/api/v1/domains/{domain['id']}", json={"enforce_custom_domain": True}).status_code == 200

    platform_browser = HostClient(app, PLATFORM_HOST, user_agent="Chrome-test")
    r = platform_browser.login("admin@acme.io")
    body = r.json()
    assert r.status_code == 200 and "handoff_url" in body and "me" not in body
    assert "hrms_access" not in platform_browser.http.cookies  # no session on the platform host
    url = body["handoff_url"]
    assert url.startswith("http://hr.acme.com:3000/auth/handoff#code=")
    code = url.split("code=")[1]

    assert HostClient(app, "acme-corp.test.local", user_agent="Chrome-test").post(
        "/api/v1/auth/handoff", json={"code": code}).status_code == 401  # wrong host
    assert HostClient(app, "hr.acme.com", user_agent="Other-agent").post(
        "/api/v1/auth/handoff", json={"code": code}).status_code == 401  # wrong user agent
    target = HostClient(app, "hr.acme.com", user_agent="Chrome-test")
    ok = target.post("/api/v1/auth/handoff", json={"code": code})
    assert ok.status_code == 200 and ok.json()["me"]["company"]["id"] == a.company_id
    assert target.get("/api/v1/auth/me").status_code == 200
    replay = HostClient(app, "hr.acme.com", user_agent="Chrome-test").post("/api/v1/auth/handoff", json={"code": code})
    assert replay.status_code == 401  # single use

    # Expired code.
    second = HostClient(app, PLATFORM_HOST, user_agent="Chrome-test").login("admin@acme.io").json()["handoff_url"]
    from app.database.db import system_session
    from app.models.branding import AuthHandoffCode

    with system_session() as db:
        db.query(AuthHandoffCode).update({AuthHandoffCode.expires_at: utcnow() - timedelta(seconds=1)})
        db.commit()
    expired = HostClient(app, "hr.acme.com", user_agent="Chrome-test").post(
        "/api/v1/auth/handoff", json={"code": second.split("code=")[1]})
    assert expired.status_code == 401


# ---------------------------------------------------------------------------
# Branded email (EC-WL-24, EC-WL-25)
# ---------------------------------------------------------------------------
def _outbox(company_id: int):
    folder = os.path.join(os.environ["EMAIL_OUTBOX_DIR"], str(company_id))
    return sorted(glob.glob(os.path.join(folder, "*.eml")), key=os.path.getmtime)


def test_invitation_email_is_branded_with_safe_links(app, tenants):
    a, _ = tenants
    a.put("/api/v1/branding/draft", json={"changes": {"display_name": "Acme Group", "support_email": "help@acme.io",
                                                      "legal_footer": "Acme Group · 12 MG Road"}})
    a.post("/api/v1/branding/publish", json={})
    before = set(_outbox(a.company_id))
    emp = create_employee(a, "Mailee")
    a.post("/api/v1/users/invite", json={"email": emp["email"], "full_name": "Mailee Test", "employee_id": emp["id"], "role_ids": []})
    new = [p for p in _outbox(a.company_id) if p not in before]
    assert len(new) == 1
    with open(new[0], "rb") as handle:
        message = email.message_from_bytes(handle.read())
    assert "Acme Group" in message["From"] and message["Reply-To"] == "help@acme.io"
    assert "Acme Group" in message["Subject"]
    parts = {part.get_content_type(): part.get_payload(decode=True).decode() for part in message.walk() if not part.is_multipart()}
    assert set(parts) == {"text/plain", "text/html"}
    assert f"http://{ACME}:3000/accept-invite?token=" in parts["text/plain"]
    assert "12 MG Road" in parts["text/html"] and "<script" not in parts["text/html"]


def test_template_overrides_are_sandboxed_and_allow_listed(app, tenants):
    """EC-WL-24: an unknown placeholder blocks saving."""
    a, _ = tenants
    ops = platform_admin(app)
    assert a.put("/api/v1/email-templates/auth.invite", json={"intro": "Hi"}).status_code == 403  # trial plan
    set_plan(ops, a.company_id, "standard")
    for bad in ("Hi {{ recipient.password }}", "{% for x in company %}x{% endfor %}", "{{ company.name|upper }}",
                "{{ ''.__class__ }}", "{{ company['name'] }}", "{{ invite.url() }}", "{{ 7*7 }}"):
        r = a.put("/api/v1/email-templates/auth.invite", json={"intro": bad})
        assert r.status_code == 400, (bad, r.text)
    r = a.put("/api/v1/email-templates/auth.invite", json={"intro": "Hi {{ recipient.first_name }}, welcome to {{ company.name }}! <b>"})
    assert r.status_code == 200, r.text
    # Literal text typed by the tenant is escaped too, so it can never inject HTML.
    injected = a.post("/api/v1/email-templates/auth.invite/preview",
                      json={"intro": '<a href="https://evil.example">Verify</a> {{ company.name }}'}).json()
    assert "<a href=\"https://evil.example\"" not in injected["html"] and "&lt;a href=" in injected["html"]
    preview = a.post("/api/v1/email-templates/auth.invite/preview", json={"intro": "Hi {{ recipient.first_name }} <b>"}).json()
    assert "Hi Alex &lt;b&gt;" in preview["html"]  # autoescaped
    r = a.put("/api/v1/email-templates/auth.invite", json={"subject": "Hello {{ unknown.field }}"})
    assert r.status_code == 400 and r.json()["code"] == "EMAIL_TEMPLATE_PLACEHOLDER"


def test_broken_override_at_send_time_falls_back(app, tenants):
    """EC-WL-24: a render failure at send time uses the default template."""
    from app.services import email_templates

    brand = {"display_name": "Acme", "product_name": "HRMS", "show_powered_by": False}
    context = email_templates.sample_context("auth.invite", "Acme", "HRMS")
    rendered = email_templates.render("auth.invite", context, brand, {"intro": "{{ missing.value }}"})
    assert rendered.used_fallback and "you have been invited" in rendered.text


def test_payslip_email_never_contains_amounts():
    """EC-WL-25: no amounts by default (the ON case of payslip.email_include_net_pay is asserted by
    tests/payroll/test_publication.py::test_email_with_net_pay_and_ec_doc_14_attachment_only_with_pdf_protection)."""
    from app.services import email_templates

    template = email_templates.TEMPLATES["payroll.payslip_published"]
    assert not any("salary" in p or "amount" in p or "net" in p for p in template.placeholders)
    context = email_templates.sample_context("payroll.payslip_published", "Acme", "HRMS")
    text = email_templates.render("payroll.payslip_published", context, {"display_name": "Acme", "product_name": "HRMS"}).text
    assert "₹" not in text and "does not include amounts" in text


def test_domain_moved_to_another_tenant_never_shows_the_previous_brand(app, tenants):
    """EC-WL-18: a removed domain shows the neutral page."""
    a, b = tenants
    ops = platform_admin(app)
    set_plan(ops, a.company_id, "professional")
    set_plan(ops, b.company_id, "professional")
    a.put("/api/v1/branding/draft", json={"changes": {"tagline": "Acme on its domain"}})
    a.post("/api/v1/branding/publish", json={})
    first = a.post("/api/v1/domains", json={"hostname": "hr.shared-example.com"}).json()
    write_dns(_domain_records(first))
    a.post(f"/api/v1/domains/{first['id']}/verify")
    assert HostClient(app, "hr.shared-example.com").get("/api/v1/public/brand").json()["tagline"] == "Acme on its domain"
    a.delete(f"/api/v1/domains/{first['id']}")
    assert HostClient(app, "hr.shared-example.com").get("/api/v1/public/brand").json()["host_kind"] == "UNKNOWN"
    second = b.post("/api/v1/domains", json={"hostname": "hr.shared-example.com"}).json()
    write_dns(_domain_records(second))
    assert b.post(f"/api/v1/domains/{second['id']}/verify").json()["status"] == "ACTIVE"
    brand = HostClient(app, "hr.shared-example.com").get("/api/v1/public/brand").json()
    assert brand["display_name"] == "Beta Industries" and brand["tagline"] != "Acme on its domain"
    assert HostClient(app, "hr.shared-example.com").login("admin@acme.io").status_code == 401
    assert HostClient(app, "hr.shared-example.com").login("admin@beta.io").status_code == 200
