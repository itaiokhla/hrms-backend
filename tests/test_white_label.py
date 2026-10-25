"""White label: theme derivation, image pipeline, brand lifecycle, partner locks, entitlements, public brand."""

import io
import random
import threading

import pytest
from PIL import Image

from app.services import brand_theme
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.white_label_helpers import (
    PLATFORM_HOST,
    HostClient,
    animated_gif,
    animated_png,
    cmyk_jpeg,
    huge_png_header,
    jpeg_with_gps,
    platform_admin,
    png,
    set_plan,
    tenant_host,
    upload,
)


# ---------------------------------------------------------------------------
# Theme derivation (EC-WL-03, EC-WL-04)
# ---------------------------------------------------------------------------
def test_every_colour_ends_with_aa_contrast():
    random.seed(7)
    for _ in range(1500):
        primary = "#%06X" % random.randint(0, 0xFFFFFF)
        accent = "#%06X" % random.randint(0, 0xFFFFFF)
        for style in ("DARK", "LIGHT", "BRAND"):
            tokens = brand_theme.derive_tokens(primary, accent, style)
            assert all(c["pass"] for c in tokens["contrast_report"]), (primary, style, tokens["contrast_report"])
            assert brand_theme.tokens_are_safe(tokens)


def test_low_contrast_colour_is_adjusted_and_exact_colour_kept():
    """EC-WL-03: the UI primary is darkened until text passes 4.5:1, the exact colour is kept and a warning is shown."""
    tokens = brand_theme.derive_tokens("#F5C518")  # yellow: white text impossible
    assert tokens["light"]["primary-foreground"] == "224 32% 12%"  # dark text instead
    assert tokens["exact"]["primary"] == "#F5C518"
    mid = brand_theme.derive_tokens("#3B82F6")  # 3.7:1 with white: darkened until text passes
    report = mid["contrast_report"][0]
    assert report["adjusted"] and report["ratio"] >= 4.5
    assert any("adjusted" in w for w in mid["warnings"])


def test_brand_hue_near_error_colour_warns():
    """EC-WL-04: a brand hue near the error colour only warns."""
    tokens = brand_theme.derive_tokens("#E11D48")
    assert any("close to the error colour" in w for w in tokens["warnings"])
    assert not any("error colour" in w for w in brand_theme.derive_tokens("#0F766E")["warnings"])


def test_status_tokens_are_never_derived():
    tokens = brand_theme.derive_tokens("#DC2626")
    for mode in ("light", "dark"):
        assert not set(tokens[mode]) & {"danger", "success", "warning", "info"}


# ---------------------------------------------------------------------------
# Image pipeline (EC-WL-01, EC-WL-06)
# ---------------------------------------------------------------------------
@pytest.fixture
def admin(app):
    return signup_company(app, "Acme Corp")


@pytest.mark.parametrize(
    "kind,data,name,ctype,code",
    [
        ("logo_light", b"not an image at all", "x.png", "image/png", "BRAND_ASSET_INVALID"),
        ("logo_light", png()[:60], "cut.png", "image/png", "BRAND_ASSET_INVALID"),
        ("logo_light", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "x.svg",
         "image/svg+xml", "BRAND_ASSET_FORMAT"),
        ("mark", animated_png(), "anim.png", "image/png", "BRAND_ASSET_ANIMATED"),
        ("logo_light", animated_gif(), "anim.gif", "image/gif", "BRAND_ASSET_FORMAT"),
        ("logo_light", huge_png_header(), "bomb.png", "image/png", "BRAND_ASSET_TOO_MANY_PIXELS"),
        ("mark", png(100, 100), "small.png", "image/png", "BRAND_ASSET_TOO_SMALL"),
        ("logo_light", png(400, 20), "flat.png", "image/png", "BRAND_ASSET_TOO_SMALL"),
        ("email_logo", b"\x89PNG" + b"0" * (1024 * 1024 + 10), "big.png", "image/png", "BRAND_ASSET_TOO_LARGE"),
    ],
    ids=["not-image", "truncated", "svg-script", "animated-png", "gif", "decompression-bomb", "mark-too-small",
         "logo-too-flat", "too-large"],
)
def test_bad_uploads_are_rejected(admin, kind, data, name, ctype, code):
    """EC-WL-01 (wrong type, truncated, animated, too large, too many pixels) and EC-WL-06 (phase 1: an SVG with a
    script is refused; nothing stored)."""
    r = upload(admin, kind, data, name, ctype)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == code
    assert admin.get("/api/v1/branding").json()["draft"] is None  # nothing stored on the draft


