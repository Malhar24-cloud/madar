"""الفحص اليومي. شغّله مرة يوميًا (cron أو Task Scheduler): flask --app madar daily
كل إجراء له مفتاح فريد، فتشغيله أكثر من مرة لا يكرر شيئًا."""
from datetime import timedelta

from .. import policy
from ..models import Employee, PayrollRun, Request, Task, User
from . import attendance, audit, mailer, settings, tasks, workflow
from .payroll import current_month, cutoff_date


def _once(key):
    return not Task.query.filter_by(key=key).first()


def roll_leave_years(today):
    """تجديد رصيد الإجازات عند ذكرى المباشرة: يُرحّل المتبقي لين الحد المحدد، ويتصفّر عدّاد المرضية."""
    done = []
    cap = settings.get_int("leave_carry_max")
    for e in Employee.query.filter_by(status="active").all():
        start = policy.leave_year_start(e, today)
        if e.leave_year_start is None:
            e.leave_year_start = start  # أول تشغيل: نعتبر المستخدم الحالي من السنة الحالية
            continue
        if e.leave_year_start >= start:
            continue
        left = policy.leave_left(e, e.leave_year_start)
        carry = min(left, cap)
        e.leave_carry, e.leave_used, e.sick_used, e.leave_year_start = carry, 0, 0, start
        audit.log("تجديد رصيد الإجازات", f"{e.name}: رصيد جديد {e.leave_left} يوم (مرحّل {carry})", automated=True)
        mailer.queue(e.email, e.name, "تجدد رصيد إجازاتك",
                     f"مرحبًا {e.first_name}،\n\nبدأت سنة إجازات جديدة لك. رصيدك الآن {e.leave_left} يوم"
                     + (f"، منها {carry} يوم مرحّلة من السنة الماضية" if carry else "") + ".\n\nالموارد البشرية",
                     employee_id=e.id, tag="إجازة")
        done.append(f"تجديد رصيد إجازات {e.name}")
    return done


def daily_sweep():
    today = policy.today()
    done = []
    for e in Employee.query.filter_by(status="active").all():
        m = e.manager
        if e.end_date and 0 <= (e.end_date - today).days <= policy.CONTRACT_ALERT_DAYS:
            key = f"renew:{e.id}:{e.end_date}"
            if _once(key):
                deadline = max(today, e.end_date - timedelta(days=policy.NOTICE_EMPLOYER))
                tasks.ensure(key, "admin", f"قرار تجديد عقد {e.name}", f"ينتهي {e.end_date} · آخر موعد للإشعار {deadline}",
                             employee_id=e.id, due=deadline, link=f"/employees/{e.id}")
                if m:
                    mailer.queue(m.email, m.name, f"طلب قرار: تجديد عقد {e.name}",
                                 f"مرحبًا {m.first_name}،\n\nينتهي عقد {e.name} ({e.title}) بتاريخ {e.end_date}، أي بعد {(e.end_date - today).days} يومًا.\n"
                                 f"نحتاج قرارك قبل {deadline} لإشعار الموظف في الوقت النظامي:\n"
                                 "- تجديد بنفس الشروط\n- تجديد مع تعديل\n- عدم التجديد\n\nمع التحية،\nالموارد البشرية",
                                 kind="draft", sensitive=True, employee_id=e.id, tag="تجديد عقد")
                done.append(f"تنبيه تجديد عقد {e.name}")
        if e.in_probation and e.probation_left <= policy.PROBATION_ALERT_DAYS:
            key = f"probation:{e.id}"
            if _once(key):
                end = e.hire_date + timedelta(days=e.probation_days)
                tasks.ensure(key, "manager", f"تقييم نهاية التجربة: {e.name}", f"تنتهي {end} · تثبيت أو تمديد",
                             employee_id=e.id, due=end - timedelta(days=3), link=f"/employees/{e.id}",
                             assignee_id=m.user.id if m and m.user else None)
                if m:
                    mailer.queue(m.email, m.name, f"تذكير: تقييم نهاية تجربة {e.name}",
                                 f"مرحبًا {m.first_name}،\n\nتنتهي فترة تجربة {e.name} بتاريخ {end}. نرجو تحديد التثبيت أو التمديد قبل {end - timedelta(days=3)}.\n\nالموارد البشرية",
                                 employee_id=e.id, tag="تجربة")
                done.append(f"تنبيه نهاية تجربة {e.name}")
        if e.iqama_expiry and 0 <= (e.iqama_expiry - today).days <= policy.IQAMA_ALERT_DAYS:
            key = f"iqama:{e.id}:{e.iqama_expiry}"
            if _once(key):
                tasks.ensure(key, "payroll", f"تجديد إقامة {e.name}", f"تنتهي {e.iqama_expiry}",
                             employee_id=e.id, due=e.iqama_expiry - timedelta(days=14), link=f"/employees/{e.id}")
                mailer.queue(e.email, e.name, "تجديد الإقامة",
                             f"مرحبًا {e.first_name}،\n\nتنتهي إقامتك بتاريخ {e.iqama_expiry}. نرجو تسليم جواز سفر ساري المفعول للموارد البشرية خلال أسبوع.\n\nالموارد البشرية",
                             employee_id=e.id, tag="إقامة")
                done.append(f"تنبيه إقامة {e.name}")
    done += roll_leave_years(today)
    done += workflow.apply_scheduled()
    done += attendance.chase_absences()
    done += _cutoff_warning(today)
    done += _wps_overdue(today)
    month = current_month()
    if today >= cutoff_date(month) and not PayrollRun.query.filter_by(month=month).first():
        key = f"payroll:{month}"
        if _once(key):
            tasks.ensure(key, "payroll", f"اعتماد مسير {month}", "المسير محسوب من الرواتب والسلف والتأمينات",
                         due=today.replace(day=27) if today.day <= 27 else today, link="/payroll")
            done.append(f"تجهيز مسير {month}")
    for d in done:
        audit.log("فحص يومي", d, automated=True)
    return done


