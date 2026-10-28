"""Rollback rehearsal of 0050_as_m2_sites_consents (13 §4.5 rollback: downgrade as_m2_sites_consents first).

Downgrade to 0049 with a face encoding stored only as ciphertext: the revision decrypts it back to the plaintext
column (0026 pattern), drops the sites / consents / documents tables and the capture-side grants; the upgrade
restores them, re-grants hr_admin and marks the existing encoding as a legacy enrolment.
"""

import os
import subprocess
import sys
from pathlib import Path

from tests.attendance.capture_v2_helpers import rows, sql
from tests.attendance_helpers import employee_with_login
from tests.helpers import signup_company

ROOT = Path(__file__).resolve().parents[2]


def _alembic(*args):
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=os.environ.copy(),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_0050_downgrade_decrypts_faces_and_upgrade_restores(app, monkeypatch):
    from app.modules.face_recognition import engine as face_engine
    from tests.attendance.test_consents import _jpeg

    admin = signup_company(app, "Rollback Co")
    emp = employee_with_login(app, admin, "Anjali")
    emp_id = emp.me()["employee"]["id"]
    monkeypatch.setattr(face_engine, "available", lambda: True)
    monkeypatch.setattr(face_engine, "encode", lambda image: "rollback-encoding")
    r = admin.post(f"/api/v1/face/enroll?employee_id={emp_id}", files={"file": ("f.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    assert rows("SELECT encoding FROM face_encodings WHERE employee_id = :e", e=emp_id)[0]["encoding"] is None
    _alembic("downgrade", "0049_as_m2_punches_sessions")
    try:
        assert rows("SELECT encoding FROM face_encodings WHERE employee_id = :e", e=emp_id)[0]["encoding"] == \
            "rollback-encoding"
        assert sql("SELECT to_regclass('attendance_sites')").scalar() is None
        assert sql("SELECT count(*) FROM role_permissions WHERE permission_code = 'attendance:manage_sites'").scalar() == 0
    finally:
        _alembic("upgrade", "head")
    from app.cli import grant_app_role
    from app.database.db import system_engine

    with system_engine.connect() as conn:
        role = conn.execute(__import__("sqlalchemy").text(
            "SELECT DISTINCT grantee FROM information_schema.role_table_grants WHERE table_name = 'employees' "
            "AND privilege_type = 'INSERT' AND grantee <> current_user")).scalars().first()
    with system_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        grant_app_role(conn, role)
    face = rows("SELECT encoding, legacy_enrolled FROM face_encodings WHERE employee_id = :e", e=emp_id)[0]
    assert face["encoding"] == "rollback-encoding" and face["legacy_enrolled"] is True
    assert sql("SELECT count(*) FROM role_permissions rp JOIN roles r ON r.id = rp.role_id WHERE r.company_id = :c "
               "AND r.system_key = 'hr_admin' AND rp.permission_code = 'attendance:manage_sites'",
               c=admin.company_id).scalar() == 1
    assert admin.get("/api/v1/attendance/sites").status_code == 200
