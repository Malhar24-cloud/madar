import calendar
import csv
import io
from datetime import date, timedelta

from .. import policy
from ..models import AttendanceDay, Employee, PayrollAdjustment, PayrollRun, Payslip, Request, db, utcnow
from . import audit, mailer, settings, tasks


def month_bounds(month):
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def add_months(month, n):
    y, m = map(int, month.split("-"))
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def current_month():
    return policy.today().strftime("%Y-%m")


def cutoff_date(month):
    start, end = month_bounds(month)
    return min(end, start.replace(day=min(settings.get_int("cutoff_day"), end.day)))


def next_open_month():
    m = current_month()
    while PayrollRun.query.filter_by(month=m).first():
        m = add_months(m, 1)
    return m


def month_for(d, ignore_cutoff=False):
    """أي مسير يدخل فيه أثر طلب مرتبط بتاريخ معين. ما يجي بعد يوم الإغلاق ينتقل للشهر الجاي تلقائيًا."""
    month = d.strftime("%Y-%m")
    if ignore_cutoff:
        return month
    if policy.today() > cutoff_date(month) and month <= current_month():
        month = add_months(current_month(), 1)
    open_m = next_open_month()
    return max(month, open_m)


def _proration(e, month):
    start, end = month_bounds(month)
    if e.hire_date > end:
        return 0.0
    if e.hire_date > start:
        return round(((end - e.hire_date).days + 1) / ((end - start).days + 1), 4)
    return 1.0


def lines(month, deduct_absence=False):
    start, end = month_bounds(month)
    emps = Employee.query.filter(Employee.status == "active", Employee.hire_date <= end).order_by(Employee.code).all()
    out = []
    for e in emps:
        f = _proration(e, month)
        gross = round(e.gross * f, 2)
        gosi = policy.gosi_employee(e, on=end, factor=f)
        employer_gosi = policy.gosi_employer(e, on=end, factor=f)
        loan = round(sum(l.installment for l in e.loans if l.is_active), 2)
        adj = PayrollAdjustment.query.filter_by(employee_id=e.id, month=month, applied_run_id=None).all()
        additions = round(sum(a.amount for a in adj if a.amount > 0), 2)
        deductions = round(-sum(a.amount for a in adj if a.amount < 0), 2)
        absent = AttendanceDay.query.filter(AttendanceDay.employee_id == e.id, AttendanceDay.day.between(start, end),
                                            AttendanceDay.status.in_(["absent", "deducted"])).all()
        deducted_days = [a for a in absent if a.status == "deducted" or deduct_absence]
        absence = round(len(deducted_days) * e.gross / 30, 2)
        notes = []
        if f < 1:
            notes.append(f"راتب تناسبي من {e.hire_date} ({f * 100:.0f}%)")
        notes += [a.reason for a in adj]
        if deducted_days:
            notes.append(f"خصم غياب {len(deducted_days)} يوم")
        net = round(gross + additions - deductions - gosi - loan - absence, 2)
        out.append({"emp": e, "basic": e.basic, "housing": e.housing, "transport": e.transport, "proration": f,
                    "gross": gross, "gosi": gosi, "employer_gosi": employer_gosi, "loan": loan, "absence": absence,
                    "additions": additions, "deductions": deductions, "net": net, "notes": notes, "adjustments": adj,
                    "unexplained": len([a for a in absent if a.status == "absent"])})
    return out


def totals(rows):
    keys = ("gross", "gosi", "loan", "net", "additions", "deductions", "absence", "employer_gosi")
    return {k: round(sum(r.get(k, 0) or 0 for r in rows), 2) for k in keys}


