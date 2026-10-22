"""Bank account change control: step-up, approval (maker-checker), cooling-off and its waiver, alerts to the
old and new contact details, velocity limit and security review, one open change per payee, cancellation,
rejection, split allocations and deactivation with history (3.7 §3, §5; 3.9 §4 Bank accounts).

EC-BNK-06, EC-BNK-09, EC-BNK-12, EC-BNK-13, EC-BNK-17.
"""

from datetime import datetime, timedelta

from tests.payroll.test_bank_accounts import (  # noqa: F401  (bank_world and world are fixtures)
    CHANGES,
    MASK,
    MY,
    MY_CHANGES,
    STAFF,
    IN_NUMBER,
    account_row,
    active_account,
    approve,
    bank_world,
    change_row,
    clear_outbox,
    in_account,
    mails,
    payee_url,
    set_setting,
    sql,
    step_up,
    submit,
    usable,
    verify_with_document,
    world,
)


def audit_of(company_id, action):
    return sql("SELECT entity_type, entity_id, details::text FROM audit_logs WHERE company_id = :c AND action = :a "
               "ORDER BY id", c=company_id, a=action)


# ---------------------------------------------------------------------------
# Step-up, approval, cooling-off, alerts (EC-BNK-06)
# ---------------------------------------------------------------------------
def test_ec_bnk_06_every_change_needs_a_step_up_within_five_minutes(bank_world):
    w = bank_world
    emma, pia = w["u"]["emma"], w["u"]["pia"]
    for client, url in ((emma, MY), (pia, payee_url(w["emp"]["Ivan"]))):
        r = client.post(url, json=in_account())
        assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")
        step_up(client, minutes_ago=6)
        r = client.post(url, json=in_account())
        assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")
    assert sql("SELECT count(*) FROM bank_accounts WHERE company_id = :c", c=w["cid"])[0][0] == 0
    step_up(emma, minutes_ago=4)
    change = submit(emma, in_account())
    row = change_row(change["id"])
    assert row["step_up_at"] and row["step_up_method"] == "EMAIL_OTP" and row["channel"] == "SELF"
    # Approving and waiving also need a fresh step-up of the approver.
    ava = w["u"]["ava"]
    r = ava.post(f"{CHANGES}/{change['id']}/approve", json={})
    assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")


