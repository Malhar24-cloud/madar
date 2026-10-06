import io
from datetime import date, timedelta

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import policy
from ..models import (PERMS, ROLE_PRESETS, ROLES, AuditLog, Email, Employee, PayrollRun, Request, SupportGrant, User, db,
                      utcnow)
from ..permissions import perm_required, stage_approvers
from ..security import fresh_required
from ..services import audit, exporter, importer, mailer, rules, settings, tokens
from ..services.workflow import REQUEST_TYPES, STAGE_LABELS, label, summary

bp = Blueprint("admin", __name__)


def _real_admin():
    """العمليات الإدارية الحساسة لمدير الموارد البشرية الحقيقي فقط، مو للمسؤول التقني حتى أثناء إذن الدعم."""
    if current_user.base_role != "admin":
        abort(403)


@bp.route("/settings", methods=["GET", "POST"])
@perm_required("admin")
@fresh_required
def settings_page():
    if request.method == "POST":
        from .. import validate as v
        bounds = {"cutoff_day": (1, 28), "remind_hours": (1, 168), "escalate_hours": (2, 336), "grace_minutes": (0, 120),
                  "absence_reminder_days": (0, 14), "leave_carry_max": (0, 90)}
        try:
            for key, (default, lbl, kind, _help) in settings.DEFAULTS.items():
                raw = request.form.get(key)
                if kind == "bool":
                    settings.set_value(key, "1" if raw else "0")
                elif kind == "int":
                    lo, hi = bounds.get(key, (0, 10000))
                    settings.set_value(key, str(v.integer(raw, lbl, lo, hi)))
                elif kind == "time":
                    settings.set_value(key, v.clock(raw, lbl))
                elif key == "weekend":
                    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
                    if not parts or len(parts) > 3 or any(p not in "0123456" or len(p) != 1 for p in parts):
                        raise ValueError(f"{lbl}: أرقام من 0 إلى 6 مفصولة بفاصلة (يومين أو ثلاثة بالكثير).")
                    settings.set_value(key, ",".join(sorted(set(parts))))
                else:
                    settings.set_value(key, v.clean_text(raw, 100))
            if settings.get_int("escalate_hours") <= settings.get_int("remind_hours"):
                raise ValueError("التصعيد لازم يكون بعد التذكير.")
        except ValueError as ex:
            db.session.rollback()
            flash(str(ex), "bad")
            return redirect(url_for("admin.settings_page"))
        audit.log("تعديل الإعدادات", "، ".join(f"{k}={settings.get(k)}" for k in settings.DEFAULTS))
        db.session.commit()
        flash("حُفظت الإعدادات.", "good")
        return redirect(url_for("admin.settings_page"))
    operators = User.query.filter_by(base_role="operator").all()
    grants = SupportGrant.query.order_by(SupportGrant.id.desc()).limit(10).all()
    return render_template("admin/settings.html", defs=settings.DEFAULTS, get=settings.get, operators=operators,
                           grants=grants, now=utcnow())


@bp.route("/settings/support", methods=["POST"])
@perm_required("admin")
@fresh_required
def support_grant():
    _real_admin()
    if request.form.get("action") == "revoke":
        g = db.session.get(SupportGrant, request.form.get("grant_id", type=int) or 0) or abort(404)
        g.revoked_at = utcnow()
        audit.log("إلغاء إذن الدعم الفني", f"{g.operator.login_name}")
        db.session.commit()
        flash("أُلغي إذن الدعم فورًا.", "good")
        return redirect(url_for("admin.settings_page"))
    op = db.session.get(User, request.form.get("operator_id", type=int) or 0)
    if not op or op.base_role != "operator":
        abort(400)
    hours = max(1, min(request.form.get("hours", type=int) or 2, 24))
    from .. import validate as v
    reason = v.clean_text(request.form.get("reason"), 300)
    if not reason:
        flash("اكتب سبب منح الإذن.", "bad")
        return redirect(url_for("admin.settings_page"))
    db.session.add(SupportGrant(operator_id=op.id, granted_by=current_user.display_name, reason=reason,
                                expires_at=utcnow() + timedelta(hours=hours)))
    audit.log("منح إذن دعم فني", f"{op.login_name} لمدة {hours} ساعة: {reason}")
    db.session.commit()
    flash(f"مُنح الإذن لمدة {hours} ساعة. الدعم للقراءة فقط، وكل ما يفتحه يُسجل.", "good")
    return redirect(url_for("admin.settings_page"))


