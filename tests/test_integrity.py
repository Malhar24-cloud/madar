"""سلامة البيانات: أي تعديل على أي موظف ينطبق صح ويوصل أثره لكل مكان (الرصيد، المسير، المسار، المهام)،
وكل خانة إدخال ترفض القيم الغريبة."""
from datetime import date, timedelta

import pytest
from werkzeug.datastructures import MultiDict

from madar import policy
from madar.models import (AttendanceDay, Email, Employee, PayrollAdjustment, PayrollRun, Request, SalaryHistory, Task,
                          User, db)
from madar.permissions import can_act, can_view_request
from madar.services import attendance, automation, importer, lifecycle, payroll, settings, workflow
from tests.conftest import login

D = "alrawasi.example"
TODAY = policy.today


def emp(code):
    return Employee.query.filter_by(code=code).first()


def user(email):
    return User.query.filter_by(email=email).first()


def form(**kw):
    return MultiDict({k: str(v) for k, v in kw.items()})


def finish(req):
    """يعتمد كل المراحل بأول شخص يحق له. يفشل إذا علق الطلب بدون أحد يقدر يعتمده."""
    for _ in range(6):
        if req.status != "pending":
            break
        who = [u for u in User.query.filter_by(active=True).all() if can_act(u, req)]
        assert who, f"الطلب {req.id} علق عند {req.current_stage} بدون أحد يقدر يعتمده"
        workflow.approve(req, who[0])
    assert req.status == "approved"
    return req


def change(e, by, **fields):
    data = workflow.clean_data("change", form(effective=TODAY().isoformat(), reason="اختبار", **fields))
    req, _ = workflow.submit(e, "change", data, "x", requested_by=by)
    return req


# ---------------- كل موظف وكل نوع تعديل ----------------
def test_every_employee_every_change_applies(app):
    """محاكاة كاملة: لكل موظف على رأس العمل نرفع كل أنواع الطلبات والتعديلات ونعتمدها لين النهاية."""
    admin, hayfa = user(f"a.alfaifi@{D}"), user(f"h.alanazi@{D}")
    with app.test_request_context():
        for e in Employee.query.filter_by(status="active").all():
            left = e.leave_left
            busy = Request.query.filter_by(employee_id=e.id, type="leave", status="pending").first()
            if left >= 4 and not busy:
                r, _ = workflow.submit(e, "leave", workflow.clean_data("leave", form(days=4, **{"from": (TODAY() + timedelta(days=40)).isoformat()})), e.name)
                if r.status == "pending":
                    finish(r)
                assert e.leave_left == left - 4, e.code
            r, _ = workflow.submit(e, "permission", workflow.clean_data(
                "permission", form(date=TODAY().isoformat(), start="09:00", hours=2)), e.name)
            if r.status == "pending":
                finish(r)
            by = hayfa if e.user and e.user.id == admin.id else admin
            basic = e.basic
            finish(change(e, by, kind="salary", basic=basic + 500))
            finish(change(e, by, kind="promotion", title="مسمى مُحدّث"))
            finish(change(e, by, kind="bonus", amount=300))
            db.session.refresh(e)
            assert e.basic == basic + 500 and e.title == "مسمى مُحدّث", e.code
            assert SalaryHistory.query.filter_by(employee_id=e.id, field="basic").count() == 1
            assert PayrollAdjustment.query.filter_by(employee_id=e.id, amount=300).count() == 1
        run = payroll.approve(payroll.next_open_month(), admin)
        assert len(run.payslips) == Employee.query.filter_by(status="active").count()
        assert all(p.net >= 0 for p in run.payslips)


def test_hr_manager_own_change_goes_to_ceo(app):
    """تعديل راتب مدير الموارد البشرية نفسه: ما يعتمده هو، يروح للرئيس التنفيذي (مديره في الهيكل)."""
    admin, hayfa, ceo = user(f"a.alfaifi@{D}"), user(f"h.alanazi@{D}"), user(f"m.alsahli@{D}")
    with app.test_request_context():
        req = change(admin.employee, hayfa, kind="salary", basic=admin.employee.basic + 1000)
        assert not can_act(admin, req) and not can_act(hayfa, req)
        assert can_act(ceo, req)
        finish(req)


