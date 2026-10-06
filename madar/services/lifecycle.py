"""التعيين وإنهاء الخدمة. كل خطوة تُسجل في سجل التدقيق."""
from datetime import timedelta

from flask import current_app

from .. import policy
from ..models import Asset, Employee, User, db
from . import audit, letters, mailer, tasks, tokens, workflow

SYSTEMS_BY_DEPT = {
    "تقنية المعلومات": ["خوادم الشركة", "لوحة الشبكة", "GitLab"],
    "المالية": ["النظام المحاسبي", "بوابة البنك"],
    "الموارد البشرية": ["مدار", "ملفات الموظفين"],
    "المبيعات": ["نظام العملاء CRM"],
    "العمليات": ["نظام إدارة المشاريع"],
    "الإدارة العليا": ["التقارير المالية"],
}


def next_code():
    """أكبر رقم في الأرقام الوظيفية + 1. يتحمّل أرقامًا بصيغ مختلفة من الاستيراد (مثل A12 أو 7781)."""
    import re
    nums = [int(m.group(1)) for (c,) in db.session.query(Employee.code).all() if (m := re.search(r"(\d+)$", c or ""))]
    n = max(nums + [1000]) + 1
    while Employee.query.filter_by(code=f"EMP-{n}").first():
        n += 1
    return f"EMP-{n}"


def onboard(data, actor):
    """data: قاموس نظيف من النموذج. يرجع (الموظف، رابط تعيين كلمة المرور)."""
    email = (data.get("email") or "").strip().lower() or None
    if email and (Employee.query.filter_by(email=email).first() or User.query.filter_by(email=email).first()):
        raise ValueError("البريد مستخدم لموظف آخر.")
    basic = int(data["basic"])
    code = next_code()
    emp = Employee(
        code=code, name=data["name"].strip(), email=email, nationality=data.get("nationality") or "سعودي",
        is_saudi="سعود" in (data.get("nationality") or "سعودي"), department=data["department"], title=data["title"].strip(),
        manager_id=data.get("manager_id"), basic=basic,
        housing=int(data["housing"]) if data.get("housing") not in (None, "") else round(basic * policy.HOUSING_PCT),
        transport=int(data["transport"]) if data.get("transport") not in (None, "") else round(basic * policy.TRANSPORT_PCT),
        hire_date=data["hire_date"], contract_type=data.get("contract_type", "open"), end_date=data.get("end_date"),
        probation_days=min(int(data.get("probation_days") or policy.PROBATION_DAYS), policy.PROBATION_MAX),
        national_id=data.get("national_id") or None, bank=data.get("bank") or None, iban=data.get("iban") or None,
        clauses=data.get("clauses") or [], phone=data.get("phone") or None, attendance_id=data.get("attendance_id") or None,
        gosi_new_system=bool(data.get("gosi_new_system")),
    )
    emp.contract_no = f"CT-{emp.hire_date.year}-{code.split('-')[-1]}"
    if not emp.is_saudi:
        emp.iqama_expiry = data.get("iqama_expiry")
    db.session.add(emp)
    db.session.flush()

    user = User(email=email, role="employee", employee_id=emp.id, active=True)
    db.session.add(user)
    db.session.flush()
    if email:
        raw = tokens.issue("set_password", user.id, current_app.config["SET_PASSWORD_TOKEN_HOURS"])
        link = f"{current_app.config['BASE_URL']}/set-password/{raw}"
    else:
        from .importer import activation_code
        link = activation_code()  # يُطبع ويُسلّم للموظف، ولا يُحفظ إلا بصمته
        tokens.issue_code("activate", user.id, link, hours=24 * 14)

    db.session.add(Asset(employee_id=emp.id, type="لابتوب", tag=f"LT-{code.split('-')[-1]}", status="بانتظار التسليم"))
    manager = db.session.get(Employee, emp.manager_id) if emp.manager_id else None
    mailer.queue(email, emp.name, f"أهلًا بك في {current_app.config['COMPANY_NAME']}",
                 f"مرحبًا {emp.first_name}،\n\nيسعدنا انضمامك بوظيفة {emp.title} في قسم {emp.department} ابتداءً من {emp.hire_date}.\n"
                 + (f"مديرك المباشر: {manager.name}.\n" if manager else "")
                 + "\nقبل يوم المباشرة نرجو تجهيز: صورة الهوية أو الإقامة، شهادة الآيبان، والمؤهلات العلمية.\n"
                 f"فعّل حسابك في مدار من الرابط أدناه (صالح {current_app.config['SET_PASSWORD_TOKEN_HOURS']} ساعة).\n\nأهلًا بك،\nالموارد البشرية",
                 employee_id=emp.id, actions=[["تفعيل حسابي", link]] if email else [], tag="تعيين")
    tasks.ensure(f"it:{emp.id}", "admin", f"تجهيز جهاز وحسابات {emp.name}",
                 "بريد الشركة، " + "، ".join(SYSTEMS_BY_DEPT.get(emp.department, [])), employee_id=emp.id,
                 due=max(policy.today(), emp.hire_date - timedelta(days=3)), link=f"/employees/{emp.id}")
    tasks.ensure(f"qiwa:{emp.id}", "recruit", f"توثيق عقد {emp.name} في قوى", f"العقد {emp.contract_no} جاهز",
                 employee_id=emp.id, due=max(policy.today(), emp.hire_date - timedelta(days=1)), link=f"/employees/{emp.id}")
    if manager and manager.user:
        tasks.ensure(f"welcome:{emp.id}", "manager", f"استقبال {emp.name}", f"أول يوم عمل {emp.hire_date}",
                     employee_id=emp.id, due=emp.hire_date, assignee_id=manager.user.id, link=f"/employees/{emp.id}")
    audit.log("تعيين موظف", f"{emp.name} ({code}) · {emp.title} · {emp.department}")
    return emp, link