@bp.route("/export.xlsx")
@perm_required("admin")
@fresh_required
def export_all():
    _real_admin()
    data = exporter.export_xlsx()
    audit.log("تصدير كامل للبيانات", "ملف Excel")
    db.session.commit()
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=madar-export-{date.today()}.xlsx"})


@bp.route("/import", methods=["GET", "POST"])
@perm_required("hiring")
def import_employees():
    if request.method == "GET":
        return render_template("admin/import.html", results=None, columns=importer.COLUMNS)
    if request.form.get("action") == "commit":
        try:
            records = importer.unpack(request.form.get("payload") or "", current_user.id)
            records, skipped = importer.recheck_duplicates(records)
            if skipped:
                flash("تجاهلنا صفوف موجودة مسبقًا: " + "، ".join(str(x) for x in skipped[:10]), "bad")
        except ValueError as ex:
            flash(str(ex), "bad")
            return redirect(url_for("admin.import_employees"))
        if not records:
            flash("ما فيه صفوف صالحة للاستيراد.", "bad")
            return redirect(url_for("admin.import_employees"))
        created, codes = importer.import_records(records, current_user.display_name,
                                                actor_is_admin=current_user.base_role == "admin")
        db.session.commit()
        return render_template("admin/import_done.html", created=created, codes=codes)
    f = request.files.get("file")
    if not f or not f.filename:
        flash("اختر ملف Excel أو CSV.", "bad")
        return redirect(url_for("admin.import_employees"))
    try:
        rows = importer.read_rows(f)
    except ValueError as ex:
        flash(str(ex), "bad")
        return redirect(url_for("admin.import_employees"))
    except Exception:
        flash("تعذّرت قراءة الملف. تأكد إنه من القالب.", "bad")
        return redirect(url_for("admin.import_employees"))
    results = importer.validate(rows)
    return render_template("admin/import.html", results=results, columns=importer.COLUMNS,
                           payload=importer.pack(results, current_user.id), ok=sum(1 for r in results if not r["errors"]))