def test_no_one_approves_two_stages(app):
    admin, hayfa = user(f"a.alfaifi@{D}"), user(f"h.alanazi@{D}")
    with app.test_request_context():
        req = change(emp("EMP-1015"), user(f"b.alrashidi@{D}"), kind="bonus", amount=1000)
        workflow.approve(req, hayfa)
        assert req.current_stage == "payroll"
        assert not can_act(hayfa, req) and can_act(admin, req)


# ---------------- تغيير المدير ----------------
def test_transfer_moves_pending_requests_to_new_manager(app):
    worker, rim = emp("EMP-1015"), emp("EMP-1010")
    with app.test_request_context():
        leave = Request.query.filter_by(employee_id=worker.id, type="leave", status="pending").first()
        assert leave.current_stage == "manager"
        old = worker.manager.user
        finish(change(worker, user(f"a.alfaifi@{D}"), kind="transfer", department="المبيعات", manager_id=rim.id))
        assert worker.manager_id == rim.id and worker.department == "المبيعات"
        assert can_act(rim.user, leave) and not can_act(old, leave)
        t = Task.query.filter_by(key=f"req:{leave.id}:0").first()
        assert t and not t.done and t.assignee_id == rim.user.id


def test_transfer_rejects_management_loop(app):
    bandar, mohammed = emp("EMP-1013"), emp("EMP-1015")
    with app.test_request_context():
        req = change(bandar, user(f"a.alfaifi@{D}"), kind="transfer", department="العمليات", manager_id=mohammed.id)
        assert req.status == "rejected"


def test_offboarding_manager_moves_team_and_settles_wages(app):
    bandar, ceo = emp("EMP-1013"), emp("EMP-1001")
    team = [e.id for e in bandar.reports]
    leave = Request.query.filter_by(employee_id=emp("EMP-1015").id, type="leave", status="pending").first()
    with app.test_request_context():
        res = lifecycle.offboard(bandar, "term", TODAY(), user(f"a.alfaifi@{D}"))
        assert all(db.session.get(Employee, i).manager_id == ceo.id for i in team)
        assert can_act(ceo.user, leave)
        start = date(TODAY().year, TODAY().month, 1)
        expect = bandar.gross * ((TODAY() - start).days + 1) / 30
        assert res["final"] > res["eos"]["amount"] and res["final"] > 0
        assert abs(res["final"] - res["eos"]["amount"] - res["leave_cash"] + res["loans_left"]) > expect * 0.5


# ---------------- الرواتب ----------------
def test_mid_month_raise_is_prorated(app):
    e = emp("EMP-1016")
    month = payroll.next_open_month()
    eff = date.fromisoformat(month + "-11")
    if eff > TODAY() + timedelta(days=300):
        pytest.skip("خارج نافذة السريان")
    with app.test_request_context():
        old = e.gross
        workflow.apply_changes(e, {"basic": e.basic + 3000}, eff)
        adj = PayrollAdjustment.query.filter_by(employee_id=e.id, month=month).all()
        start, end = payroll.month_bounds(month)
        expected = -round((e.gross - old) * 10 / ((end - start).days + 1), 2)
        assert any(abs(a.amount - expected) < 0.01 for a in adj)


def test_sick_leave_after_30_days_reduces_pay(app):
    e = emp("EMP-1004")
    e.sick_used = 28
    db.session.commit()
    with app.test_request_context():
        r, _ = workflow.submit(e, "sick", workflow.clean_data("sick", form(days=5, reason="R-77", **{"from": TODAY().isoformat()})), e.name)
        finish(r)
    adj = PayrollAdjustment.query.filter_by(employee_id=e.id, request_id=r.id).one()
    assert abs(adj.amount + round(3 * 0.25 * e.gross / 30, 2)) < 0.01  # 3 أيام بثلاثة أرباع الأجر


def test_payroll_only_next_open_month(app):
    admin = user(f"a.alfaifi@{D}")
    with app.test_request_context():
        with pytest.raises(ValueError):
            payroll.approve("2024-01", admin)
        with pytest.raises(ValueError):
            payroll.approve(payroll.add_months(payroll.next_open_month(), 3), admin)


def test_negative_net_blocks_payroll(app):
    e = emp("EMP-1018")
    db.session.add(PayrollAdjustment(employee_id=e.id, month=payroll.next_open_month(), amount=-e.gross * 2, reason="اختبار"))
    db.session.commit()
    with app.test_request_context():
        with pytest.raises(ValueError):
            payroll.approve(payroll.next_open_month(), user(f"a.alfaifi@{D}"))
    assert any("صافي سالب" in i["title"] for i in payroll.readiness(payroll.next_open_month()))