def test_upload_strips_metadata_and_builds_derivatives(admin):
    r = upload(admin, "login_image", jpeg_with_gps(), "photo.jpg", "image/jpeg")
    assert r.status_code == 200, r.text
    asset = r.json()
    raw = admin.get(asset["url"]).content
    with Image.open(io.BytesIO(raw)) as img:
        assert not img.getexif()  # EXIF/GPS removed
        assert "icc_profile" not in img.info

    r = upload(admin, "mark", png(512, 512, (200, 50, 50, 255)), "mark.png")
    assert r.status_code == 200, r.text
    derivatives = r.json()["derivative_urls"]
    assert {"favicon-16", "favicon-32", "favicon-ico", "apple-touch-180", "pwa-192", "pwa-512", "maskable-512"} <= set(derivatives)
    with Image.open(io.BytesIO(admin.get(derivatives["favicon-ico"]).content)) as ico:
        assert ico.format == "ICO"

    r = upload(admin, "logo_light", cmyk_jpeg(), "cmyk.jpg", "image/jpeg")
    assert r.status_code == 200, r.text  # CMYK converted to sRGB


# ---------------------------------------------------------------------------
# Brand lifecycle: draft -> preview -> publish -> rollback
# ---------------------------------------------------------------------------
def _brand_on(app, host: str, **headers) -> dict:
    client = HostClient(app, host)
    r = client.get("/api/v1/public/brand", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_draft_invisible_until_published_and_rollback(app, admin):
    """EC-WL-07 (draft only through a signed preview token, no-store, others see the published brand) and EC-WL-10
    (rollback is a new version; history kept; served at once)."""
    host = tenant_host("acme-corp")
    before = _brand_on(app, host)
    assert before["display_name"] == "Acme Corp" and before["tokens"] is None

    r = admin.put("/api/v1/branding/draft", json={"changes": {"primary_color": "#0F766E", "tagline": "Work, simplified"}})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["unpublished_changes"] == 2
    # Draft is not public.
    assert _brand_on(app, host)["tagline"] != "Work, simplified"

    # Preview token shows the draft, uncached, to whoever holds it on this host only.
    tenant_admin = HostClient(app, host)
    tenant_admin.http.cookies = admin.http.cookies
    token = tenant_admin.post("/api/v1/branding/preview-token").json()["token"]
    preview = HostClient(app, host).get("/api/v1/public/brand", headers={"X-HRMS-Brand-Preview": token})
    assert preview.json()["tagline"] == "Work, simplified"
    assert "no-store" in preview.headers["cache-control"]
    elsewhere = HostClient(app, tenant_host("other")).get("/api/v1/public/brand", headers={"X-HRMS-Brand-Preview": token})
    assert elsewhere.json().get("tagline") != "Work, simplified"

    validation = admin.post("/api/v1/branding/draft/validate").json()
    assert validation["valid"] and len(validation["changes"]) == 2
    r = admin.post("/api/v1/branding/publish", json={"row_version": validation["row_version"]})
    assert r.status_code == 200, r.text
    v2 = r.json()["version"]
    live = _brand_on(app, host)
    assert live["tagline"] == "Work, simplified" and live["tokens"]["light"]["primary"].startswith("175 ")
    assert live["icons"]["favicon-32"]  # initials mark generated at publish

    admin.put("/api/v1/branding/draft", json={"changes": {"tagline": "Version three"}})
    admin.post("/api/v1/branding/publish", json={})
    assert _brand_on(app, host)["tagline"] == "Version three"

    versions = admin.get("/api/v1/branding/versions").json()
    previous = next(v for v in versions if v["version"] == v2)
    r = admin.post(f"/api/v1/branding/versions/{previous['id']}/restore")
    assert r.status_code == 200, r.text
    assert _brand_on(app, host)["tagline"] == "Work, simplified"
    history = admin.get("/api/v1/branding/versions").json()
    assert history[0]["restored_from_version"] == v2 and len(history) == 4  # nothing rewritten


def test_concurrent_publish_one_wins(app, admin):
    """EC-WL-08: two publishes at once, one wins and the other gets 409."""
    admin.put("/api/v1/branding/draft", json={"changes": {"tagline": "Race"}})
    row_version = admin.get("/api/v1/branding").json()["draft"]["row_version"]
    results = []

    def go():
        results.append(admin.post("/api/v1/branding/publish", json={"row_version": row_version}).status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 409], results


def test_stale_draft_edit_is_refused(admin):
    admin.put("/api/v1/branding/draft", json={"changes": {"tagline": "One"}})
    stale = admin.get("/api/v1/branding").json()["draft"]["row_version"]
    admin.put("/api/v1/branding/draft", json={"changes": {"tagline": "Two"}, "row_version": stale})
    r = admin.put("/api/v1/branding/draft", json={"changes": {"tagline": "Three"}, "row_version": stale})
    assert r.status_code == 409 and r.json()["code"] == "BRAND_DRAFT_CHANGED"


def test_branding_requires_permission(app, admin):
    """EC-WL-07: only users with branding:manage reach the draft."""
    employee = invite_and_activate(app, admin, create_employee(admin, "Plain"), ["employee"])
    hr = invite_and_activate(app, admin, create_employee(admin, "Hr"), ["hr_admin"])
    for client in (employee, hr):
        assert client.get("/api/v1/branding").status_code == 403
        assert client.put("/api/v1/branding/draft", json={"changes": {"tagline": "x"}}).status_code == 403
        assert upload(client, "logo_light", png()).status_code == 403
    assert hr.get("/api/v1/email-templates").status_code == 200  # HR may edit email wording
    assert employee.get("/api/v1/email-templates").status_code == 403
    assert employee.get("/api/v1/branding/current").status_code == 200  # everyone sees their own live brand


def test_invalid_field_values_rejected(admin):
    for changes in (
        {"primary_color": "red"}, {"support_url": "http://insecure.example"}, {"support_url": "javascript:alert(1)"},
        {"tagline": "<script>"}, {"support_phone": "12"}, {"default_locale": "xx-YY"}, {"font": "Comic"},
        {"unknown_field": 1},
    ):
        r = admin.put("/api/v1/branding/draft", json={"changes": changes})
        assert r.status_code == 400, (changes, r.text)


# ---------------------------------------------------------------------------
# Plans and partner locks (EC-WL-11, EC-WL-12)
# ---------------------------------------------------------------------------
def test_plan_gates_product_name_and_downgrade_keeps_value_inactive(app, admin):
    """EC-WL-12: after a downgrade the field resolves to the platform value; the tenant's value is kept inactive
    (NOT_ENTITLED)."""
    r = admin.put("/api/v1/branding/draft", json={"changes": {"product_name": "Acme People"}})
    assert r.status_code == 403 and r.json()["code"] == "BRAND_NOT_ENTITLED"
    ops = platform_admin(app)
    set_plan(ops, admin.company_id, "professional")
    assert admin.put("/api/v1/branding/draft", json={"changes": {"product_name": "Acme People"}}).status_code == 200
    assert admin.post("/api/v1/branding/publish", json={}).status_code == 200
    host = tenant_host("acme-corp")
    assert _brand_on(app, host)["product_name"] == "Acme People"
    set_plan(ops, admin.company_id, "trial")
    assert _brand_on(app, host)["product_name"] == "Intertech HRMS"
    explain = admin.get("/api/v1/branding/resolve").json()
    product = next(f for f in explain["fields"] if f["field"] == "product_name")
    assert product["source"]["reason"] == "NOT_ENTITLED" and product["levels"]["TENANT"] == "Acme People"


def test_partner_lock_overrides_tenant_ui_and_api(app):
    """EC-WL-11: a partner-locked field is refused by the API (403 BRAND_FIELD_LOCKED) and reported as locked to the
    UI."""
    ops = platform_admin(app)
    tenant_a = signup_company(app, "Acme Corp")
    tenant_b = signup_company(app, "Beta Industries")
    partner = ops.post("/api/v1/platform/partners", json={
        "name": "PayEase", "tenant_entitlements": {"branding.product_name": True, "branding.remove_powered_by": True},
    }).json()
    for tenant in (tenant_a, tenant_b):
        assert ops.patch(f"/api/v1/platform/companies/{tenant.company_id}", json={"partner_id": partner["id"]}).status_code == 200
    r = ops.put(f"/api/v1/platform/brands/partners/{partner['id']}", json={
        "changes": {"product_name": "PayEase HR", "show_powered_by": False, "primary_color": "#7C3AED"},
        "locked_fields": ["product_name", "show_powered_by"], "powered_by_label": "Powered by PayEase",
    })
    assert r.status_code == 200, r.text
    assert ops.post(f"/api/v1/platform/brands/partners/{partner['id']}/publish").status_code == 200

    for slug in ("acme-corp", "beta-industries"):
        brand = _brand_on(app, tenant_host(slug))
        assert brand["product_name"] == "PayEase HR" and brand["show_powered_by"] is False
        assert brand["tokens"]["light"]["primary"].startswith("262 ")  # partner colour, not locked

    r = tenant_a.put("/api/v1/branding/draft", json={"changes": {"product_name": "Acme People"}})
    assert r.status_code == 403 and r.json()["code"] == "BRAND_FIELD_LOCKED"
    # Unlocked partner value can still be overridden by the tenant.
    assert tenant_a.put("/api/v1/branding/draft", json={"changes": {"primary_color": "#0F766E"}}).status_code == 200
    tenant_a.post("/api/v1/branding/publish", json={})
    assert _brand_on(app, tenant_host("acme-corp"))["tokens"]["light"]["primary"].startswith("175 ")
    assert _brand_on(app, tenant_host("beta-industries"))["tokens"]["light"]["primary"].startswith("262 ")
    view = tenant_a.get("/api/v1/branding").json()
    assert "product_name" in view["locked_fields"] and view["partner"]["name"] == "PayEase"
    # A partner relationship grants no tenant HR data on the platform console.
    partners = ops.get("/api/v1/platform/partners").json()["partners"][0]
    assert set(partners["tenants"][0]) == {"id", "name", "status", "plan", "active_employees"}


def test_partner_credit_or_lock_only_edit_can_be_published(app):
    """Gate-chrome regression (24 Oct): a partner brand draft that changes only the "powered by" credit or only the
    field locks was refused with 422 "There are no unpublished changes." (the diff compared brand fields only), so an
    operator could never publish it. The credit must reach the tenant's public brand and its documents."""
    ops = platform_admin(app)
    tenant = signup_company(app, "Acme Corp")
    partner = ops.post("/api/v1/platform/partners", json={
        "name": "PayEase", "tenant_entitlements": {"branding.product_name": True},
    }).json()
    assert ops.patch(f"/api/v1/platform/companies/{tenant.company_id}", json={"partner_id": partner["id"]}).status_code == 200

    r = ops.put(f"/api/v1/platform/brands/partners/{partner['id']}", json={"changes": {}, "powered_by_label": "Powered by PayEase"})
    assert r.status_code == 200, r.text
    assert [c["field"] for c in r.json()["validation"]["changes"]] == ["powered_by_label"]
    r = ops.post(f"/api/v1/platform/brands/partners/{partner['id']}/publish")
    assert r.status_code == 200, r.text
    assert _brand_on(app, tenant_host("acme-corp"))["powered_by_label"] == "Powered by PayEase"
    from app.services import brand_service

    assert brand_service.document_brand(tenant.company_id, None)["powered_by_label"] == "Powered by PayEase"

    r = ops.put(f"/api/v1/platform/brands/partners/{partner['id']}", json={"changes": {}, "locked_fields": ["tagline"]})
    assert r.status_code == 200, r.text
    assert [c["field"] for c in r.json()["validation"]["changes"]] == ["locked_fields"]
    assert ops.post(f"/api/v1/platform/brands/partners/{partner['id']}/publish").status_code == 200
    assert "tagline" in tenant.get("/api/v1/branding").json()["locked_fields"]

    # Nothing changed at all is still refused.
    ops.put(f"/api/v1/platform/brands/partners/{partner['id']}", json={"changes": {}})
    r = ops.post(f"/api/v1/platform/brands/partners/{partner['id']}/publish")
    assert r.status_code == 422 and "no unpublished changes" in r.text


def test_kill_switch_falls_back_to_platform_brand(app, admin):
    """EC-WL-26: the platform kill switch falls back to the platform brand."""
    admin.put("/api/v1/branding/draft", json={"changes": {"primary_color": "#0F766E", "tagline": "Totally a bank"}})
    admin.post("/api/v1/branding/publish", json={})
    ops = platform_admin(app)
    r = ops.post(f"/api/v1/platform/companies/{admin.company_id}/brand-kill-switch",
                 json={"disabled": True, "reason": "impersonating a bank"})
    assert r.status_code == 200, r.text
    brand = _brand_on(app, tenant_host("acme-corp"))
    assert brand["tokens"] is None and brand["tagline"] != "Totally a bank"
    ops.post(f"/api/v1/platform/companies/{admin.company_id}/brand-kill-switch", json={"disabled": False})
    assert _brand_on(app, tenant_host("acme-corp"))["tagline"] == "Totally a bank"


# ---------------------------------------------------------------------------
# Public brand isolation (EC-WL-19, EC-WL-20, EC-WL-21) and assets
# ---------------------------------------------------------------------------
def test_public_brand_allow_list_and_host_isolation(app):
    """EC-WL-19 (forged host without the proxy key is the platform host), EC-WL-20 (Vary: Host) and EC-WL-21 (unknown
    host: neutral platform brand)."""
    a = signup_company(app, "Acme Corp")
    b = signup_company(app, "Beta Industries")
    a.put("/api/v1/branding/draft", json={"changes": {"tagline": "Acme only", "support_email": "hr@acme.io"}})
    a.post("/api/v1/branding/publish", json={})
    brand_a = _brand_on(app, tenant_host("acme-corp"))
    brand_b = _brand_on(app, tenant_host("beta-industries"))
    assert brand_a["tagline"] == "Acme only" and brand_b["tagline"] != "Acme only"
    allowed = {
        "version", "host_kind", "host_status", "product_name", "display_name", "short_name", "tagline", "logos", "icons",
        "tokens", "theme_color", "font", "sidebar_style", "radius", "support_url", "help_center_url", "terms_url",
        "privacy_url", "status_page_url", "show_powered_by", "powered_by_label", "default_locale", "dir", "login_scope",
        "is_platform_host", "primary_url", "redirect_to_primary",
    }
    assert set(brand_a) <= allowed, set(brand_a) - allowed  # no ids, contacts, plans or drafts
    assert "hr@acme.io" not in str(brand_a)
    assert brand_a["login_scope"] == "TENANT"

    unknown = _brand_on(app, "nobody.example.com")
    assert unknown["host_kind"] == "UNKNOWN" and unknown["display_name"] == "Intertech HRMS"
    assert unknown["login_scope"] == "NONE"
    # Forged host without the proxy key is treated as the platform host.
    forged = HostClient(app, tenant_host("acme-corp"), proxy_key="wrong").get("/api/v1/public/brand").json()
    assert forged["host_kind"] == "PLATFORM" and forged["tagline"] != "Acme only"
    # A forged brand hint cookie cannot show another tenant's brand on the platform host.
    hinted = HostClient(app, PLATFORM_HOST).get("/api/v1/public/brand", headers={"X-HRMS-Brand-Hint": f"{a.company_id}.forged"})
    assert hinted.json()["tagline"] != "Acme only"
    r = HostClient(app, PLATFORM_HOST).get("/api/v1/public/brand")
    assert "Host" in r.headers["vary"] and "X-HRMS-Host" in r.headers["vary"]


def test_assets_served_only_where_allowed(app):
    a = signup_company(app, "Acme Corp")
    signup_company(app, "Beta Industries")
    draft_asset = upload(a, "logo_light", png(420, 120, (10, 20, 200, 255))).json()
    url = draft_asset["url"]
    # Draft image: not public on any host, but visible to the uploading admin (no-store).
    assert HostClient(app, tenant_host("acme-corp")).get(url).status_code == 404
    own = a.get(url)
    assert own.status_code == 200 and "no-store" in own.headers["cache-control"]
    a.put("/api/v1/branding/draft", json={"changes": {"logo_light": draft_asset["id"]}})
    a.post("/api/v1/branding/publish", json={})
    published = HostClient(app, tenant_host("acme-corp")).get(url)
    assert published.status_code == 200 and "immutable" in published.headers["cache-control"]
    assert published.headers["x-content-type-options"] == "nosniff"
    assert HostClient(app, tenant_host("beta-industries")).get(url).status_code == 404  # another tenant's host
    assert HostClient(app, "nobody.example.com").get(url).status_code == 404
    assert a.get("/api/v1/public/brand-assets/../../etc/passwd").status_code == 404
    assert a.get("/api/v1/public/brand-assets/" + "0" * 64 + ".png").status_code == 404


def test_legacy_logo_endpoint_still_works(app, admin):
    files = {"file": ("logo.png", png(300, 100), "image/png")}
    assert admin.post("/api/v1/settings/logo", files=files).status_code == 200
    r = admin.get("/api/v1/settings/logo")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert admin.get("/api/v1/settings").json()["hasLogo"] is True
    assert _brand_on(app, tenant_host("acme-corp"))["logos"]["light"]


def test_platform_host_preview_needs_a_signed_in_browser_of_that_tenant(app, admin):
    from app.core.security import brand_hint_value

    admin.put("/api/v1/branding/draft", json={"changes": {"tagline": "Unpublished look"}})
    on_platform = HostClient(app, PLATFORM_HOST)
    on_platform.http.cookies = admin.http.cookies
    token = on_platform.post("/api/v1/branding/preview-token").json()["token"]
    stranger = HostClient(app, PLATFORM_HOST).get("/api/v1/public/brand", headers={"X-HRMS-Brand-Preview": token}).json()
    assert stranger["tagline"] != "Unpublished look" and not stranger.get("preview")
    hint = brand_hint_value(admin.company_id, PLATFORM_HOST)
    owner = HostClient(app, PLATFORM_HOST).get("/api/v1/public/brand",
                                               headers={"X-HRMS-Brand-Preview": token, "X-HRMS-Brand-Hint": hint}).json()
    assert owner["tagline"] == "Unpublished look" and owner["preview"] is True