def readiness(month):
    """قائمة بما يعيق إغلاق المسير. هذي اللي تخلي مسؤول الرواتب يتابع من شاشة وحدة بدل المكالمات."""
    start, end = month_bounds(month)
    items = []
    pend = Request.query.filter(Request.status == "pending", Request.payroll_month == month).all()
    for r in pend:
        from .workflow import STAGE_LABELS, label
        from ..permissions import stage_approvers
        who = ", ".join(u.display_name for u in stage_approvers(r, r.current_stage)[:2]) if r.current_stage else ""
        items.append({"level": "bad", "title": f"{label(r)} معلق · {r.employee.name}",
                      "detail": f"عند {STAGE_LABELS.get(r.current_stage, '')} ({who}) منذ {int(r.hours_waiting)} ساعة",
                      "link": f"/requests/{r.id}"})
    absences = AttendanceDay.query.filter(AttendanceDay.day.between(start, end), AttendanceDay.status == "absent").all()
    by_emp = {}
    for a in absences:
        by_emp.setdefault(a.employee, []).append(a.day)
    for emp, days in by_emp.items():
        items.append({"level": "warn", "title": f"غياب غير مبرر · {emp.name}",
                      "detail": f"{len(days)} يوم بدون طلب: " + "، ".join(d.strftime("%m/%d") for d in sorted(days)[:6]),
                      "link": f"/attendance?month={month}"})
    for e in Employee.query.filter(Employee.status == "active", Employee.hire_date <= end).all():
        missing = [lbl for v, lbl in ((e.iban, "الآيبان"), (e.national_id, "رقم الهوية/الإقامة"), (e.bank, "البنك")) if not v]
        if missing:
            items.append({"level": "bad", "title": f"بيانات ناقصة · {e.name}", "detail": "ناقص: " + "، ".join(missing),
                          "link": f"/employees/{e.id}"})
        elif e.iban and not (e.iban.startswith("SA") and len(e.iban) == 24):
            items.append({"level": "bad", "title": f"آيبان غير صحيح · {e.name}", "detail": e.iban, "link": f"/employees/{e.id}"})
        if start <= e.hire_date <= end:
            items.append({"level": "info", "title": f"موظف جديد هذا الشهر · {e.name}",
                          "detail": f"باشر {e.hire_date} · راتب تناسبي", "link": f"/employees/{e.id}"})
    for r in lines(month):
        due = r["gross"] + r["additions"]
        taken = r["loan"] + r["deductions"] + r["absence"]
        if r["net"] < 0:
            items.append({"level": "bad", "title": f"صافي سالب · {r['emp'].name}", "detail": f"{r['net']:,.0f} ريال",
                          "link": f"/employees/{r['emp'].id}"})
        elif due and taken > due / 2:
            items.append({"level": "warn", "title": f"الحسميات تتجاوز نصف الأجر · {r['emp'].name}",
                          "detail": f"{taken:,.0f} من {due:,.0f} ريال. المادة 92 تمنع تجاوز النصف إلا بحكم أو موافقة",
                          "link": f"/employees/{r['emp'].id}"})
    for a in PayrollAdjustment.query.filter_by(month=month, applied_run_id=None).all():
        items.append({"level": "info", "title": f"{'إضافة' if a.amount > 0 else 'خصم'} {abs(a.amount):,.0f} · {a.employee.name}",
                      "detail": a.reason, "link": f"/employees/{a.employee_id}"})
    return items


