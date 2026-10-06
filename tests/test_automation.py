import io
import re
from datetime import date, timedelta

import pyotp

from madar import policy
from madar.models import (AttendanceDay, AuditLog, Email, Employee, PayrollAdjustment, Request, SupportGrant, Task, User,
                          db, utcnow)
from madar.permissions import can_act
from madar.services import automation, importer, payroll, settings, workflow
from tests.conftest import login, outbox

D = "alrawasi.example"


def emp(code):
    return Employee.query.filter_by(code=code).first()


def user(email):
    return User.query.filter_by(email=email).first()


def test_reminder_then_escalation(app, client):
    req = Request.query.filter_by(employee_id=emp("EMP-1015").id, type="leave").first()
    req.stage_since = utcnow() - timedelta(hours=30)
    db.session.commit()
    automation.hourly_sweep()
    db.session.commit()
    assert req.reminders_sent == 1
    assert any("تذكير" in m["Subject"] and f"b.alrashidi@{D}" in m["To"] for m in outbox(app))
    req.stage_since = utcnow() - timedelta(hours=50)
    db.session.commit()
    automation.hourly_sweep()
    db.session.commit()
    ceo = user(f"m.alsahli@{D}")  # مديرة بندر
    assert req.escalated_to_id == ceo.id and can_act(ceo, req)
    login(client, f"m.alsahli@{D}")
    client.post(f"/requests/{req.id}/decide", data={"decision": "approve"})
    db.session.refresh(req)
    assert req.current_stage == "hr"
    assert any("بعد التصعيد" in ev.action for ev in req.events)


def test_delegation_lets_substitute_approve(client):
    login(client, f"b.alrashidi@{D}")
    sub = emp("EMP-1014")
    client.post("/account/delegate", data={"delegate_id": sub.id, "until": (date.today() + timedelta(days=5)).isoformat()})
    req = Request.query.filter_by(employee_id=emp("EMP-1015").id, type="leave").first()
    assert can_act(sub.user, req)
    own = Request.query.filter_by(employee_id=sub.id, type="loan").first()
    assert not can_act(sub.user, own)  # البديل ما يعتمد طلبه هو


def test_change_request_retro_and_four_eyes(app, client):
    hr = user(f"h.alanazi@{D}")
    month = payroll.current_month()
    login(client, f"h.alanazi@{D}")
    client.post("/payroll/approve", data={"month": month, "confirm": "yes"})
    client.post("/logout")
    target = emp("EMP-1015")
    old_basic = target.basic
    login(client, f"n.alqahtani@{D}")  # أخصائية التوظيف ترفع الطلب
    start = date.today().replace(day=1)
    client.post("/requests/change", data={"employee_id": target.id, "kind": "salary", "effective": start.isoformat(),
                                          "basic": str(old_basic + 1000), "reason": "تعديل بعد التقييم"})
    req = Request.query.filter_by(type="change", employee_id=target.id).first()
    assert req and req.current_stage == "hr"
    assert not can_act(user(f"n.alqahtani@{D}"), req)  # اللي رفع ما يعتمد
    client.post("/logout")
    login(client, f"a.alfaifi@{D}")
    client.post(f"/requests/{req.id}/decide", data={"decision": "approve"})
    client.post("/logout")
    login(client, f"h.alanazi@{D}")
    client.post(f"/requests/{req.id}/decide", data={"decision": "approve"})
    db.session.refresh(target)
    assert target.basic == old_basic + 1000
    adj = PayrollAdjustment.query.filter_by(employee_id=target.id).first()
    assert adj and abs(adj.amount - 1000) < 0.01 and adj.month == payroll.add_months(month, 1)
    rows = payroll.lines(adj.month)
    assert any(r["emp"].id == target.id and r["additions"] == 1000 for r in rows)


def test_cutoff_moves_financial_effect(app):
    settings.set_value("cutoff_day", "1")
    db.session.commit()
    e = emp("EMP-1011")
    req, _ = workflow.submit(e, "loan", {"amount": 3000, "months": 3, "reason": "x"}, e.name)
    if date.today().day > 1:
        assert req.payroll_month == payroll.add_months(payroll.current_month(), 1)