# ---------------- الإجازات ----------------
def test_overlapping_leave_rejected(app):
    e = emp("EMP-1011")
    start = (TODAY() + timedelta(days=50)).isoformat()
    with app.test_request_context():
        r1, _ = workflow.submit(e, "leave", workflow.clean_data("leave", form(days=2, **{"from": start})), e.name)
        assert r1.status == "approved"
        r2, _ = workflow.submit(e, "sick", workflow.clean_data("sick", form(days=1, reason="R", **{"from": start})), e.name)
        assert r2.status == "rejected"


def test_balance_rechecked_at_approval(app):
    e = emp("EMP-1011")
    with app.test_request_context():
        r, _ = workflow.submit(e, "leave", workflow.clean_data("leave", form(days=10, **{"from": (TODAY() + timedelta(days=60)).isoformat()})), e.name)
        workflow.approve(r, user(f"r.aldosari@{D}"))
        e.leave_used = e.entitlement  # انصرف الرصيد بعد التقديم
        with pytest.raises(workflow.RequestError):
            workflow.approve(r, user(f"n.alqahtani@{D}"))


def test_leave_year_rollover_with_carry_cap(app):
    e = emp("EMP-1014")
    e.leave_year_start = policy.leave_year_start(e) - timedelta(days=365)
    e.leave_used, e.sick_used = 2, 9
    settings.set_value("leave_carry_max", "10")
    db.session.commit()
    with app.test_request_context():
        automation.roll_leave_years(TODAY())
    assert e.leave_used == 0 and e.sick_used == 0 and e.leave_carry == 10
    assert e.leave_left == e.entitlement + 10
    with app.test_request_context():
        automation.roll_leave_years(TODAY())  # مرة ثانية ما تغير شي
    assert e.leave_carry == 10


# ---------------- المدخلات ----------------
@pytest.mark.parametrize("rtype,fields", [
    ("loan", {"amount": "nan", "months": "2"}),
    ("loan", {"amount": "inf", "months": "2"}),
    ("loan", {"amount": "1e30", "months": "2"}),
    ("loan", {"amount": "500", "months": "2.5"}),
    ("leave", {"days": "3", "from": "9999-12-31"}),
    ("leave", {"days": "-1", "from": "2026-12-01"}),
    ("permission", {"date": "2026-10-04", "start": "25:99", "hours": "1"}),
    ("permission", {"date": "2026-10-04", "start": "10:00", "hours": "nan"}),
    ("change", {"kind": "bonus", "amount": "inf", "effective": "2026-10-04", "reason": "x"}),
    ("change", {"kind": "salary", "basic": "1e9", "effective": "2026-10-04", "reason": "x"}),
    ("change", {"kind": "salary", "basic": "9000", "effective": "0001-01-01", "reason": "x"}),
    ("change", {"kind": "evil", "effective": "2026-10-04", "reason": "x"}),
    ("resign", {"last_day": "1900-01-01"}),
])
def test_bad_request_inputs_rejected(app, rtype, fields):
    with pytest.raises(workflow.RequestError):
        workflow.clean_data(rtype, MultiDict(fields))


def test_new_employee_form_rejects_bad_values(client):
    login(client, f"n.alqahtani@{D}")
    bad = {"name": "س", "nationality": "سعودي", "department": "المالية", "title": "محاسب", "basic": "inf",
           "hire_date": "0001-01-01", "email": "x@y.com\r\nBcc: evil@evil.com", "iban": "SA0000000000000000000000",
           "national_id": "999", "phone": "abc"}
    r = client.post("/employees/new", data=bad)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and Employee.query.filter_by(name="س").first() is None
    for msg in ("الراتب الأساسي", "تاريخ المباشرة", "البريد غير صحيح", "الآيبان", "رقم الهوية", "الجوال"):
        assert msg in page


def test_recruiter_cannot_take_over_manager_without_email(client):
    bandar = emp("EMP-1013")
    bandar.email = None
    bandar.user.email = None
    db.session.commit()
    login(client, f"n.alqahtani@{D}")
    assert client.post(f"/employees/{bandar.id}/activation").status_code == 403
    assert bandar.user.password_hash