@bp.route("/import/template.xlsx")
@perm_required("hiring")
def import_template():
    return Response(importer.template_xlsx(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=madar-employees-template.xlsx"})


@bp.route("/employees/<int:emp_id>/activation", methods=["POST"])
@perm_required("hiring")
@fresh_required
def new_activation(emp_id):
    """رمز تفعيل جديد للعامل اللي ما عنده بريد أو نسي كلمة المرور.
    ممنوع لنفسك، وممنوع لأي حساب عنده صلاحيات (إلا من مدير الموارد البشرية)، عشان ما أحد يستولي على حساب أعلى منه."""
    e = db.session.get(Employee, emp_id) or abort(404)
    if not e.user or not e.user.active:
        abort(400)
    if e.email:
        abort(400)  # من عنده بريد يعيّن كلمة المرور من رابط يوصله هو، مو من رمز يطبعه شخص ثاني
    sensitive = (e.user.is_staff or e.user.base_role in ("admin", "operator", "manager") or e.user.is_manager
                 or Employee.query.filter(Employee.delegate_id == e.id).first() is not None)
    if e.user.id == current_user.id or (sensitive and current_user.base_role != "admin"):
        abort(403)
    code = importer.activation_code()
    tokens.issue_code("activate", e.user.id, code, hours=24 * 14)
    e.user.password_hash = None
    e.user.session_version += 1
    audit.log("إصدار رمز تفعيل", f"{e.name} ({e.code})")
    db.session.commit()
    return render_template("admin/import_done.html", created=[], codes=[{"code": e.code, "name": e.name, "activation": code}])


@bp.route("/reports/delays")
@perm_required("reports")
def delays():
    pending = [r for r in Request.query.filter_by(status="pending").all() if r.current_stage]
    by_person = {}
    for r in pending:
        for u in stage_approvers(r, r.current_stage):
            if u.employee_id == r.employee_id:
                continue
            row = by_person.setdefault(u.id, {"user": u, "count": 0, "oldest": 0, "reqs": []})
            row["count"] += 1
            row["oldest"] = max(row["oldest"], r.hours_waiting)
            row["reqs"].append(r)
    from ..models import RequestEvent
    speed = {}
    for ev in RequestEvent.query.filter(RequestEvent.hours.isnot(None)).all():
        s = speed.setdefault(ev.actor, [0, 0.0])
        s[0] += 1
        s[1] += ev.hours
    speed_rows = sorted(((a, n, t / n) for a, (n, t) in speed.items()), key=lambda x: -x[2])
    return render_template("admin/delays.html", rows=sorted(by_person.values(), key=lambda x: -x["oldest"]),
                           speed=speed_rows, label=label, summary=summary, stage_labels=STAGE_LABELS)


@bp.route("/account/delegate", methods=["GET", "POST"])
@login_required
def delegate():
    """المدير يحدد بديلًا يعتمد طلبات فريقه أثناء إجازته، فلا تتعطل الطلبات."""
    me = current_user.employee
    if not me or not me.reports:
        abort(403)
    if request.method == "POST":
        if request.form.get("action") == "clear":
            me.delegate_id, me.delegate_until = None, None
            audit.log("إلغاء التفويض")
        else:
            d = db.session.get(Employee, request.form.get("delegate_id", type=int) or 0)
            try:
                until = date.fromisoformat(request.form.get("until") or "")
            except ValueError:
                until = None
            if not d or d.id == me.id or d.status != "active" or not until or until < policy.today() or until > policy.today() + timedelta(days=90):
                flash("اختر بديلًا وتاريخ انتهاء خلال 90 يومًا.", "bad")
                return redirect(url_for("admin.delegate"))
            me.delegate_id, me.delegate_until = d.id, until
            audit.log("تفويض صلاحية الاعتماد", f"إلى {d.name} حتى {until}")
        db.session.commit()
        flash("حُفظ التفويض.", "good")
        return redirect(url_for("admin.delegate"))
    options = Employee.query.filter(Employee.status == "active", Employee.id != me.id).order_by(Employee.name).all()
    return render_template("admin/delegate.html", me=me, options=options, today=policy.today())


@bp.route("/system")
@perm_required("admin", operator=True)
def system():
    import os
    backups = []
    bdir = current_app.config["BACKUP_DIR"]
    if os.path.isdir(bdir):
        backups = sorted(os.listdir(bdir), reverse=True)[:5]
    health = {
        "db": True,
        "failed_emails": Email.query.filter_by(status="failed").count(),
        "pending_requests": Request.query.filter_by(status="pending").count(),
        "escalated": Request.query.filter(Request.status == "pending", Request.escalated_to_id.isnot(None)).count(),
        "last_daily": AuditLog.query.filter_by(action="فحص يومي").order_by(AuditLog.id.desc()).first(),
        "last_run": PayrollRun.query.order_by(PayrollRun.month.desc()).first(),
        "users": User.query.filter_by(active=True).count(),
        "ai": settings.get_bool("ai_enabled") and bool(current_app.config.get("ANTHROPIC_API_KEY")),
        "mail": current_app.config["MAIL_BACKEND"], "sso": current_app.config.get("OIDC_PROVIDER") or "—",
    }
    return render_template("admin/system.html", h=health, backups=backups, grant=current_user.support_grant)


# ---------------- الصلاحيات ----------------
ASSIGNABLE_ROLES = ["employee", "manager", "hr", "payroll", "recruit", "admin"]
GRANTABLE = [p for p in PERMS if p != "admin"]


@bp.route("/permissions")
@perm_required("admin")
@fresh_required
def permissions():
    _real_admin()
    q = (request.args.get("q") or "").strip()[:60]
    users = User.query.filter(User.active.is_(True), User.base_role != "operator").all()
    if q:
        users = [u for u in users if q.lower() in (u.display_name + " " + (u.email or "") + " " +
                                                   (u.employee.code if u.employee else "")).lower()]
    users.sort(key=lambda u: (u.base_role != "admin", not u.is_staff, not u.is_manager, u.display_name))
    shown = users[:150]
    admins = sum(1 for u in users if u.base_role == "admin")
    return render_template("admin/permissions.html", users=shown, total=len(users), q=q, perms=PERMS, grantable=GRANTABLE,
                           roles=ROLES, assignable=ASSIGNABLE_ROLES, presets=ROLE_PRESETS, admins=admins)


@bp.route("/permissions/<int:user_id>", methods=["POST"])
@perm_required("admin")
@fresh_required
def permissions_save(user_id):
    _real_admin()
    u = db.session.get(User, user_id) or abort(404)
    if u.id == current_user.id:
        flash("ما تقدر تعدّل صلاحياتك بنفسك. يعدّلها مدير موارد بشرية ثاني.", "bad")
        return redirect(url_for("admin.permissions"))
    if u.base_role == "operator" or not u.active:
        abort(400)
    role = request.form.get("role")
    if role not in ASSIGNABLE_ROLES:
        abort(400)
    if u.base_role == "admin" and role != "admin":
        others = User.query.filter(User.base_role == "admin", User.active.is_(True), User.id != u.id).count()
        if not others:
            flash("لازم يبقى مدير موارد بشرية واحد على الأقل.", "bad")
            return redirect(url_for("admin.permissions"))
    before = (u.base_role, sorted(u.effective_perms))
    if request.form.get("preset") == "1" or request.form.get("perms_shown") != "1":
        perms = None
    else:
        perms = sorted(p for p in request.form.getlist("perms") if p in GRANTABLE)
    u.base_role, u.perms = role, (None if role == "admin" else perms)
    after = (u.base_role, sorted(u.effective_perms))
    if before != after:
        u.session_version += 1  # تنتهي جلساته المفتوحة، ويدخل من جديد (مع التحقق بخطوتين إذا صار عنده صلاحيات)
        audit.log("تعديل صلاحيات", f"{u.display_name}: {ROLES.get(before[0])} {before[1]} ← {ROLES.get(after[0])} {after[1]}")
        if u.email:
            mailer.queue(u.email, u.display_name, "تغيّرت صلاحياتك في مدار",
                         f"مرحبًا {u.display_name.split()[0]}،\n\nعدّل {current_user.display_name} صلاحياتك في مدار.\n"
                         f"صفتك الآن: {ROLES.get(after[0])}.\n"
                         + ("الصلاحيات: " + "، ".join(PERMS[p] for p in after[1]) if after[1] else "بدون صلاحيات إدارية.")
                         + "\n\nإذا ما كنت تتوقع هذا التغيير، بلّغ الموارد البشرية فورًا.\n\nمدار",
                         employee_id=u.employee_id, tag="أمان")
    db.session.commit()
    flash(f"حُفظت صلاحيات {u.display_name}.", "good")
    return redirect(url_for("admin.permissions", q=request.args.get("q") or None))


# ---------------- قواعد الطلبات ----------------
@bp.route("/rules", methods=["GET", "POST"])
@perm_required("admin")
@fresh_required
def rules_page():
    _real_admin()
    if request.method == "POST":
        changes = []
        for rtype in rules.DEFS:
            old = rules.get(rtype)
            new = rules.save(rtype, request.form.get(f"{rtype}_auto") == "1", request.form.get(f"{rtype}_limit"),
                             request.form.getlist(f"{rtype}_stages"))
            if new != old:
                changes.append(f"{REQUEST_TYPES[rtype]['label']}: {rules.describe(rtype, new)}")
        if changes:
            audit.log("تعديل قواعد الطلبات", " | ".join(changes))
        db.session.commit()
        flash("حُفظت القواعد. تنطبق على الطلبات الجديدة فقط." if changes else "ما فيه تغيير.", "good")
        return redirect(url_for("admin.rules_page"))
    rows = [{"type": t, "label": REQUEST_TYPES[t]["label"], "rule": rules.get(t), "def": d,
             "text": rules.describe(t)} for t, d in rules.DEFS.items()]
    return render_template("admin/rules.html", rows=rows, stage_labels=STAGE_LABELS, order=rules.STAGE_ORDER,
                           units=rules.UNIT_LABELS)