def test_ec_bnk_06_compromised_session_change_waits_for_approval_cooling_off_and_alerts_old_and_new_contacts(
        bank_world):
    w = bank_world
    cid, emma, ava = w["cid"], w["u"]["emma"], w["u"]["ava"]
    first, _ = active_account(w, emma, in_account())
    verify_with_document(w, first, emma)  # payouts go to verified accounts only (exclude_unverified default ON)
    old_address = emma.email.lower()
    # The attacker first changes the contact e-mail, then the bank account.
    sql("UPDATE employees SET email = 'attacker@evil.test' WHERE id = :e", e=w["emp"]["Emma"])
    sql("UPDATE users SET email = 'attacker@evil.test' WHERE id = :u", u=emma.user_id)
    clear_outbox(cid)
    step_up(emma)
    change = submit(emma, in_account(number="50100123456784"))
    second = change["bank_account_id"]
    assert change["status"] == "PENDING_APPROVAL"
    assert set(change) == {"id", "change_request_id", "bank_account_id", "status", "approval_request_id",
                           "row_version"}  # no risk flags to the requester
    alerts = mails(cid)
    assert {m["to"].lower() for m in alerts} == {old_address, "attacker@evil.test"}
    assert all("requested" in m["subject"] and f"{MASK}6784" in m["text"] and "50100123456784" not in m["text"]
               for m in alerts)
    # Nothing changes for payments until the approval and the cooling-off.
    assert [i["bank_account_id"] for i in usable(cid, w["emp"]["Emma"])] == [first]
    pending = w["u"]["pia"].get(f"{CHANGES}/{change['id']}").json()
    assert pending["after"]["account_masked"] == f"{MASK}6784" and pending["before"][0]["id"] == first
    assert pending["alert_recipients"] and all("***@" in a for a in pending["alert_recipients"])
    step_up(ava)
    result = approve(ava, change["id"], remarks="Confirmed by phone call")
    assert result["status"] == "APPLIED"
    row = account_row(second)
    hours = (_ts(row["usable_from"]) - _ts(row["approved_at"])).total_seconds() / 3600
    assert row["status"] == "ACTIVE" and round(hours) == 48 and row["approved_by_user_id"] == ava.user_id
    assert account_row(first)["status"] == "INACTIVE" and account_row(first)["ended_at"] == row["usable_from"]
    serving = usable(cid, w["emp"]["Emma"])
    assert [(i["bank_account_id"], i["payable"]) for i in serving] == [(first, True), (second, False)]
    approved = [m for m in mails(cid) if "was changed" in m["subject"]]
    assert {m["to"].lower() for m in approved} == {old_address, "attacker@evil.test"}
    note = sql("SELECT message FROM notifications WHERE user_id = :u ORDER BY id DESC LIMIT 1", u=emma.user_id)[0][0]
    assert f"{MASK}6784" in note and "50100123456784" not in note
    # Waiving the cooling-off needs an approver, a reason and a step-up; it is audited.
    r = emma.post(f"{STAFF}/{second}/waive-cooling-off", json={"reason": "I need it now"})
    assert r.status_code in (403, 404)
    r = ava.post(f"{STAFF}/{second}/waive-cooling-off", json={"reason": "x"})
    assert (r.status_code, r.json()["code"]) == (400, "REASON_REQUIRED")
    r = ava.post(f"{STAFF}/{second}/waive-cooling-off", json={"reason": "Salary run today, verified by phone",
                                                               "row_version": 1})
    assert (r.status_code, r.json()["code"]) == (409, "ROW_VERSION_CONFLICT")
    r = ava.post(f"{STAFF}/{second}/waive-cooling-off", json={"reason": "Salary run today, verified by phone"})
    assert r.status_code == 200, r.text
    assert [i["bank_account_id"] for i in usable(cid, w["emp"]["Emma"])] == [second]
    assert len([a for a in audit_of(cid, "BANK_COOLING_OFF_WAIVED") if a[1] == str(second)]) == 1
    r = ava.post(f"{STAFF}/{second}/waive-cooling-off", json={"reason": "Salary run today, verified by phone"})
    assert (r.status_code, r.json()["code"]) == (409, "NOT_COOLING_OFF")


def _ts(value):
    return datetime.fromisoformat(value)


def test_self_change_without_approval_activates_with_cooling_off(bank_world):
    w = bank_world
    set_setting(w["cid"], "payroll.bank.change_requires_approval", False)
    set_setting(w["cid"], "payroll.bank.cooling_off_hours", 24)
    emma = w["u"]["emma"]
    step_up(emma)
    change = submit(emma, in_account())
    assert change["status"] == "APPLIED" and change["approval_request_id"] is None
    row = account_row(change["bank_account_id"])
    hours = (_ts(row["usable_from"]) - _ts(row["approved_at"])).total_seconds() / 3600
    assert row["status"] == "ACTIVE" and round(hours) == 24 and row["approved_by_user_id"] is None
    [item] = usable(w["cid"], w["emp"]["Emma"])
    assert item["hold_reasons"][0] == "COOLING_OFF"