def hourly_sweep():
    """تذكير الموافقين المتأخرين وتصعيد الطلبات. يُشغّل كل ساعة."""
    return workflow.remind_and_escalate()


def _cutoff_warning(today):
    """قبل إغلاق المسير بثلاثة أيام: كل شخص عنده طلبات تؤثر على المسير يوصله إيميل واحد فيه قائمتها."""
    from ..permissions import stage_approvers
    month = current_month()
    cut = cutoff_date(month)
    if not 0 <= (cut - today).days <= 3:
        return []
    pend = Request.query.filter(Request.status == "pending", Request.payroll_month == month).all()
    by_user = {}
    for r in pend:
        for u in stage_approvers(r, r.current_stage):
            if u.employee_id != r.employee_id:
                by_user.setdefault(u.id, (u, []))[1].append(r)
    done = []
    for uid, (u, reqs) in by_user.items():
        key = f"cutoff:{month}:{uid}"
        if not _once(key):
            continue
        tasks.ensure(key, "assigned", f"{len(reqs)} طلب يؤثر على مسير {month}", f"الإغلاق {cut}", due=cut, link="/requests?show=action",
                     assignee_id=u.id)
        lines = "\n".join(f"- {workflow.label(r)} · {r.employee.name} ({workflow.summary(r)})" for r in reqs)
        mailer.queue(u.email, u.display_name, f"قبل إغلاق الرواتب: {len(reqs)} طلب بانتظارك",
                     f"مرحبًا {u.display_name.split()[0]}،\n\nيُغلق مسير {month} يوم {cut}. هذي الطلبات تنتظر قرارك وتؤثر على رواتب الموظفين:\n{lines}\n\n"
                     "أي طلب ما يُعتمد قبل الإغلاق ينتقل أثره للشهر الجاي.\n\nمدار للموارد البشرية", tag="إغلاق المسير")
        done.append(f"تنبيه إغلاق المسير لـ{u.display_name} ({len(reqs)} طلب)")
    return done


def _wps_overdue(today):
    done = []
    for run in PayrollRun.query.filter(PayrollRun.wps_uploaded_at.is_(None)).all():
        days = (today - run.approved_at.date()).days
        if days >= policy.WPS_UPLOAD_DAYS - 2:
            key = f"wps-late:{run.month}"
            if _once(key):
                tasks.ensure(key, "admin", f"تنبيه: ملف حماية الأجور لمسير {run.month} لم يُرفع",
                             f"مرّ {days} يوم على اعتماد المسير. التأخير يعرّض المنشأة لمخالفة في مُدد",
                             due=today, link=f"/payroll/?month={run.month}")
                done.append(f"تصعيد رفع حماية الأجور {run.month}")
    return done
