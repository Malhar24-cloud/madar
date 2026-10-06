"""دورة الطلبات: تقديم ← فحص آلي ← اعتماد تلقائي للروتيني ← موافقات متتالية مع تذكير وتصعيد ← تنفيذ الأثر."""
from datetime import date, datetime, timedelta

from flask import current_app

from .. import policy
from .. import validate as v
from ..models import (AttendanceDay, Employee, Loan, PayrollAdjustment, PayrollRun, Request, RequestEvent,
                      SalaryHistory, ScheduledChange, User, db, utcnow)
from ..permissions import can_act, stage_approvers, subordinates
from . import audit, letters, mailer, rules, settings, tasks, tokens

REQUEST_TYPES = {
    "leave": {"label": "إجازة سنوية", "stages": ["manager", "hr"], "self": True},
    "sick": {"label": "إجازة مرضية", "stages": ["manager", "hr"], "self": True},
    "permission": {"label": "استئذان", "stages": ["manager"], "self": True},
    "salary_cert": {"label": "تعريف بالراتب", "stages": ["hr"], "self": True},
    "exp_cert": {"label": "شهادة خبرة", "stages": ["hr"], "self": True},
    "loan": {"label": "سلفة", "stages": ["manager", "payroll"], "self": True},
    "resign": {"label": "استقالة", "stages": ["manager", "hr"], "self": True},
    "change": {"label": "تعديل وظيفي أو مالي", "stages": ["hr", "payroll"], "self": False},
}
CHANGE_KINDS = {
    "salary": "تعديل راتب أو بدلات",
    "promotion": "ترقية",
    "transfer": "نقل",
    "bonus": "مكافأة لمرة واحدة",
    "deduction": "خصم لمرة واحدة",
}
STAGE_LABELS = {"manager": "المدير المباشر", "hr": "الموارد البشرية", "payroll": "الرواتب"}
STATUS_LABELS = {"pending": "قيد المعالجة", "approved": "معتمد", "rejected": "مرفوض", "cancelled": "ملغى"}
AFFECTS_PAYROLL = {"loan", "change", "sick", "permission"}


class RequestError(ValueError):
    pass


def label(req):
    base = REQUEST_TYPES[req.type]["label"]
    if req.type == "change":
        return CHANGE_KINDS.get((req.data or {}).get("kind"), base)
    return base