# ---------------------------------------------------------------------------
# Maker-checker for HR changes (EC-BNK-12) and employees without a login (EC-BNK-17)
# ---------------------------------------------------------------------------
def test_ec_bnk_12_ec_bnk_17_hr_changes_need_another_person_and_alert_the_employee(bank_world):
    w = bank_world
    cid, hal, ava = w["cid"], w["u"]["hal"], w["u"]["ava"]
    set_setting(cid, "payroll.bank.change_requires_approval", False)  # HR changes still need approval
    ivan_email = sql("SELECT email FROM employees WHERE id = :e", e=w["emp"]["Ivan"])[0][0]
    clear_outbox(cid)
    step_up(hal)
    change = submit(hal, in_account("Ivan Test", "50100123456785"), url=payee_url(w["emp"]["Ivan"]))
    assert change["status"] == "PENDING_APPROVAL" and "HR_CHANGE" in change["risk_flags"]
    assert account_row(change["bank_account_id"])["status"] == "PENDING_APPROVAL"
    r = hal.post(f"{CHANGES}/{change['id']}/approve", json={})
    assert r.status_code == 403, r.text  # the maker cannot approve their own change
    r = hal.post(f"{CHANGES}/{change['id']}/approve", json={"waive_cooling_off_reason": "Self-approval attempt"})
    assert r.status_code == 403
    r = w["u"]["pia"].post(f"{CHANGES}/{change['id']}/approve", json={})
    assert r.status_code == 403  # no approve permission
    step_up(ava)
    assert approve(ava, change["id"])["status"] == "APPLIED"
    alerts = mails(cid)
    assert {m["to"].lower() for m in alerts} == {ivan_email.lower()}  # Ivan has no login: e-mail only
    assert any("by HR" in m["text"] for m in alerts)
    # The maker cannot verify the account either.
    step_up(hal)
    r = hal.post(f"{STAFF}/{change['bank_account_id']}/review", json={"action": "MARK_FAILED", "reason": "test it"})
    assert r.status_code == 403


def test_employee_cannot_approve_their_own_change_even_with_the_approver_role(bank_world):
    w = bank_world
    ava = w["u"]["ava"]
    step_up(ava)
    change = submit(ava, in_account("Ava Test", "50100123456786"))
    r = ava.post(f"{CHANGES}/{change['id']}/approve", json={})
    assert r.status_code == 403
    assert change_row(change["id"])["status"] == "PENDING_APPROVAL"