def unpaid_wages(emp, last_day):
    """راتب الأيام من بداية أول شهر ما انعتمد مسيره لين آخر يوم عمل، بعد حصة التأمينات،
    والإضافات أو الخصومات المعلقة. لأن الموظف بعد إنهاء الخدمة ما يدخل أي مسير."""
    from ..models import PayrollAdjustment
    from .payroll import add_months, month_bounds, next_open_month
    wages, m = 0.0, next_open_month()
    while m <= last_day.strftime("%Y-%m"):
        start, end = month_bounds(m)
        a, b = max(start, emp.hire_date), min(end, last_day)
        if b >= a:
            f = ((b - a).days + 1) / ((end - start).days + 1)
            wages += emp.gross * f - policy.gosi_employee(emp, on=end, factor=f)
        m = add_months(m, 1)
    adj = PayrollAdjustment.query.filter_by(employee_id=emp.id, applied_run_id=None).all()
    pending = sum(a.amount for a in adj)
    for a in adj:
        a.reason = (a.reason + " · سُوّيت في مخالصة نهاية الخدمة")[:300]
        a.month = "0000-00"  # خرجت من أي مسير قادم لأنها دخلت في المخالصة
    return round(wages, 2), round(pending, 2)


def offboard(emp, reason, last_day, actor_user):
    if emp.status != "active":
        raise ValueError("الموظف منتهية خدمته مسبقًا.")
    eos = policy.end_of_service(emp, reason, on=last_day)
    leave_cash = round(emp.leave_left * emp.gross / 30, 2)
    loans_left = round(sum(l.remaining for l in emp.loans if l.is_active), 2)
    wages, pending_adj = unpaid_wages(emp, last_day)
    final = round(eos["amount"] + leave_cash + wages + pending_adj - loans_left, 2)

    emp.status, emp.left_date, emp.left_reason, emp.final_amount = "left", last_day, reason, final
    steps = []
    if emp.user:
        emp.user.active = False
        emp.user.session_version += 1
        tokens.revoke_user(emp.user.id)
        steps.append("تعطيل حساب مدار وإنهاء كل الجلسات المفتوحة وإلغاء الروابط المعلقة")
    for loan in emp.loans:
        if loan.is_active:
            loan.paid = loan.months
    if loans_left:
        steps.append(f"تسوية سلف بقيمة {loans_left:,.0f} من المستحقات")
    if wages:
        steps.append(f"راتب الأيام اللي ما انصرفت لين آخر يوم: {wages:,.0f} ريال (بعد التأمينات)")
    if pending_adj:
        steps.append(f"إضافات وخصومات معلقة ({pending_adj:+,.0f}) أُدخلت في المخالصة")
    held = [a for a in emp.assets if a.status != "مسترجعة"]
    for a in held:
        a.status = "بانتظار الاسترجاع"
    workflow.cancel_pending_for(emp, actor_user.display_name)
    team = list(emp.reports)
    for r in team:
        r.manager_id = emp.manager_id if emp.manager_id != r.id else None
    db.session.flush()
    for r in team:
        db.session.expire(r, ["manager"])
        workflow.reroute_pending(r)
    if team:
        steps.append(f"نقل {len(team)} موظف لمدير {emp.name}، وتحويل طلباتهم المعلقة له")
    tasks.close(f"resign:{emp.id}")

    systems = ["البريد الوظيفي", "VPN", "بطاقة الدخول"] + SYSTEMS_BY_DEPT.get(emp.department, [])
    tasks.ensure(f"revoke:{emp.id}", "admin", f"سحب صلاحيات {emp.name} في الأنظمة الخارجية", "، ".join(systems),
                 employee_id=emp.id, due=policy.today(), link=f"/employees/{emp.id}")
    if held:
        tasks.ensure(f"assets:{emp.id}", "admin", f"استرجاع عهد {emp.name}", "، ".join(f"{a.type} ({a.tag})" for a in held),
                     employee_id=emp.id, due=last_day, link=f"/employees/{emp.id}")
    tasks.ensure(f"final:{emp.id}", "payroll", f"صرف مستحقات {emp.name}", f"{final:,.0f} ريال بعد المخالصة",
                 employee_id=emp.id, due=last_day + timedelta(days=7), link=f"/employees/{emp.id}")

    letter = letters.issue("term", emp, issued_by=actor_user.display_name)
    mailer.queue(emp.email, emp.name, "إشعار إنهاء الخدمة والمستحقات",
                 f"السيد/ة {emp.name}،\n\nنفيدكم بانتهاء علاقتكم التعاقدية مع الشركة اعتبارًا من {last_day}.\n"
                 f"صافي مستحقاتكم النهائية {final:,.0f} ريال، وتُصرف بعد استكمال المخالصة وتسليم العهد.\n"
                 f"مرجع الخطاب: {letter.number}.\n\nنشكركم على ما قدمتموه.\nإدارة الموارد البشرية",
                 kind="draft", sensitive=True, employee_id=emp.id, tag="إنهاء خدمة")
    audit.log("إنهاء خدمة", f"{emp.name} ({emp.code}) · {'استقالة' if reason == 'resign' else 'إنهاء'} · مستحقات {final:,.0f}")
    return {"eos": eos, "leave_cash": leave_cash, "loans_left": loans_left, "final": final, "steps": steps,
            "systems": systems, "assets": held, "letter": letter}