def _d(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise RequestError("التاريخ غير صحيح.")


def _days_word(n):
    return "أيام" if 3 <= n <= 10 else "يوم"


def summary(req):
    d = req.data or {}
    if req.type in ("leave", "sick"):
        return f"{d.get('days')} {_days_word(int(d.get('days', 0)))} من {d.get('from')}"
    if req.type == "permission":
        return f"{d.get('hours')} ساعة يوم {d.get('date')} من {d.get('start')}"
    if req.type == "loan":
        return f"{float(d.get('amount', 0)):,.0f} ريال على {d.get('months')} أقساط"
    if req.type == "salary_cert":
        return f"موجه إلى {d.get('to') or 'من يهمه الأمر'}"
    if req.type == "exp_cert":
        return f"عن الفترة من {req.employee.hire_date} حتى تاريخه"
    if req.type == "resign":
        return f"آخر يوم عمل {d.get('last_day')}"
    if req.type == "change":
        parts = []
        for k, lbl in (("title", "المسمى"), ("department", "القسم"), ("basic", "الأساسي"), ("housing", "السكن"), ("transport", "النقل")):
            if d.get(k) not in (None, ""):
                parts.append(f"{lbl}: {d[k]:,}" if isinstance(d[k], (int, float)) else f"{lbl}: {d[k]}")
        if d.get("amount"):
            parts.append(f"المبلغ: {float(d['amount']):,.0f}")
        return " · ".join(parts) + f" · يسري من {d.get('effective')}"
    return "—"


# ---------------- التحقق من المدخلات ----------------
def clean_data(rtype, form):
    """كل حقل في نماذج الطلبات يمر من هنا: نوعه، حدوده، وتاريخه داخل نافذة معقولة. أي قيمة غريبة ترجع رسالة وما توصل للقاعدة."""
    if rtype not in REQUEST_TYPES:
        raise RequestError("نوع طلب غير معروف.")
    today = policy.today()
    try:
        reason = v.clean_text(form.get("reason"), 300)
        if rtype in ("leave", "sick"):
            days = v.integer(form.get("days"), "عدد الأيام", 1, 60 if rtype == "leave" else 30)
            start = v.day(form.get("from"), "تاريخ البداية", today - timedelta(days=30), today + timedelta(days=365))
            if rtype == "sick" and not reason:
                raise RequestError("أدخل رقم التقرير الطبي.")
            return {"days": days, "from": start.isoformat(), "reason": reason}
        if rtype == "permission":
            d = v.day(form.get("date"), "التاريخ", today - timedelta(days=7), today + timedelta(days=90))
            start = v.clock(form.get("start"), "وقت الخروج")
            hours = v.number(form.get("hours"), "عدد الساعات", 0.5, 4, step=0.5)
            return {"date": d.isoformat(), "start": start, "hours": hours, "reason": reason}
        if rtype == "loan":
            amount = round(v.number(form.get("amount"), "المبلغ", 1, v.MAX_MONEY), 2)
            months = v.integer(form.get("months"), "عدد الأقساط", 1, policy.LOAN_MAX_MONTHS)
            return {"amount": amount, "months": months, "reason": reason}
        if rtype == "salary_cert":
            return {"to": v.clean_text(form.get("to"), 120) or "من يهمه الأمر"}
        if rtype == "exp_cert":
            return {}
        if rtype == "resign":
            last = v.day(form.get("last_day"), "آخر يوم عمل", today, today + timedelta(days=180))
            return {"last_day": last.isoformat(), "reason": reason}
        if rtype == "change":
            kind = v.choice(form.get("kind"), CHANGE_KINDS, "نوع التعديل")
            if not reason:
                raise RequestError("اكتب سبب التعديل.")
            # بأثر رجعي حتى 90 يوم فقط، عشان ما ينحسب فرق ضخم عن سنوات بالغلط أو بالتلاعب
            eff = v.day(form.get("effective"), "تاريخ السريان", today - timedelta(days=90), today + timedelta(days=365))
            out = {"kind": kind, "effective": eff.isoformat(), "reason": reason}
            if kind in ("salary", "promotion"):
                for k, lbl in (("basic", "الأساسي"), ("housing", "السكن"), ("transport", "النقل")):
                    out[k] = v.integer(form.get(k), lbl, 1 if k == "basic" else 0, v.MAX_MONEY, allow_empty=True)
                if kind == "promotion":
                    out["title"] = v.clean_text(form.get("title"), 120) or None
                    if not out["title"]:
                        raise RequestError("أدخل المسمى الجديد.")
                elif not any(out.get(k) is not None for k in ("basic", "housing", "transport")):
                    raise RequestError("أدخل قيمة جديدة واحدة على الأقل.")
            if kind == "transfer":
                out["department"] = v.choice(form.get("department"), policy.DEPARTMENTS, "القسم الجديد")
                mid = v.integer(form.get("manager_id"), "المدير الجديد", 1, 10 ** 9, allow_empty=True)
                if mid and not db.session.get(Employee, mid):
                    raise RequestError("المدير الجديد غير موجود.")
                out["manager_id"] = mid
            if kind in ("bonus", "deduction"):
                out["amount"] = v.integer(form.get("amount"), "المبلغ", 1, v.MAX_MONEY)
            return out
    except ValueError as ex:
        raise RequestError(str(ex))
    return {}


def _event(req, actor, action, note="", hours=None):
    db.session.add(RequestEvent(request=req, actor=actor, action=action, note=(note or "")[:500], hours=hours))


def _month_permission_hours(emp, day):
    first = day.replace(day=1)
    total = 0.0
    for r in Request.query.filter(Request.employee_id == emp.id, Request.type == "permission",
                                  Request.status.in_(["approved", "pending"])).all():
        d = _d(r.data["date"])
        if d.year == first.year and d.month == first.month:
            total += float(r.data["hours"])
    return total


def _check(emp, req):
    d, t = req.data, req.type
    if t == "leave":
        if _d(d["from"]) < policy.today() - timedelta(days=30):
            return "الإجازة بأثر رجعي تُرفع خلال 30 يومًا من تاريخها."
        if d["days"] > emp.leave_left:
            return f"عدد الأيام المطلوبة ({d['days']}) أكبر من رصيدك المتبقي ({emp.leave_left} يوم)."
    if t == "permission":
        day = _d(d["date"])
        if day < policy.today() - timedelta(days=7):
            return "الاستئذان يُرفع خلال 7 أيام من تاريخه."
        used = _month_permission_hours(emp, day) - d["hours"]
        if used + d["hours"] > policy.PERMISSION_MONTHLY_MAX:
            return f"تجاوزت حد الاستئذان الشهري ({policy.PERMISSION_MONTHLY_MAX} ساعات). المستخدم {used:g} ساعة."
    if t == "loan":
        limit = emp.basic * policy.LOAN_MAX_BASIC_MULTIPLE
        if d["amount"] > limit:
            return f"المبلغ يتجاوز الحد المسموح ({policy.LOAN_MAX_BASIC_MULTIPLE} رواتب أساسية = {limit:,.0f} ريال)."
        if any(l.is_active for l in emp.loans):
            return "لديك سلفة قائمة لم تُسدد بعد."
    if t == "resign" and _d(d["last_day"]) < policy.today():
        return "آخر يوم عمل في الماضي."
    if t not in ("change", "permission"):
        dup = Request.query.filter(Request.employee_id == emp.id, Request.type == t, Request.status == "pending",
                                   Request.id != req.id).first()
        if dup:
            return "لديك طلب مماثل قيد المعالجة."
    if t in ("leave", "sick"):
        clash = _overlap(emp, req)
        if clash:
            return clash
    if t == "change":
        return _check_change(emp, req)
    return None


def _span(r):
    start = _d(r.data["from"])
    return start, start + timedelta(days=int(r.data["days"]) - 1)


def _overlap(emp, req):
    """ما يصير موظف عنده إجازتين على نفس الأيام (كان ينخصم الرصيد مرتين)."""
    s, e = _span(req)
    for r in Request.query.filter(Request.employee_id == emp.id, Request.type.in_(["leave", "sick"]),
                                  Request.status.in_(["approved", "pending"]), Request.id != req.id).all():
        rs, re_ = _span(r)
        if rs <= e and s <= re_:
            return f"الفترة تتداخل مع {label(r)} (طلب رقم {r.id} من {rs} إلى {re_})."
    return None


def _check_change(emp, req):
    d = req.data
    if d["kind"] == "transfer" and d.get("manager_id"):
        m = db.session.get(Employee, int(d["manager_id"]))
        if not m or m.status != "active":
            return "المدير الجديد ليس على رأس العمل."
        if m.id == emp.id or m.id in subordinates(emp):
            return "ما يصير الموظف مدير نفسه أو مدير لمديره (حلقة في الهيكل)."
    if d["kind"] == "deduction" and float(d["amount"]) > emp.gross / 2:
        return "الخصم أكبر من نصف الأجر الشهري، والمادة 92 من نظام العمل تمنع ذلك. قسّمه على أكثر من شهر."
    return None


def recheck(req):
    """فحص ثاني لحظة الاعتماد: الأشياء تتغير بين التقديم والقرار (رصيد انصرف، مدير ترك العمل...)."""
    emp, d = req.employee, req.data
    if emp.status != "active":
        return "الموظف ليس على رأس العمل."
    if req.type == "leave" and req.stage_index >= len(req.stages) - 1 and d["days"] > emp.leave_left:
        return f"الرصيد الحالي ({emp.leave_left} يوم) ما يكفي الإجازة ({d['days']} يوم)."
    if req.type in ("leave", "sick"):
        return _overlap(emp, req)
    if req.type == "loan" and req.stage_index >= len(req.stages) - 1 and any(l.is_active for l in emp.loans):
        return "صار عند الموظف سلفة قائمة."
    if req.type == "change":
        return _check_change(emp, req)
    return None


def _auto_reason(rtype, data, rule):
    """الاعتماد التلقائي حسب القاعدة اللي حددها مدير الموارد البشرية من صفحة «قواعد الطلبات».
    الطلب اللي يقدمه شخص نيابة عن غيره، أو بأثر رجعي، ما يُعتمد تلقائيًا."""
    if not rule["auto"]:
        return None
    if rtype == "leave" and data["days"] <= rule["limit"] and _d(data["from"]) >= policy.today():
        return f"اعتماد تلقائي: {data['days']} {_days_word(data['days'])} (الحد {rule['limit']:g}) والرصيد يكفي"
    if rtype == "sick" and data["days"] <= rule["limit"] and _d(data["from"]) >= policy.today() - timedelta(days=3):
        return f"اعتماد تلقائي: إجازة مرضية {data['days']} {_days_word(data['days'])} ضمن الحد ({rule['limit']:g})"
    if rtype == "permission" and data["hours"] <= rule["limit"]:
        return f"اعتماد تلقائي: استئذان {data['hours']:g} ساعة ضمن الحد ({rule['limit']:g}) والحد الشهري"
    if rtype in ("salary_cert", "exp_cert"):
        return "إصدار الخطاب وإرساله بالإيميل"
    return None


def submit(emp, rtype, data, actor_name, requested_by=None):
    if emp.status != "active":
        raise RequestError("الموظف ليس على رأس العمل.")
    # قفل سجل الموظف لين تنتهي العملية: طلبان متزامنان ما يعدّون من نفس الرصيد (PostgreSQL)
    db.session.query(Employee).filter_by(id=emp.id).with_for_update().first()
    from .payroll import month_for
    rule = rules.get(rtype)
    req = Request(employee=emp, type=rtype, data=data, stages=list(rule["stages"]), status="pending",
                  requested_by_id=requested_by.id if requested_by else None, approved_by_ids=[])
    if rtype in AFFECTS_PAYROLL:
        ref = data.get("effective") or data.get("date") or data.get("from")
        req.payroll_month = month_for(_d(ref) if ref else policy.today())
    db.session.add(req)
    db.session.flush()
    _event(req, actor_name, "تقديم الطلب", data.get("reason", ""))
    lbl = label(req)

    problem = _check(emp, req)
    if problem:
        req.status, req.auto, req.closed_at = "rejected", True, utcnow()
        _event(req, "الأتمتة", "رفض تلقائي", problem)
        mailer.queue(emp.email, emp.name, f"تعذّر قبول طلبك: {lbl}",
                     f"مرحبًا {emp.first_name}،\n\nلم يُقبل طلبك ({lbl}) للسبب التالي:\n{problem}\n\n"
                     "تقدر تعدّل الطلب وتعيد تقديمه من بوابة مدار.\n\nمع التحية،\nالموارد البشرية",
                     employee_id=emp.id, tag="فحص آلي")
        audit.log("رفض تلقائي", f"{lbl} لـ{emp.name}: {problem}", automated=True)
        return req, "رُفض تلقائيًا: " + problem

    auto_reason = _auto_reason(rtype, data, rule)
    if auto_reason:
        req.stages, req.auto = [], True
        _event(req, "الأتمتة", auto_reason)
        finalize(req)
        audit.log("اعتماد تلقائي", f"{lbl} لـ{emp.name}", automated=True)
        msg = {"leave": "تم اعتماد إجازتك فورًا وخصمها من رصيدك.", "permission": "تم اعتماد الاستئذان فورًا.",
               "sick": "تم اعتماد الإجازة المرضية فورًا. احتفظ بالتقرير الطبي.",
               "salary_cert": "صدر خطاب التعريف بالراتب وأُرسل إلى بريدك وبوابتك.",
               "exp_cert": "صدرت شهادة الخبرة وأُرسلت إلى بريدك وبوابتك."}.get(rtype, "تم اعتماد الطلب تلقائيًا.")
        return req, msg

    notify_stage(req)
    audit.log("تقديم طلب", f"{lbl} لـ{emp.name} أُحيل إلى {STAGE_LABELS[req.current_stage]}", automated=True)
    note = ""
    if req.payroll_month and req.payroll_month != month_for(policy.today(), ignore_cutoff=True):
        note = f" أثره المالي يدخل مسير {req.payroll_month}."
    return req, f"اجتاز الطلب الفحص الآلي وأُحيل إلى {STAGE_LABELS[req.current_stage]} للموافقة.{note}"


def _approval_email(req, user, kind="new"):
    cfg = current_app.config
    emp, lbl = req.employee, label(req)
    raw = tokens.issue("approve", user.id, cfg["APPROVAL_TOKEN_HOURS"], request_id=req.id, stage_index=req.stage_index)
    url = f"{cfg['BASE_URL']}/a/{raw}"
    first = user.display_name.split()[0]
    head = {
        "new": f"طلب بانتظار موافقتك: {lbl} · {emp.name}",
        "remind": f"تذكير: طلب ينتظرك منذ {int(req.hours_waiting)} ساعة · {emp.name}",
        "escalate": f"تصعيد: طلب متأخر عند {STAGE_LABELS[req.current_stage]} · {emp.name}",
    }[kind]
    intro = {
        "new": f"قدّم {emp.name} ({emp.title}) طلب {lbl}: {summary(req)}.",
        "remind": f"طلب {lbl} من {emp.name} ({summary(req)}) ما زال بانتظار قرارك منذ {int(req.hours_waiting)} ساعة.",
        "escalate": f"طلب {lbl} من {emp.name} ({summary(req)}) متأخر {int(req.hours_waiting)} ساعة عند {STAGE_LABELS[req.current_stage]}. "
                    "صُعّد لك حسب سياسة الشركة، وتقدر تتخذ القرار مباشرة.",
    }[kind]
    if req.payroll_month:
        intro += f"\nيؤثر على مسير {req.payroll_month} (الإغلاق يوم {settings.get_int('cutoff_day')})."
    mailer.queue(user.email, user.display_name, head,
                 f"مرحبًا {first}،\n\n{intro}\n" + (f"السبب: {req.data.get('reason')}\n" if req.data.get("reason") else "")
                 + f"\nالرابط صالح {cfg['APPROVAL_TOKEN_HOURS']} ساعة ويستخدم مرة واحدة.\n\nمدار للموارد البشرية",
                 employee_id=emp.id, actions=[["مراجعة الطلب واتخاذ القرار", url]], tag={"new": "موافقة", "remind": "تذكير", "escalate": "تصعيد"}[kind])


def eligible_approvers(req):
    """من يوصله إيميل الموافقة: أصحاب الصلاحية اللي يحق لهم فعلًا (بدون صاحب الطلب ومقدمه ومن اعتمد مرحلة سابقة).
    إذا ما بقى أحد، يروح لمدير الموارد البشرية."""
    out = [u for u in stage_approvers(req, req.current_stage) if can_act(u, req)]
    if not out:
        out = [u for u in User.query.filter(User.base_role == "admin", User.active.is_(True)).all() if can_act(u, req)]
    return out


def notify_stage(req):
    stage = req.current_stage
    emp, lbl = req.employee, label(req)
    req.stage_since, req.reminders_sent, req.escalated_to_id = utcnow(), 0, None
    approvers = eligible_approvers(req)
    for u in approvers:
        _approval_email(req, u, "new")
    from ..permissions import HR_STAGE_PERM, STAGE_PERM
    # المهمة تظهر في صندوق كل من يقدر يعتمد: لو شخص واحد تنسند له، وإلا تظهر لكل أصحاب الصلاحية
    role = "manager" if stage == "manager" else (STAGE_PERM.get(stage) or HR_STAGE_PERM.get(req.type, "admin"))
    assignee = approvers[0].id if len(approvers) == 1 else None
    if not approvers or all(u.base_role == "admin" for u in approvers):
        role = "admin"
    tasks.ensure(f"req:{req.id}:{req.stage_index}", role, f"{lbl}: {emp.name}",
                 f"{summary(req)} · بانتظار {STAGE_LABELS[stage]}", employee_id=emp.id,
                 due=policy.today() + timedelta(days=2), link=f"/requests/{req.id}", assignee_id=assignee)


def escalation_target(req):
    """لمن يُصعّد الطلب: مدير المدير في مرحلة المدير المباشر، وإلا مدير الموارد البشرية."""
    if req.current_stage == "manager":
        m = req.employee.manager
        mm = m.manager if m else None
        if mm and mm.status == "active" and mm.user and mm.user.active and mm.id != req.employee_id:
            return mm.user
    admins = [u for u in User.query.filter(User.base_role == "admin", User.active.is_(True)).all()
              if u.employee_id != req.employee_id]
    return admins[0] if admins else None


def remind_and_escalate():
    """يُشغّل كل ساعة. يذكّر الموافق المتأخر ثم يصعّد. يرجع قائمة بما نُفّذ."""
    remind_h, esc_h = settings.get_int("remind_hours"), settings.get_int("escalate_hours")
    done = []
    for req in Request.query.filter_by(status="pending").all():
        if not req.current_stage:
            continue
        waited = req.hours_waiting
        stage = STAGE_LABELS[req.current_stage]
        if waited >= esc_h and not req.escalated_to_id:
            target = escalation_target(req)
            if target:
                req.escalated_to_id = target.id
                _event(req, "الأتمتة", f"تصعيد إلى {target.display_name}", f"تأخر {int(waited)} ساعة عند {stage}")
                _approval_email(req, target, "escalate")
                tasks.ensure(f"esc:{req.id}:{req.stage_index}", "assigned", f"تصعيد: {label(req)} · {req.employee.name}",
                             f"متأخر {int(waited)} ساعة عند {stage}", employee_id=req.employee_id,
                             due=policy.today(), link=f"/requests/{req.id}", assignee_id=target.id)
                audit.log("تصعيد طلب", f"{label(req)} لـ{req.employee.name} إلى {target.display_name}", automated=True)
                done.append(f"تصعيد طلب #{req.id} إلى {target.display_name}")
        elif waited >= remind_h and req.reminders_sent == 0:
            for u in eligible_approvers(req):
                _approval_email(req, u, "remind")
            req.reminders_sent = 1
            _event(req, "الأتمتة", "تذكير الموافق", f"بعد {int(waited)} ساعة")
            audit.log("تذكير موافق", f"طلب #{req.id} عند {stage}", automated=True)
            done.append(f"تذكير بطلب #{req.id}")
    return done


def approve(req, user, note="", via="web"):
    if not can_act(user, req):
        raise PermissionError("not allowed")
    problem = recheck(req)
    if problem:
        raise RequestError(problem)
    stage = req.current_stage
    regular = any(u.id == user.id for u in stage_approvers(req, stage) if u.id != req.escalated_to_id)
    if req.escalated_to_id == user.id:
        action = "موافقة بعد التصعيد · " + STAGE_LABELS[stage]
    elif user.base_role == "admin" and not regular:
        action = "موافقة نيابة عن " + STAGE_LABELS[stage]
    else:
        action = "موافقة " + STAGE_LABELS[stage]
    _event(req, user.display_name, action + (" (عبر الإيميل)" if via == "email" else ""), note, hours=round(req.hours_waiting, 1))
    req.approved_by_ids = list(req.approved_by_ids or []) + [user.id]
    tokens.revoke_request(req.id, req.stage_index)
    tasks.close(f"req:{req.id}:{req.stage_index}")
    tasks.close(f"esc:{req.id}:{req.stage_index}")
    req.stage_index += 1
    if req.stage_index >= len(req.stages):
        finalize(req)
    else:
        notify_stage(req)
    audit.log("موافقة على طلب", f"{label(req)} لـ{req.employee.name} ({action})")


def reject(req, user, note="", via="web"):
    if not can_act(user, req):
        raise PermissionError("not allowed")
    emp, lbl = req.employee, label(req)
    _event(req, user.display_name, "رفض" + (" (عبر الإيميل)" if via == "email" else ""), note, hours=round(req.hours_waiting, 1))
    req.status, req.closed_at = "rejected", utcnow()
    tokens.revoke_request(req.id)
    tasks.close(f"req:{req.id}:")
    tasks.close(f"esc:{req.id}:")
    notify_to = emp if req.type != "change" else None
    if notify_to:
        mailer.queue(emp.email, emp.name, f"بخصوص طلبك: {lbl}",
                     f"مرحبًا {emp.first_name}،\n\nنعتذر، لم تتم الموافقة على طلبك ({lbl} · {summary(req)})."
                     + (f"\nالسبب: {note}" if note else "") + "\n\nللاستفسار تواصل مع الموارد البشرية.\n\nمع التحية،\nالموارد البشرية",
                     employee_id=emp.id, tag="قرار طلب")
    elif req.requested_by and req.requested_by.email:
        mailer.queue(req.requested_by.email, req.requested_by.display_name, f"رُفض طلب {lbl} لـ{emp.name}",
                     f"لم يُعتمد طلب {lbl} ({summary(req)})." + (f"\nالسبب: {note}" if note else ""), employee_id=emp.id, tag="قرار طلب")
    audit.log("رفض طلب", f"{lbl} لـ{emp.name}")


def cancel_pending_for(emp, actor="النظام"):
    for req in Request.query.filter_by(employee_id=emp.id, status="pending"):
        req.status, req.closed_at = "cancelled", utcnow()
        _event(req, actor, "إلغاء تلقائي بسبب إنهاء الخدمة")
        tokens.revoke_request(req.id)
        tasks.close(f"req:{req.id}:")


def _excuse_attendance(emp, start, days, note):
    """إجازة أو استئذان معتمد يغطي أيام الغياب أو التأخير في سجل الحضور، فلا يُخصم ولا يُطالَب الموظف."""
    for i in range(days):
        day = start + timedelta(days=i)
        row = AttendanceDay.query.filter_by(employee_id=emp.id, day=day).first()
        if row and row.status in ("absent", "late"):
            row.status, row.note = "excused", note
    tasks.close(f"absence:{emp.id}:{start.isoformat()}")


def finalize(req):
    emp, d = req.employee, req.data
    req.status, req.closed_at = "approved", utcnow()
    base = current_app.config["BASE_URL"]
    if req.type == "leave":
        emp.leave_used += d["days"]
        _excuse_attendance(emp, _d(d["from"]), d["days"], "إجازة سنوية معتمدة")
        mailer.queue(emp.email, emp.name, "اعتماد إجازتك",
                     f"مرحبًا {emp.first_name}،\n\nتم اعتماد إجازتك السنوية لمدة {d['days']} {_days_word(d['days'])} ابتداءً من {d['from']}.\n"
                     f"رصيدك المتبقي: {emp.leave_left} يوم.\n\nإجازة سعيدة،\nالموارد البشرية", employee_id=emp.id, tag="إجازة")
    elif req.type == "sick":
        from .payroll import next_open_month
        split = policy.sick_pay_split(emp.sick_used, d["days"])
        cut = round(sum(n * (1 - rate) for n, rate in split) * emp.gross / 30, 2)
        if cut:
            month = max(req.payroll_month or next_open_month(), next_open_month())
            parts = "، ".join(f"{n} يوم بـ{int(rate * 100)}%" for n, rate in split if rate < 1)
            db.session.add(PayrollAdjustment(employee_id=emp.id, month=month, amount=-cut, request_id=req.id,
                                             reason=f"إجازة مرضية بعد الثلاثين يومًا الأولى في السنة (المادة 117): {parts}"))
        emp.sick_used += d["days"]
        _excuse_attendance(emp, _d(d["from"]), d["days"], "إجازة مرضية معتمدة")
        mailer.queue(emp.email, emp.name, "اعتماد الإجازة المرضية",
                     f"مرحبًا {emp.first_name}،\n\nتم اعتماد إجازتك المرضية ({d['days']} يوم). نتمنى لك الصحة والعافية.\n\nالموارد البشرية",
                     employee_id=emp.id, tag="إجازة")
    elif req.type == "permission":
        _excuse_attendance(emp, _d(d["date"]), 1, f"استئذان معتمد {d['hours']:g} ساعة")
        mailer.queue(emp.email, emp.name, "اعتماد الاستئذان",
                     f"مرحبًا {emp.first_name}،\n\nتم اعتماد استئذانك يوم {d['date']} ({d['hours']:g} ساعة من {d['start']}).\n\nالموارد البشرية",
                     employee_id=emp.id, tag="استئذان")
    elif req.type in ("salary_cert", "exp_cert"):
        ltype = "salary" if req.type == "salary_cert" else "exp"
        letter = letters.issue(ltype, emp, addressee=d.get("to", ""), request_id=req.id)
        mailer.queue(emp.email, emp.name, f"{letters.LETTER_TYPES[ltype]} {letter.number}",
                     f"مرحبًا {emp.first_name}،\n\nصدر {letters.LETTER_TYPES[ltype]} رقم {letter.number}. تقدر تعرضه وتحفظه PDF من بوابة مدار.\n\n"
                     "مع التحية،\nالموارد البشرية", employee_id=emp.id, actions=[["عرض الخطاب", f"{base}/letters/{letter.id}"]], tag="خطاب")
    elif req.type == "loan":
        db.session.add(Loan(employee=emp, amount=d["amount"], months=d["months"]))
        mailer.queue(emp.email, emp.name, "اعتماد السلفة",
                     f"مرحبًا {emp.first_name}،\n\nتم اعتماد سلفة بمبلغ {d['amount']:,.0f} ريال، وتُخصم تلقائيًا على {d['months']} أقساط "
                     f"قيمة كل قسط {d['amount'] / d['months']:,.0f} ريال ابتداءً من مسير {req.payroll_month}.\n\nالموارد البشرية",
                     employee_id=emp.id, tag="سلفة")
    elif req.type == "resign":
        tasks.ensure(f"resign:{emp.id}", "admin", f"بدء إنهاء خدمة {emp.name}", f"استقالة معتمدة · آخر يوم {d['last_day']}",
                     employee_id=emp.id, due=_d(d["last_day"]), link=f"/employees/{emp.id}#offboard")
        mailer.queue(emp.email, emp.name, "تأكيد اعتماد الاستقالة",
                     f"مرحبًا {emp.first_name}،\n\nتم اعتماد استقالتك، وآخر يوم عمل {d['last_day']}. سنتواصل معك لتسليم العهد والمخالصة.\n\n"
                     "نشكرك على الفترة التي قضيتها معنا.\nالموارد البشرية", employee_id=emp.id, tag="استقالة")
    elif req.type == "change":
        apply_change_request(req)


# ---------------- التعديلات الوظيفية والمالية ----------------
FIELD_LABELS = {"basic": "الأساسي", "housing": "السكن", "transport": "النقل", "title": "المسمى", "department": "القسم", "manager_id": "المدير"}


def apply_change_request(req):
    from .payroll import next_open_month
    emp, d = req.employee, req.data
    effective = _d(d["effective"])
    kind = d["kind"]
    if kind in ("bonus", "deduction"):
        amount = float(d["amount"]) * (1 if kind == "bonus" else -1)
        month = max(req.payroll_month or next_open_month(), next_open_month())
        db.session.add(PayrollAdjustment(employee_id=emp.id, month=month, amount=amount,
                                         reason=f"{CHANGE_KINDS[kind]}: {d.get('reason', '')}"[:300], request_id=req.id))
        if kind == "bonus":
            mailer.queue(emp.email, emp.name, "مكافأة", f"مرحبًا {emp.first_name}،\n\nاعتُمدت لك مكافأة بمبلغ {abs(amount):,.0f} ريال، "
                         f"وتُصرف مع مسير {month}.\n\nالإدارة", employee_id=emp.id, tag="تعديل مالي")
        else:
            mailer.queue(emp.email, emp.name, "إشعار خصم", f"السيد/ة {emp.name}،\n\nنفيدكم باعتماد خصم بمبلغ {abs(amount):,.0f} ريال "
                         f"من مسير {month}.\nالسبب: {d.get('reason', '')}\n\nإدارة الموارد البشرية",
                         kind="draft", sensitive=True, employee_id=emp.id, tag="تعديل مالي")
        return
    changes = {k: d[k] for k in ("basic", "housing", "transport", "title", "department", "manager_id") if d.get(k) not in (None, "")}
    if effective > policy.today():
        db.session.add(ScheduledChange(employee_id=emp.id, request_id=req.id, effective=effective, changes=changes))
        _event(req, "الأتمتة", f"جُدول التطبيق في {effective}")
    else:
        apply_changes(emp, changes, effective, req.id)
    mailer.queue(emp.email, emp.name, f"إشعار {CHANGE_KINDS[kind]}",
                 f"مرحبًا {emp.first_name}،\n\nنفيدك باعتماد {CHANGE_KINDS[kind]} ({summary(req)}).\n\nإدارة الموارد البشرية",
                 employee_id=emp.id, tag="تعديل وظيفي")


def apply_changes(emp, changes, effective, request_id=None):
    """يطبّق التعديل، ويسجّل التاريخ، ويضبط الرواتب:
    - فروقات بأثر رجعي لأي مسير معتمد بعد تاريخ السريان
    - تسوية تناسبية إذا التعديل يسري في نص شهر ما انعتمد مسيره بعد (الأيام قبل السريان بالراتب القديم)
    - إذا تغيّر المدير: الطلبات المعلقة عند المدير القديم تنتقل للجديد فورًا"""
    from .payroll import month_bounds, next_open_month
    old_gross = emp.gross
    old_manager = emp.manager_id
    for k, val in changes.items():
        old = getattr(emp, k)
        if k == "manager_id":
            val = int(val) if val else None
        setattr(emp, k, val)
        db.session.add(SalaryHistory(employee_id=emp.id, effective=effective, field=k,
                                     old=str(old) if old is not None else "", new=str(val) if val is not None else "", request_id=request_id))
    db.session.flush()
    if emp.manager_id != old_manager:
        db.session.expire(emp, ["manager"])
        reroute_pending(emp)
    diff = emp.gross - old_gross
    if diff:
        target = next_open_month()
        approved = {r.month: r for r in PayrollRun.query.all()}
        for month, run in sorted(approved.items()):
            start, end = month_bounds(month)
            if end < effective or not any(p.employee_id == emp.id for p in run.payslips):
                continue
            days = (end - max(start, effective)).days + 1
            amount = round(diff * days / ((end - start).days + 1), 2)
            if amount:
                db.session.add(PayrollAdjustment(employee_id=emp.id, month=target, amount=amount, request_id=request_id,
                                                 reason=f"فروقات بأثر رجعي عن {month} ({days} يوم) لتعديل سارٍ من {effective}"))
        eff_month = effective.strftime("%Y-%m")
        start, end = month_bounds(eff_month)
        if eff_month not in approved and eff_month >= next_open_month() and effective > start:
            before = (effective - start).days
            amount = round(-diff * before / ((end - start).days + 1), 2)
            if amount:
                db.session.add(PayrollAdjustment(employee_id=emp.id, month=eff_month, amount=amount, request_id=request_id,
                                                 reason=f"تسوية تناسبية: {before} يوم من {eff_month} قبل سريان التعديل في {effective}"))
    audit.log("تطبيق تعديل", f"{emp.name}: " + "، ".join(f"{FIELD_LABELS.get(k, k)}={x}" for k, x in changes.items()), automated=True)


def reroute_pending(emp):
    """لما يتغير مدير الموظف (نقل أو إنهاء خدمة المدير)، الطلبات اللي تنتظر «المدير المباشر» تروح للمدير الجديد:
    تنلغى روابط القديم، وتنقفل مهمته، ويوصل الجديد إيميل ومهمة."""
    for req in Request.query.filter_by(employee_id=emp.id, status="pending").all():
        if req.current_stage != "manager":
            continue
        tokens.revoke_request(req.id, req.stage_index)
        from ..models import Task
        Task.query.filter(Task.key.in_([f"req:{req.id}:{req.stage_index}", f"esc:{req.id}:{req.stage_index}"])).delete(
            synchronize_session=False)
        _event(req, "الأتمتة", "تحويل الطلب للمدير الجديد")
        notify_stage(req)


def apply_scheduled():
    done = []
    for sc in ScheduledChange.query.filter(ScheduledChange.applied_at.is_(None), ScheduledChange.effective <= policy.today()).all():
        if sc.employee.status == "active":
            apply_changes(sc.employee, sc.changes, sc.effective, sc.request_id)
            done.append(f"تطبيق تعديل {sc.employee.name} السارِي من {sc.effective}")
        sc.applied_at = utcnow()
    return done