# ---------------------------------------------------------------------------
# Velocity limit and security review (EC-BNK-13)
# ---------------------------------------------------------------------------
def test_ec_bnk_13_a_third_change_in_90_days_is_blocked_pending_a_security_review(bank_world):
    w = bank_world
    cid, emma, ava = w["cid"], w["u"]["emma"], w["u"]["ava"]
    set_setting(cid, "payroll.bank.change_requires_approval", False)
    for number in ("50100123456701", "50100123456702"):
        step_up(emma)
        assert submit(emma, in_account(number=number))["status"] == "APPLIED"
    clear_outbox(cid)
    step_up(emma)
    third = submit(emma, in_account(number="50100123456703"))
    assert third["status"] == "SECURITY_REVIEW"
    assert account_row(third["bank_account_id"])["status"] == "DRAFT"
    assert any("security review" in m["subject"] for m in mails(cid))
    staff_view = w["u"]["pia"].get(f"{CHANGES}/{third['id']}").json()
    assert "VELOCITY_LIMIT" in [f["code"] for f in staff_view["risk_flags"]]
    step_up(emma)
    r = emma.post(MY, json=in_account(number="50100123456704"))
    assert (r.status_code, r.json()["code"]) == (409, "CHANGE_ALREADY_PENDING")
    r = emma.post(f"{CHANGES}/{third['id']}/security-review", json={"decision": "RELEASE", "reason": "mine"})
    assert r.status_code in (403, 404)
    step_up(ava)
    r = ava.post(f"{CHANGES}/{third['id']}/approve", json={})
    assert (r.status_code, r.json()["code"]) == (409, "NOT_PENDING")
    r = ava.post(f"{CHANGES}/{third['id']}/security-review",
                 json={"decision": "RELEASE", "reason": "Employee confirmed by video call"})
    assert r.status_code == 200 and r.json()["status"] == "APPLIED", r.text
    assert account_row(third["bank_account_id"])["status"] == "ACTIVE"
    # A fourth change is blocked again and this time rejected: the account never becomes usable.
    step_up(emma)
    fourth = submit(emma, in_account(number="50100123456705"))
    assert fourth["status"] == "SECURITY_REVIEW"
    r = ava.post(f"{CHANGES}/{fourth['id']}/security-review", json={"decision": "REJECT",
                                                                     "reason": "Could not reach the employee"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert account_row(fourth["bank_account_id"])["status"] == "DRAFT"
    assert fourth["bank_account_id"] not in [i["bank_account_id"] for i in usable(cid, w["emp"]["Emma"],
                                                                                   _far_future())]
    reviewed = audit_of(cid, "BANK_CHANGE_SECURITY_REVIEWED")
    assert len(reviewed) == 2


def _far_future():
    from app.core import clock

    return clock.utcnow() + timedelta(days=30)


# ---------------------------------------------------------------------------
# One open change, cancellation, rejection
# ---------------------------------------------------------------------------
def test_one_open_change_per_payee_cancel_and_reject(bank_world):
    w = bank_world
    cid, emma, ava, gus = w["cid"], w["u"]["emma"], w["u"]["ava"], w["u"]["gus"]
    step_up(emma)
    first = submit(emma, in_account())
    r = emma.post(MY, json=in_account(number="50100123456787"))
    assert (r.status_code, r.json()["code"]) == (409, "CHANGE_ALREADY_PENDING")
    step_up(w["u"]["pia"])
    r = w["u"]["pia"].post(payee_url(w["emp"]["Emma"]), json=in_account(number="50100123456787"))
    assert (r.status_code, r.json()["code"]) == (409, "CHANGE_ALREADY_PENDING")
    assert gus.post(f"{MY_CHANGES}/{first['id']}/cancel", json={}).status_code == 404
    r = emma.post(f"{MY_CHANGES}/{first['id']}/cancel", json={"remarks": "Typed the wrong account"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED", r.text
    assert account_row(first["bank_account_id"])["status"] == "DRAFT"
    r = emma.post(f"{MY_CHANGES}/{first['id']}/cancel", json={})
    assert (r.status_code, r.json()["code"]) == (409, "NOT_PENDING")
    step_up(emma)
    second = submit(emma, in_account(number="50100123456788"))
    step_up(ava)
    r = ava.post(f"{CHANGES}/{second['id']}/reject", json={})
    assert r.status_code == 422
    r = ava.post(f"{CHANGES}/{second['id']}/reject", json={"remarks": "Holder could not be confirmed"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    row = account_row(second["bank_account_id"])
    assert row["status"] == "DRAFT" and row["status_reason"] == "Holder could not be confirmed"
    mine = emma.get(MY_CHANGES).json()["items"]
    assert [c["status"] for c in mine] == ["REJECTED", "CANCELLED"]
    assert "risk_flags" not in mine[0] and "ip_address" not in mine[0]
    assert usable(cid, w["emp"]["Emma"], _far_future()) == []
    notes = [t for (t,) in sql("SELECT title FROM notifications WHERE user_id = :u", u=emma.user_id)]
    assert "Bank account change not approved" in notes


# ---------------------------------------------------------------------------
# Split allocations (EC-BNK-09) and deactivation with history
# ---------------------------------------------------------------------------
def test_ec_bnk_09_split_allocations_are_validated_on_save(bank_world):
    w = bank_world
    cid, emma = w["cid"], w["u"]["emma"]
    set_setting(cid, "payroll.bank.split_payments_enabled", True)
    set_setting(cid, "payroll.bank.max_accounts_per_payee", 2)
    first, _ = active_account(w, emma, in_account(allocation={"type": "REMAINDER", "order": 2}))
    step_up(emma)
    r = emma.post(MY, json=in_account(number="50100123456791", allocation={"type": "PERCENT", "value": "130",
                                                                             "order": 1}))
    assert r.status_code == 400 and r.json()["code"] == "PERCENT_RANGE", r.text
    r = emma.post(MY, json=in_account(number="50100123456791", allocation={"type": "REMAINDER", "order": 1}))
    assert r.status_code == 400 and r.json()["code"] == "REMAINDER_NOT_UNIQUE", r.text
    r = emma.post(MY, json=in_account(number="50100123456791"))
    assert (r.status_code, r.json()["code"]) == (400, "ALLOCATION_REQUIRED")
    second, _ = active_account(w, emma, in_account(number="50100123456791",
                                                    allocation={"type": "PERCENT", "value": "30", "order": 1}))
    accounts = {i["bank_account_id"]: i["allocation"] for i in usable(cid, w["emp"]["Emma"])}
    assert accounts == {first: {"type": "REMAINDER", "value": None, "order": 2},
                        second: {"type": "PERCENT", "value": "30", "order": 1}}
    step_up(emma)
    r = emma.post(MY, json=in_account(number="50100123456792", allocation={"type": "FIXED", "value": "100.00",
                                                                             "order": 3}))
    assert r.status_code == 400 and r.json()["code"] == "TOO_MANY_ACCOUNTS", r.text
    # The REMAINDER account cannot leave the PERCENT account alone.
    r = emma.post(f"{MY}/{first}/deactivate", json={"reason": "Closing this account"})
    assert r.status_code == 400 and r.json()["code"] == "REMAINDER_REQUIRED", r.text


def test_deactivation_keeps_history_and_checks_row_version(bank_world):
    from app.core import clock

    w = bank_world
    cid, emma = w["cid"], w["u"]["emma"]
    account_id, _ = active_account(w, emma, in_account())
    before = clock.utcnow()
    version = account_row(account_id)["row_version"]
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=emma.user_id)
    r = emma.post(f"{MY}/{account_id}/deactivate", json={"reason": "Closing this account"})
    assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")
    step_up(emma)
    r = emma.post(f"{MY}/{account_id}/deactivate", json={"reason": "Closing this account", "row_version": version - 1})
    assert (r.status_code, r.json()["code"]) == (409, "ROW_VERSION_CONFLICT")
    r = emma.post(f"{MY}/{account_id}/deactivate", json={"reason": "Closing this account"},
                  headers={"If-Match": f'"{version}"'})
    assert r.status_code == 200 and r.json()["status"] == "INACTIVE", r.text
    r = emma.post(f"{MY}/{account_id}/deactivate", json={"reason": "Closing this account"})
    assert r.status_code == 409
    assert usable(cid, w["emp"]["Emma"]) == []
    assert [i["bank_account_id"] for i in usable(cid, w["emp"]["Emma"], before)] == [account_id]  # history
    assert w["u"]["pia"].get(STAFF, params={"status": "INACTIVE"}).json()["items"][0]["id"] == account_id
    assert sql("SELECT count(*) FROM bank_accounts WHERE id = :i", i=account_id)[0][0] == 1
    assert IN_NUMBER not in str(audit_of(cid, "BANK_ACCOUNT_DEACTIVATE"))


def test_change_requests_listing_scope_and_idempotent_submission(bank_world):
    w = bank_world
    emma, pia, max_ = w["u"]["emma"], w["u"]["pia"], w["u"]["max"]
    step_up(emma)
    headers = {"Idempotency-Key": "bank-change-0001"}
    first = emma.post(MY, json=in_account(), headers=headers)
    again = emma.post(MY, json=in_account(), headers=headers)
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    other = emma.post(MY, json=in_account(number="50100123456793"), headers=headers)
    assert other.status_code in (409, 422)
    assert sql("SELECT count(*) FROM bank_accounts WHERE company_id = :c", c=w["cid"])[0][0] == 1
    stored = sql("SELECT coalesce(request_hash, '') || coalesce(response::text, '') FROM idempotency_keys "
                 "WHERE key = 'bank-change-0001'")
    assert stored and IN_NUMBER not in stored[0][0]
    assert [c["id"] for c in pia.get(CHANGES).json()["items"]] == [first.json()["id"]]
    assert pia.get(CHANGES, params={"status": "APPLIED"}).json()["items"] == []
    assert max_.get(CHANGES).json()["items"] == []  # AE01 reader: Emma is in LE01
    assert max_.get(f"{CHANGES}/{first.json()['id']}").status_code == 404
    assert w["u"]["cleo"].get(CHANGES).status_code == 403
