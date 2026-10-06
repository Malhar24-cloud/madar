"""اختبارات الصلاحيات وقواعد الطلبات وطبقات الحماية."""
import io
import time
import zipfile
from datetime import date, timedelta

import pyotp
import pytest

from madar.filesafe import check_zip
from madar.models import AuditLog, Email, Employee, Request, User, db
from madar.permissions import can_act
from madar.security import password_problem, safe_cell
from madar.services import audit, importer, mailer, rules, workflow
from tests.conftest import login

D = "alrawasi.example"
ADMIN = f"a.alfaifi@{D}"


def user(email):
    return User.query.filter_by(email=email).first()


def emp(code):
    return Employee.query.filter_by(code=code).first()


def leave(client, email, days, start_in=10):
    login(client, email)
    start = (date.today() + timedelta(days=start_in)).isoformat()
    client.post("/requests/new/leave", data={"from": start, "days": str(days)})
    client.post("/logout")
    e = user(email).employee
    return Request.query.filter_by(employee_id=e.id, type="leave").order_by(Request.id.desc()).first()


# ---------------- صفحة الصلاحيات ----------------
def test_permissions_page_admin_only(client):
    login(client, f"h.alanazi@{D}")  # مسؤولة رواتب
    assert client.get("/permissions").status_code == 403
    assert client.get("/rules").status_code == 403
    client.post("/logout")
    login(client, ADMIN)
    r = client.get("/permissions")
    assert r.status_code == 200 and "الصلاحيات".encode() in r.data


def test_grant_leaves_permission_routes_requests(client):
    """مثال: نعطي محمد صلاحية الإجازات، فتوصله طلبات الإجازة في مرحلة الموارد البشرية."""
    mohammed = user(f"m.alghamdi@{D}")
    assert not mohammed.can("leaves")
    login(client, ADMIN)
    r = client.post(f"/permissions/{mohammed.id}", data={"role": "employee", "perms_shown": "1", "perms": ["leaves"]})
    assert r.status_code == 302
    client.post("/logout")
    db.session.refresh(mohammed)
    assert mohammed.can("leaves") and not mohammed.can("payroll")
    assert AuditLog.query.filter_by(action="تعديل صلاحيات").count() == 1

    req = leave(client, f"t.alsubaie@{D}", 8)
    assert req.status == "pending" and req.current_stage == "manager"
    workflow.approve(req, user(f"r.aldosari@{D}"))
    assert req.current_stage == "hr"
    assert can_act(mohammed, req)
    assert not can_act(user(f"h.alanazi@{D}"), req)  # الرواتب ما لها دخل بالإجازات


def test_cannot_edit_own_permissions_or_grant_admin_perm(client):
    admin = user(ADMIN)
    login(client, ADMIN)
    client.post(f"/permissions/{admin.id}", data={"role": "employee", "perms_shown": "1"})
    db.session.refresh(admin)
    assert admin.base_role == "admin"
    # صلاحية «الإدارة» ما تنعطى كمربع: تُتجاهل
    u = user(f"m.alghamdi@{D}")
    client.post(f"/permissions/{u.id}", data={"role": "employee", "perms_shown": "1", "perms": ["admin", "letters"]})
    db.session.refresh(u)
    assert u.effective_perms == {"letters"} and not u.can("admin")


def test_permission_change_kills_staff_sessions(app):
    a, b = app.test_client(), app.test_client()
    u = user(f"n.alqahtani@{D}")
    login(b, u.email)
    assert b.get("/employees/").status_code == 200
    login(a, ADMIN)
    a.post(f"/permissions/{u.id}", data={"role": "employee", "perms_shown": "1"})
    assert b.get("/employees/").status_code == 302  # أُخرجت وتحتاج دخول جديد


