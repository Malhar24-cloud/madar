import re
from datetime import date, timedelta

from madar.models import Email, Employee, Loan, PayrollRun, Request, Task, User, db
from madar.services import ai, automation, textract
from madar import policy
from tests.conftest import login, outbox

D = "alrawasi.example"


def emp(code):
    return Employee.query.filter_by(code=code).first()


def test_login_and_lockout(client):
    assert login(client, f"b.alrashidi@{D}", "wrong").status_code == 200
    for _ in range(4):
        login(client, f"b.alrashidi@{D}", "wrong")
    r = login(client, f"b.alrashidi@{D}")  # كلمة المرور الصحيحة لكن الحساب مقفل
    assert "مقفل".encode() in r.data
    assert User.query.filter_by(email=f"b.alrashidi@{D}").first().locked_until is not None


def test_security_headers_and_login_required(client):
    r = client.get("/employees/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    r = client.get("/login")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_employee_sees_only_self(client):
    login(client, f"m.alghamdi@{D}")
    other = emp("EMP-1014")
    assert client.get(f"/employees/{other.id}").status_code == 403
    assert client.get("/employees/").status_code == 403
    assert client.get("/payroll/").status_code == 403
    assert client.get(f"/employees/{emp('EMP-1015').id}").status_code == 200


def test_manager_cannot_see_salary(client):
    login(client, f"b.alrashidi@{D}")
    r = client.get(f"/employees/{emp('EMP-1015').id}")
    assert r.status_code == 200 and "الراتب الشهري".encode() not in r.data
    assert client.get(f"/employees/{emp('EMP-1002').id}").status_code == 403  # خارج فريقه


def test_short_leave_auto_approved(client):
    login(client, f"t.alsubaie@{D}")
    start = (date.today() + timedelta(days=5)).isoformat()
    client.post("/requests/new/leave", data={"from": start, "days": "2"})
    r = Request.query.filter_by(employee_id=emp("EMP-1011").id, type="leave").first()
    assert r.status == "approved" and r.auto
    assert emp("EMP-1011").leave_used == 2


def test_loan_over_limit_rejected(client):
    login(client, f"t.alsubaie@{D}")
    client.post("/requests/new/loan", data={"amount": "50000", "months": "4"})
    r = Request.query.filter_by(employee_id=emp("EMP-1011").id, type="loan").first()
    assert r.status == "rejected" and r.auto


def test_email_approval_link_single_use(app, client):
    # طلب إجازة محمد الغامدي (5 أيام) أُنشئ في البيانات التجريبية وينتظر مديره بندر
    req = Request.query.filter_by(employee_id=emp("EMP-1015").id, type="leave").first()
    assert req.current_stage == "manager"
    msg = next(m for m in outbox(app) if f"b.alrashidi@{D}" in m["To"] and "إجازة" in m["Subject"])
    link = re.search(r"http://test(/a/[\w\-]+)", msg.get_body(("plain",)).get_content()).group(1)
    assert b"<form" in client.get(link).data                    # GET لا ينفذ شيئًا
    assert req.current_stage == "manager"
    assert client.post(link, data={"decision": "approve"}).status_code == 200
    db.session.refresh(req)
    assert req.current_stage == "hr"
    assert client.post(link, data={"decision": "approve"}).status_code == 410  # الرابط لا يُستخدم مرتين


def test_no_self_approval(client):
    login(client, f"a.alfaifi@{D}")  # مدير الموارد البشرية
    start = (date.today() + timedelta(days=20)).isoformat()
    client.post("/requests/new/leave", data={"from": start, "days": "5"})
    r = Request.query.filter_by(employee_id=emp("EMP-1019").id, status="pending").first()
    assert r is not None
    assert client.post(f"/requests/{r.id}/decide", data={"decision": "approve"}).status_code == 403


def test_full_approval_chain(client):
    req = Request.query.filter_by(employee_id=emp("EMP-1014").id, type="loan").first()
    login(client, f"b.alrashidi@{D}")
    client.post(f"/requests/{req.id}/decide", data={"decision": "approve"})
    client.post("/logout")
    login(client, f"h.alanazi@{D}")  # مسؤولة الرواتب
    client.post(f"/requests/{req.id}/decide", data={"decision": "approve"})
    db.session.refresh(req)
    assert req.status == "approved"
    assert Loan.query.filter_by(employee_id=emp("EMP-1014").id).count() == 1


def test_payroll_run(client):
    login(client, f"h.alanazi@{D}")
    month = date.today().strftime("%Y-%m")
    before = Loan.query.filter_by(employee_id=emp("EMP-1018").id).first().paid
    client.post("/payroll/approve", data={"month": month, "confirm": "yes"})
    run = PayrollRun.query.filter_by(month=month).first()
    assert run and len(run.payslips) == Employee.query.filter_by(status="active").count()
    assert Loan.query.filter_by(employee_id=emp("EMP-1018").id).first().paid == before + 1
    r = client.get(f"/payroll/{month}/wps.csv")
    assert r.status_code == 200 and "IBAN" in r.get_data(as_text=True)
    client.post("/payroll/approve", data={"month": month, "confirm": "yes"})
    assert PayrollRun.query.filter_by(month=month).count() == 1


def test_offboarding_kills_session(app):
    victim = app.test_client()
    login(victim, f"o.abdelrahman@{D}")
    assert victim.get("/").status_code == 200
    admin = app.test_client()
    login(admin, f"a.alfaifi@{D}")
    e = emp("EMP-1017")
    r = admin.post(f"/employees/{e.id}/offboard", data={"reason": "term", "last_day": date.today().isoformat(), "confirm": "yes"})
    assert r.status_code == 200
    assert victim.get("/").status_code == 302  # الجلسة القديمة انتهت
    assert login(victim, f"o.abdelrahman@{D}").status_code == 200  # لا يقدر يدخل
    assert Email.query.filter_by(employee_id=e.id, status="pending_approval").count() == 1  # الإشعار الحساس مسودة
    assert Task.query.filter_by(key=f"revoke:{e.id}").first() is not None


def test_ai_tools_enforce_permissions(app):
    worker = User.query.filter_by(email=f"m.alghamdi@{D}").first()
    manager = User.query.filter_by(email=f"b.alrashidi@{D}").first()
    assert "error" in ai.run_tool(worker, "get_employee", {"code": "EMP-1013"})
    assert "gross" in ai.run_tool(worker, "get_employee", {})
    info = ai.run_tool(manager, "get_employee", {"code": "EMP-1015"})
    assert "gross" not in info and "national_id" not in info and "iban" not in info
    assert "error" in ai.run_tool(manager, "end_of_service", {"code": "EMP-1015", "reason": "term"})
    assert len(ai.run_tool(worker, "search_employees", {"query": ""})) == 1
    assert "رصيد" in ai.local_reply(worker, "كم باقي من إجازتي؟")


def test_contract_extract_local_and_risks():
    text = ("الطرف الثاني (الموظف): مشاري الحربي\nالجنسية: سعودي\nالمسمى الوظيفي: محلل بيانات\nالقسم: تقنية المعلومات\n"
            "تاريخ المباشرة: 2026-11-01\nنوع العقد: محدد المدة\nتاريخ انتهاء العقد: 2028-11-01\nالراتب الأساسي: 10,500 ريال\n"
            "فترة التجربة: 200 يوم\nالإجازة السنوية: 21 يومًا\nبند عدم المنافسة: لا يعمل لدى منافس لمدة سنة.\n")
    d = textract.local_contract_extract(text)
    assert d["name"] == "مشاري الحربي" and d["basic_salary"] == 10500 and d["contract_type"] == "fixed"
    assert d["department"] == "تقنية المعلومات" and d["start_date"] == "2026-11-01"
    risks = policy.contract_risks(d)
    assert any("200" in r for r in risks) and any("المنافسة" in r for r in risks)


def test_daily_sweep_idempotent(app):
    first = automation.daily_sweep()
    db.session.commit()
    assert first
    assert automation.daily_sweep() == []


def test_onboarding_creates_account_and_set_password(app, client):
    login(client, f"n.alqahtani@{D}")  # أخصائية التوظيف
    r = client.post("/employees/new", data={
        "name": "هتان الزهراني", "email": f"h.alzahrani@{D}", "nationality": "سعودي", "department": "تقنية المعلومات",
        "title": "محلل أمن", "basic": "12000", "hire_date": (date.today() + timedelta(days=14)).isoformat(),
        "contract_type": "open", "probation_days": "90"})
    assert r.status_code == 302
    u = User.query.filter_by(email=f"h.alzahrani@{D}").first()
    assert u and u.password_hash is None
    msg = next(m for m in outbox(app) if f"h.alzahrani@{D}" in m["To"])
    link = re.search(r"http://test(/set-password/[\w\-]+)", msg.get_body(("plain",)).get_content()).group(1)
    other = app.test_client()
    assert other.post(link, data={"password": "short", "password2": "short"}).status_code == 200
    assert other.post(link, data={"password": "StrongPass2026", "password2": "StrongPass2026"}).status_code == 302
    assert login(other, f"h.alzahrani@{D}", "StrongPass2026").status_code == 302