def test_approval_emails_hidden_from_non_payroll_outbox(client):
    m = Email.query.filter(Email.tag == "موافقة").first()
    assert m
    login(client, f"n.alqahtani@{D}")
    assert client.get(f"/outbox/{m.id}").status_code == 403


def test_manager_cannot_see_salary_change_he_did_not_raise(app):
    with app.test_request_context():
        req = change(emp("EMP-1015"), user(f"a.alfaifi@{D}"), kind="salary", basic=20000)
    assert not can_view_request(user(f"b.alrashidi@{D}"), req)


def test_leaves_holder_sees_hr_stage_task(client, app):
    with app.test_request_context():
        r, _ = workflow.submit(emp("EMP-1011"), "leave", workflow.clean_data(
            "leave", form(days=8, **{"from": (TODAY() + timedelta(days=70)).isoformat()})), "x")
        workflow.approve(r, user(f"r.aldosari@{D}"))
        db.session.commit()
    login(client, f"n.alqahtani@{D}")
    assert f"/requests/{r.id}".encode() in client.get("/tasks").data


def test_import_payload_bound_to_user(app):
    with app.test_request_context():
        token = importer.pack([{"data": {"name": "x"}, "errors": []}], user_id=1)
        assert importer.unpack(token, 1)
        with pytest.raises(ValueError):
            importer.unpack(token, 2)


def test_attendance_skips_own_and_ambiguous_and_old_punches(app):
    from datetime import datetime, time as t
    mine = emp("EMP-1006")
    other = emp("EMP-1016")
    other.attendance_id = "1006"  # يساوي نهاية الرقم الوظيفي لهيفاء: غامض
    db.session.commit()
    day = TODAY() - timedelta(days=1)
    with app.test_request_context():
        res = attendance.import_punches([(mine.attendance_id, datetime.combine(day, t(8, 0))),
                                         ("1006", datetime.combine(day, t(8, 0))),
                                         (emp("EMP-1017").attendance_id, datetime(1900, 1, 1, 8))],
                                        skip_employee_id=mine.id)
    assert res["skipped"] == 2 and "1006" in res["unknown"]


def test_safe_next_rejects_tricks(app):
    from madar.routes.auth import _safe_next
    for bad in ("//evil.com", "/\\evil.com", "/\t/evil.com", "https://evil.com", "/\n/evil.com", "javascript:alert(1)"):
        assert _safe_next(bad) is None, bad
    assert _safe_next("/requests/3") == "/requests/3"


# ---------------- ثغرات المراجعة الثانية ----------------
def test_department_manager_cannot_approve_payroll_stage(app):
    """لما مسؤول الرواتب الوحيد هو اللي رفع التعديل، مرحلة الرواتب تروح لمدير الموارد البشرية، مو لمدير القسم."""
    hayfa, admin, bandar = user(f"h.alanazi@{D}"), user(f"a.alfaifi@{D}"), user(f"b.alrashidi@{D}")
    with app.test_request_context():
        req = change(emp("EMP-1014"), hayfa, kind="bonus", amount=5000)
        workflow.approve(req, admin)
        assert req.current_stage == "payroll"
        assert not can_act(bandar, req) and not can_act(user(f"m.alsahli@{D}"), req)
        assert can_act(admin, req)  # الشركة صغيرة: ما فيه غيره، فيُسمح له بالمرحلة الثانية


def test_delegate_cannot_approve_request_of_own_boss(app):
    ceo, bandar, mohammed = emp("EMP-1001"), emp("EMP-1013"), emp("EMP-1015")
    ceo.delegate_id, ceo.delegate_until = mohammed.id, TODAY() + timedelta(days=5)
    db.session.commit()
    with app.test_request_context():
        r, _ = workflow.submit(bandar, "loan", workflow.clean_data("loan", form(amount=1000, months=2)), bandar.name)
        assert r.current_stage == "manager"
        assert not can_act(mohammed.user, r) and can_act(ceo.user, r)


def test_payroll_cannot_run_ahead_into_future_months(app):
    admin = user(f"a.alfaifi@{D}")
    with app.test_request_context():
        m = payroll.next_open_month()
        if m == payroll.current_month():
            payroll.approve(m, admin)
        with pytest.raises(ValueError):
            payroll.approve(payroll.next_open_month(), admin)