# ---------------- قواعد الطلبات ----------------
def test_rules_change_auto_approval(client):
    login(client, ADMIN)
    client.post("/rules", data={"leave_auto": "1", "leave_limit": "5", "leave_stages": ["manager", "hr"],
                                "loan_auto": "1", "loan_stages": ["manager"]})
    client.post("/logout")
    assert rules.get("leave")["limit"] == 5
    loan = rules.get("loan")
    assert loan["auto"] is False and "payroll" in loan["stages"]  # السلفة ما تصير تلقائية ولا تتجاوز الرواتب
    r = leave(client, f"t.alsubaie@{D}", 5)
    assert r.status == "approved" and r.auto

    login(client, ADMIN)
    client.post("/rules", data={"leave_stages": ["hr"]})  # إيقاف التلقائي وتمريرها على الموارد البشرية فقط
    client.post("/logout")
    r = leave(client, f"j.albuqami@{D}", 2)
    assert r.status == "pending" and r.stages == ["hr"]


def test_rules_normalize_rejects_tampering():
    r = rules.normalize("change", {"auto": True, "stages": ["manager"]})
    assert r == {"auto": False, "limit": None, "stages": ["payroll"]}
    r = rules.normalize("permission", {"auto": True, "limit": 99, "stages": []})
    assert r["limit"] == 4 and r["stages"] == ["manager"]


def test_backdated_leave_never_auto(client):
    r = leave(client, f"t.alsubaie@{D}", 1, start_in=-3)
    assert r.status == "pending"


# ---------------- فصل المهام ----------------
def test_same_person_cannot_approve_two_stages(client):
    login(client, f"b.alrashidi@{D}")
    client.post("/requests/change", data={"employee_id": emp("EMP-1015").id, "kind": "bonus", "amount": "1000",
                                          "effective": date.today().isoformat(), "reason": "إنجاز مشروع"})
    req = Request.query.filter_by(type="change").first()
    assert req.stages == ["hr", "payroll"]
    hayfa = user(f"h.alanazi@{D}")  # عندها «التعديلات» و«الرواتب»
    assert can_act(hayfa, req)
    workflow.approve(req, hayfa)
    assert req.current_stage == "payroll"
    assert not can_act(hayfa, req)  # ما تعتمد المرحلتين لحالها
    assert can_act(user(ADMIN), req)