def test_attendance_import_absence_and_backdated_leave(app, client):
    login(client, f"h.alanazi@{D}")
    e = emp("EMP-1016")
    day = date.today() - timedelta(days=3)
    while day.weekday() in (4, 5):
        day -= timedelta(days=1)
    prev = day - timedelta(days=1)
    while prev.weekday() in (4, 5):
        prev -= timedelta(days=1)
    csv_text = "AC-No.,Name,Time\n" + f"{e.attendance_id},x,{prev} 08:40:00\n{e.attendance_id},x,{prev} 16:00:00\n" \
               + f"{e.attendance_id},x,{day} 07:55:00\n"
    AttendanceDay.query.filter_by(employee_id=e.id).delete()
    db.session.commit()
    r = client.post("/attendance/import", data={"file": (io.BytesIO(csv_text.encode()), "punches.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    late = AttendanceDay.query.filter_by(employee_id=e.id, day=prev).first()
    assert late.status == "late" and late.late_minutes == 40
    settings.set_value("absence_reminder_days", "0")
    # يوم بدون بصمة بين prev و day لا يوجد دائمًا، فننشئ غيابًا صريحًا باستيراد يوم لاحق بدون بصمة للموظف
    gap = AttendanceDay(employee_id=e.id, day=prev - timedelta(days=7), status="absent", note="test")
    db.session.add(gap)
    db.session.commit()
    automation.daily_sweep()
    db.session.commit()
    assert Task.query.filter_by(key=f"absence:{e.id}:{gap.day.isoformat()}").first()
    client.post("/logout")
    login(client, f"a.hassan@{D}")
    client.post("/requests/new/leave", data={"from": gap.day.isoformat(), "days": "1"})
    req = Request.query.filter_by(employee_id=e.id, type="leave").order_by(Request.id.desc()).first()
    assert req.status == "pending"  # بأثر رجعي: لا اعتماد تلقائي
    client.post("/logout")
    login(client, f"b.alrashidi@{D}")
    client.post(f"/requests/{req.id}/decide", data={"decision": "approve"})
    client.post("/logout")
    login(client, f"a.alfaifi@{D}")
    client.post(f"/requests/{req.id}/decide", data={"decision": "approve"})
    db.session.refresh(gap)
    assert gap.status == "excused"


def test_permission_auto_and_monthly_limit(client):
    login(client, f"t.alsubaie@{D}")
    d = date.today().isoformat()
    client.post("/requests/new/permission", data={"date": d, "start": "08:00", "hours": "1"})
    r = Request.query.filter_by(type="permission").first()
    assert r.status == "approved" and r.auto
    for _ in range(3):
        client.post("/requests/new/permission", data={"date": d, "start": "10:00", "hours": "3"})
    rejected = Request.query.filter_by(type="permission", status="rejected").all()
    assert rejected and "حد الاستئذان" in rejected[-1].events[-1].note


def test_readiness_lists_blockers(app):
    e = emp("EMP-1012")
    e.iban = None
    db.session.commit()
    items = payroll.readiness(payroll.next_open_month())
    assert any("بيانات ناقصة" in i["title"] and e.name in i["title"] for i in items)


def test_excel_import_and_activation_code(app, client):
    from openpyxl import Workbook, load_workbook
    tpl = load_workbook(io.BytesIO(importer.template_xlsx()))
    ws = tpl.active
    ws.delete_rows(4, ws.max_row)
    ws.append(["", "عامل جديد", "2123456789", "باكستاني", "العمليات", "عامل", "EMP-1013", "2026-01-05", 3000, "", "",
               "محدد", "2027-01-04", 10, "الراجحي", "SA0380000000608010167519", "", "0551112233", "2027-03-01", "", "777", ""])
    ws.append(["", "", "123", "سعودي", "قسم غلط", "x", "EMP-9999", "غلط", "abc"] + [""] * 13)
    buf = io.BytesIO()
    tpl.save(buf)
    login(client, f"a.alfaifi@{D}")
    r = client.post("/import", data={"action": "validate", "file": (io.BytesIO(buf.getvalue()), "emps.xlsx")},
                    content_type="multipart/form-data")
    page = r.get_data(as_text=True)
    assert "جاهز" in page and "القسم «قسم غلط» غير معروف" in page
    payload = re.search(r'name="payload" value="([^"]+)"', page).group(1)
    r = client.post("/import", data={"action": "commit", "payload": payload})
    page = r.get_data(as_text=True)
    code = re.search(r"([A-Z0-9]{4}-[A-Z0-9]{4})", page).group(1)
    w = Employee.query.filter_by(name="عامل جديد").first()
    assert w and w.manager.code == "EMP-1013" and w.email is None and w.leave_used == policy.LEAVE_BASE - 10
    other = app.test_client()
    assert other.post("/activate", data={"code": w.code, "activation": "WRONG-CODE", "password": "Abcdef12345",
                                         "password2": "Abcdef12345"}).status_code == 200
    r = other.post("/activate", data={"code": w.code, "activation": code, "password": "Abcdef12345", "password2": "Abcdef12345"})
    assert r.status_code == 302
    assert login(other, w.code, "Abcdef12345").status_code == 302


def test_two_factor_required_for_admin(app):
    app.config["REQUIRE_2FA"] = True
    c = app.test_client()
    r = login(c, f"a.alfaifi@{D}")
    assert "/login/2fa" in r.headers["Location"]
    assert c.get("/").status_code == 302  # ما دخل للحين
    c.get("/login/2fa")
    with c.session_transaction() as s:
        secret = s["2fa_secret"]
    assert c.post("/login/2fa", data={"code": "000000"}).status_code == 200
    r = c.post("/login/2fa", data={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 302 and c.get("/").status_code == 200
    assert user(f"a.alfaifi@{D}").totp_secret == secret
    worker = app.test_client()
    assert login(worker, f"m.alghamdi@{D}").headers["Location"].endswith("/")  # الموظف العادي بدون خطوة ثانية
    app.config["REQUIRE_2FA"] = False


def test_operator_needs_grant_and_is_read_only(app):
    op = app.test_client()
    login(op, f"it.support@{D}")
    assert "/system" in op.get("/employees/").headers.get("Location", "")
    assert op.get("/system").status_code == 200
    admin = app.test_client()
    login(admin, f"a.alfaifi@{D}")
    opu = user(f"it.support@{D}")
    admin.post("/settings/support", data={"operator_id": opu.id, "hours": "2", "reason": "فحص مشكلة"})
    assert SupportGrant.query.count() == 1
    assert op.get("/employees/").status_code == 200
    assert op.post("/payroll/approve", data={"month": payroll.current_month(), "confirm": "yes"}).status_code == 403
    assert AuditLog.query.filter(AuditLog.action == "اطلاع أثناء الدعم الفني").count() >= 1
    g = SupportGrant.query.first()
    admin.post("/settings/support", data={"action": "revoke", "grant_id": g.id})
    assert "/system" in op.get("/employees/").headers.get("Location", "")


def test_gosi_new_system_rates():
    class E:
        is_saudi, gosi_new_system, basic, housing = True, True, 10000, 2500
    assert policy.gosi_rates(E(), date(2025, 6, 30)) == (0.0975, 0.1175)
    assert policy.gosi_rates(E(), date(2026, 7, 1)) == (0.1075, 0.1275)
    E.gosi_new_system = False
    assert policy.gosi_rates(E(), date(2027, 1, 1)) == (0.0975, 0.1175)


def test_new_hire_proration_and_wps_upload(app, client):
    login(client, f"a.alfaifi@{D}")
    nxt = payroll.add_months(payroll.current_month(), 1)
    start, end = payroll.month_bounds(nxt)
    hire = start.replace(day=16)
    client.post("/employees/new", data={"name": "موظف تناسبي", "email": f"p.test@{D}", "nationality": "سعودي",
                                       "department": "المالية", "title": "محاسب", "basic": "6000", "hire_date": hire.isoformat(),
                                       "contract_type": "open", "probation_days": "90", "iban": "SA0380000000608010167519",
                                       "national_id": "1098765432", "bank": "الراجحي"})
    row = next(r for r in payroll.lines(nxt) if r["emp"].name == "موظف تناسبي")
    days = (end - hire).days + 1
    assert abs(row["proration"] - round(days / ((end - start).days + 1), 4)) < 1e-6
    month = payroll.current_month()
    client.post("/payroll/approve", data={"month": month, "confirm": "yes"})
    assert client.get(f"/payroll/{month}/wps.xlsx").status_code == 200
    assert Task.query.filter_by(key=f"wps:{month}", done=False).first()
    client.post(f"/payroll/{month}/uploaded")
    assert Task.query.filter_by(key=f"wps:{month}").first().done