def approve(month, user, deduct_absence=False):
    if PayrollRun.query.filter_by(month=month).first():
        raise ValueError("مسير هذا الشهر معتمد مسبقًا.")
    if month > current_month():
        raise ValueError(f"ما يُعتمد مسير شهر ما بدأ بعد ({month}). السلف والكشوف تنحسب شهر بشهر.")
    if month != next_open_month():
        # ما يُعتمد إلا الشهر المفتوح التالي بالترتيب: يمنع مسيرات وهمية لأشهر ماضية أو مستقبلية (كانت تسدد السلف بالغلط)
        raise ValueError(f"المسير المفتوح الآن هو {next_open_month()}. اعتمد الأشهر بالترتيب.")
    bad = [r for r in lines(month, deduct_absence) if r["net"] < 0]
    if bad:
        raise ValueError("صافي راتب سالب عند: " + "، ".join(r["emp"].name for r in bad[:5]) + ". راجع الخصومات قبل الاعتماد.")
    rows = lines(month, deduct_absence)
    run = PayrollRun(month=month, approved_by=user.display_name, total_net=totals(rows)["net"])
    db.session.add(run)
    db.session.flush()
    start, end = month_bounds(month)
    for r in rows:
        e = r["emp"]
        db.session.add(Payslip(run_id=run.id, employee_id=e.id, basic=r["basic"], housing=r["housing"], transport=r["transport"],
                               proration=r["proration"], gosi=r["gosi"], loan=r["loan"], absence=r["absence"],
                               additions=r["additions"], deductions=r["deductions"], net=r["net"], bank=e.bank, iban=e.iban,
                               notes=r["notes"]))
        for a in r["adjustments"]:
            a.applied_run_id = run.id
        for loan in e.loans:
            if loan.is_active:
                loan.paid += 1
        if deduct_absence:
            AttendanceDay.query.filter(AttendanceDay.employee_id == e.id, AttendanceDay.day.between(start, end),
                                       AttendanceDay.status == "absent").update({"status": "deducted"}, synchronize_session=False)
        mailer.queue(e.email, e.name, f"كشف راتب {month}",
                     f"مرحبًا {e.first_name}،\n\nكشف راتبك لشهر {month}:\nالإجمالي: {r['gross']:,.2f}\n"
                     + (f"إضافات: +{r['additions']:,.2f}\n" if r["additions"] else "")
                     + f"التأمينات: -{r['gosi']:,.2f}\n"
                     + (f"قسط السلفة: -{r['loan']:,.2f}\n" if r["loan"] else "")
                     + (f"خصومات: -{r['deductions'] + r['absence']:,.2f}\n" if r["deductions"] or r["absence"] else "")
                     + f"الصافي: {r['net']:,.2f} ريال\n\nالتفاصيل في بوابة مدار.\n\nالموارد البشرية",
                     employee_id=e.id, tag="كشف راتب")
    tasks.close(f"payroll:{month}")
    tasks.ensure(f"wps:{month}", "payroll", f"رفع ملف حماية الأجور لمسير {month}",
                 f"نزّل الملف من صفحة الرواتب وارفعه في مُدد خلال {policy.WPS_UPLOAD_DAYS} أيام",
                 due=policy.today() + timedelta(days=policy.WPS_UPLOAD_DAYS), link=f"/payroll/?month={month}")
    audit.log("اعتماد مسير الرواتب", f"{month} · {len(rows)} موظف · صافي {run.total_net:,.2f}")
    return run


def mark_wps_uploaded(run, user):
    run.wps_uploaded_at, run.wps_uploaded_by = utcnow(), user.display_name
    tasks.close(f"wps:{run.month}")
    audit.log("رفع حماية الأجور", f"مسير {run.month}")


WPS_HEADERS = ["National ID / Iqama", "Employee Name", "Bank", "IBAN", "Basic Salary", "Housing Allowance",
               "Other Allowances", "Deductions", "Net Salary", "Payment Date"]


def wps_rows(run):
    _, end = month_bounds(run.month)
    for p in sorted(run.payslips, key=lambda x: x.employee.code):
        e = p.employee
        other = round(p.transport * p.proration + p.additions, 2)
        ded = round(p.gosi + p.loan + p.absence + p.deductions, 2)
        yield [e.national_id or "", e.name, p.bank or "", p.iban or "", f"{p.basic * p.proration:.2f}",
               f"{p.housing * p.proration:.2f}", f"{other:.2f}", f"{ded:.2f}", f"{p.net:.2f}", end.isoformat()]


def wps_csv(run):
    """ملف حماية الأجور: UTF-8، نقطة للكسور العشرية، واسم ملف بأحرف إنجليزية. القالب الرسمي يختلف حسب بنك المنشأة،
    فراجع ترتيب الأعمدة مع البنك أو قالب مُدد قبل أول رفع."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(WPS_HEADERS)
    from ..security import safe_cell
    for row in wps_rows(run):
        w.writerow([safe_cell(c) for c in row])
    return "﻿" + buf.getvalue()


def wps_xlsx(run):
    from openpyxl import Workbook

    from ..security import safe_cell
    wb = Workbook()
    ws = wb.active
    ws.title = "WPS"
    ws.append(WPS_HEADERS)
    for row in wps_rows(run):
        ws.append([safe_cell(c) for c in (row[0], row[1], row[2], row[3])] + [float(x) for x in row[4:9]] + [safe_cell(row[9])])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