# ---------------- إعادة التحقق والجلسات ----------------
def test_sensitive_pages_need_fresh_reauth(client):
    login(client, ADMIN)
    with client.session_transaction() as s:
        s["fresh_at"] = 0
    r = client.get("/permissions")
    assert r.status_code == 302 and "/reauth" in r.headers["Location"]
    assert client.get("/export.xlsx").status_code == 302
    r = client.post("/reauth", data={"password": "wrong", "next": "/permissions"})
    assert "غير صحيحة".encode() in r.data
    r = client.post("/reauth", data={"password": "Madar@2026!", "next": "/permissions"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/permissions")
    assert client.get("/permissions").status_code == 200


def test_idle_session_expires(client):
    login(client, f"m.alghamdi@{D}")
    assert client.get("/requests").status_code == 200
    with client.session_transaction() as s:
        s["last_seen"] = time.time() - 31 * 60
    r = client.get("/requests")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_must_change_password_forced(app, client):
    u = user(f"m.alghamdi@{D}")
    u.must_change_password = True
    db.session.commit()
    r = login(client, u.email)
    assert "/account/password" in r.headers["Location"]
    assert "/account/password" in client.get("/requests").headers["Location"]


# ---------------- التخمين والمحاولات ----------------
def test_login_rate_limit(client):
    for _ in range(10):
        login(client, f"nobody@{D}", "x")
    r = login(client, f"nobody@{D}", "x")
    assert r.status_code == 429


def test_totp_code_cannot_be_reused(app):
    u = user(ADMIN)
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()
    from madar.routes.auth import verify_totp
    with app.test_request_context():
        assert verify_totp(u, secret, code)
        assert not verify_totp(u, secret, code)  # نفس الرمز مرة ثانية مرفوض
        assert not verify_totp(u, secret, "abc123")


def test_password_policy(app):
    u = user(f"m.alghamdi@{D}")
    assert password_problem("password123")
    assert password_problem("aaaaaaaaa1")
    assert password_problem("m.alghamdi99x", u)
    assert password_problem("emp-1015-secret9", u)
    assert password_problem("Madar@2026!", u)  # نفس الحالية
    assert password_problem("Blue-Falcon-73") is None


# ---------------- البيانات الحساسة ----------------
def test_national_id_and_iban_encrypted_at_rest(app):
    e = emp("EMP-1001")
    raw = db.session.execute(db.text("select national_id, iban from employees where id=:i"), {"i": e.id}).one()
    assert raw[0].startswith("enc:") and raw[1].startswith("enc:")
    assert e.national_id.isdigit() and e.iban.startswith("SA")


def test_financial_emails_hidden_from_non_payroll(client):
    m = mailer.queue("x@test", "س", "سلفة", "مبلغ 5000", kind="draft", tag="سلفة")
    db.session.commit()
    login(client, f"n.alqahtani@{D}")  # التوظيف: عندها المراسلات بدون الرواتب
    assert client.get(f"/outbox/{m.id}").status_code == 403
    assert "مبلغ 5000".encode() not in client.get("/outbox?show=all").data


def test_sent_email_links_scrubbed_from_database(app):
    m = mailer.queue("x@test", "س", "رابط", "نص", actions=[["فتح", "http://test/a/secret-token"]])
    assert m.status == "sent"
    assert "secret-token" not in str(m.actions)


def test_email_link_for_loan_requires_login(app, client):
    req = Request.query.filter_by(type="loan").first()
    from madar.services import tokens
    raw = tokens.issue("approve", user(f"b.alrashidi@{D}").id, 48, request_id=req.id, stage_index=req.stage_index)
    db.session.commit()
    r = client.get(f"/a/{raw}")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


# ---------------- الاستيراد والملفات ----------------
def test_import_cannot_escalate_roles(app):
    recs = [{"name": "مستخدم تجربة", "nationality": "سعودي", "department": "المالية", "title": "محاسب", "basic": 5000,
             "hire_date": date.today(), "contract_type": "open", "email": f"new.admin@{D}", "role": "admin"}]
    with app.test_request_context():
        importer.import_records(recs, "اختبار", actor_is_admin=False)
    assert user(f"new.admin@{D}").base_role == "employee"


def test_activation_code_not_for_staff_by_non_admin(client):
    login(client, f"n.alqahtani@{D}")  # التوظيف
    target = emp("EMP-1006")  # مسؤولة الرواتب
    assert client.post(f"/employees/{target.id}/activation").status_code in (400, 403)


def test_zip_bomb_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/big.xml", b"0" * (60 * 1024 * 1024))
    with pytest.raises(ValueError):
        check_zip(buf.getvalue())


def test_formula_injection_neutralized():
    assert safe_cell("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert safe_cell("@SUM(A1)") == "'@SUM(A1)"
    assert safe_cell("-250.00") == "-250.00"
    assert safe_cell(500) == 500


# ---------------- سجل التدقيق ----------------
def test_audit_chain_detects_tampering(app):
    audit.log("اختبار", "سطر 1")
    audit.log("اختبار", "سطر 2")
    db.session.commit()
    assert audit.verify_chain()[0]
    row = AuditLog.query.filter_by(detail="سطر 1").first()
    db.session.execute(db.text("update audit_log set detail='معدل' where id=:i"), {"i": row.id})
    db.session.commit()
    db.session.expire_all()
    ok, bad = audit.verify_chain()
    assert not ok and bad == row.id


def test_operator_cannot_open_permissions_even_with_grant(app):
    from madar.models import SupportGrant, utcnow
    op = user(f"it.support@{D}")
    db.session.add(SupportGrant(operator_id=op.id, granted_by="اختبار", reason="فحص", expires_at=utcnow() + timedelta(hours=1)))
    db.session.commit()
    c = app.test_client()
    login(c, op.email)
    assert c.get("/permissions").status_code == 403
    assert c.get("/export.xlsx").status_code == 403
