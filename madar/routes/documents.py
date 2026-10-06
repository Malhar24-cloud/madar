from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..models import Email, Employee, Letter, db, utcnow
from ..permissions import perm_required
from ..services import ai, audit, letters, mailer, tasks
from ..security import limited


def limited_ai():
    return limited("ai", current_user.id, 60, 3600)

bp = Blueprint("documents", __name__)

# من عنده «المراسلات» بدون «الرواتب» يشوف بس الإيميلات اللي متأكدين إنها ما فيها مبالغ.
# القاعدة قائمة سماح (مو قائمة منع): أي نوع إيميل جديد يكون مخفي افتراضيًا لين نتأكد منه.
NON_FINANCIAL_TAGS = {"إجازة", "استئذان", "تعيين", "إقامة", "تجديد عقد", "تجربة", "حضور", "استقالة", "كتابة بالذكاء"}


def _can_see_email(m):
    if current_user.can("admin", "payroll"):
        return True
    return m.tag in NON_FINANCIAL_TAGS


@bp.route("/letters", endpoint="letters")
@login_required
def letters_index():
    q = Letter.query
    if not current_user.can("letters", "payroll"):
        q = q.filter_by(employee_id=current_user.employee_id or -1)
    elif not current_user.can("payroll"):
        q = q.filter(Letter.type != "term")  # خطاب إنهاء الخدمة فيه المستحقات المالية
    emps = Employee.query.filter_by(status="active").order_by(Employee.name).all() if current_user.can("letters") else []
    return render_template("documents/letters.html", rows=q.order_by(Letter.id.desc()).limit(200).all(), emps=emps,
                           types=letters.LETTER_TYPES)



@bp.route("/letters/<int:letter_id>")
@login_required
def letter_view(letter_id):
    l = db.session.get(Letter, letter_id) or abort(404)
    staff_ok = current_user.can("payroll") or (current_user.can("letters") and l.type != "term")
    if not (staff_ok or l.employee_id == current_user.employee_id):
        abort(403)
    return render_template("documents/letter_view.html", l=l, label=letters.LETTER_TYPES[l.type])


@bp.route("/letters/new", methods=["POST"])
@perm_required("letters")
def letter_new():
    ltype = request.form.get("type")
    emp = db.session.get(Employee, request.form.get("employee_id", type=int) or 0)
    if ltype not in ("salary", "exp", "warning") or not emp:
        abort(400)
    if emp.status != "active" and ltype != "exp":
        abort(400)
    from .. import validate as v
    note = v.clean_text(request.form.get("note"), 1000, multiline=True)
    l = letters.issue(ltype, emp, addressee=v.clean_text(request.form.get("to"), 120), note=note,
                      issued_by=current_user.display_name)
    lbl = letters.LETTER_TYPES[ltype]
    sensitive = ltype == "warning"
    mailer.queue(emp.email, emp.name, f"{lbl} {l.number}",
                 f"{'السيد/ة ' + emp.name if sensitive else 'مرحبًا ' + emp.first_name}،\n\n"
                 + (f"نرفق لكم {lbl} رقم {l.number}.\n{note}\n" if sensitive else f"صدر {lbl} رقم {l.number} ويمكنك عرضه من بوابة مدار.\n")
                 + "\nإدارة الموارد البشرية",
                 kind="draft" if sensitive else "auto", sensitive=sensitive, employee_id=emp.id,
                 actions=[["عرض الخطاب", f"{current_app.config['BASE_URL']}/letters/{l.id}"]], tag="خطاب")
    audit.log("إصدار خطاب", f"{lbl} {l.number} لـ{emp.name}")
    db.session.commit()
    flash("صدر الخطاب." + (" الإشعار بانتظار موافقتك في المراسلات لأنه حساس." if sensitive else " وأُرسل للموظف."), "good")
    return redirect(url_for("documents.letter_view", letter_id=l.id))


@bp.route("/outbox")
@perm_required("outbox")
def outbox():
    show = request.args.get("show", "pending")
    q = Email.query
    if show == "pending":
        q = q.filter_by(status="pending_approval")
    elif show == "sent":
        q = q.filter_by(status="sent")
    elif show == "failed":
        q = q.filter_by(status="failed")
    counts = {s: Email.query.filter_by(status=s).count() for s in ("pending_approval", "sent", "failed", "cancelled")}
    rows = [m for m in q.order_by(Email.id.desc()).limit(300).all() if _can_see_email(m)][:200]
    return render_template("documents/outbox.html", rows=rows, show=show,
                           counts=counts, ai_on=ai.enabled())


@bp.route("/outbox/<int:email_id>", methods=["GET", "POST"])
@perm_required("outbox")
def email_view(email_id):
    m = db.session.get(Email, email_id) or abort(404)
    if not _can_see_email(m):
        abort(403)
    if request.method == "POST":
        if not current_user.can("admin"):
            abort(403)
        if m.status != "pending_approval":
            abort(409)
        action = request.form.get("action")
        from .. import validate as v
        m.subject = v.clean_text(request.form.get("subject") or m.subject, 250)
        m.body = v.clean_text(request.form.get("body") or m.body, 10000, multiline=True)
        if action == "rewrite":
            if not ai.enabled() or limited_ai():
                flash("الذكاء الاصطناعي غير مفعّل أو وصلت حد الاستخدام.", "bad")
            else:
                try:
                    m.body = ai.rewrite_email(m.subject, m.body) or m.body
                    flash("تمت إعادة الصياغة. راجعها قبل الإرسال.", "good")
                except Exception as ex:
                    current_app.logger.warning("rewrite failed: %s", ex)
                    flash("تعذّرت إعادة الصياغة، النص كما هو.", "bad")
            db.session.commit()
            return redirect(url_for("documents.email_view", email_id=m.id))
        if action == "send":
            m.approved_by = current_user.display_name
            mailer.deliver(m)
            tasks.close(f"mail:{m.id}")
            audit.log("اعتماد وإرسال إيميل", f"{m.subject} → {m.to_name}")
            flash("أُرسل الإيميل." if m.status == "sent" else "فشل الإرسال: " + (m.error or ""), "good" if m.status == "sent" else "bad")
        elif action == "cancel":
            m.status = "cancelled"
            tasks.close(f"mail:{m.id}")
            audit.log("إلغاء مسودة", m.subject)
            flash("أُلغيت المسودة.", "good")
        db.session.commit()
        return redirect(url_for("documents.outbox"))
    return render_template("documents/email_view.html", m=m, ai_on=ai.enabled())


@bp.route("/outbox/<int:email_id>/retry", methods=["POST"])
@perm_required("admin")
def email_retry(email_id):
    m = db.session.get(Email, email_id) or abort(404)
    if m.status != "failed" or any(u == mailer.SCRUBBED for _l, u in (m.actions or [])):
        abort(409)
    mailer.deliver(m)
    db.session.commit()
    flash("أُرسل الإيميل." if m.status == "sent" else "فشل مرة ثانية: " + (m.error or ""), "good" if m.status == "sent" else "bad")
    return redirect(url_for("documents.outbox", show="failed"))


@bp.route("/outbox/compose", methods=["GET", "POST"])
@perm_required("outbox")
def compose():
    emps = Employee.query.filter_by(status="active").order_by(Employee.name).all()
    if request.method == "POST":
        emp = db.session.get(Employee, request.form.get("employee_id", type=int) or 0) or abort(400)
        if ai.enabled() and limited_ai():
            flash("وصلت حد استخدام الذكاء الاصطناعي لهذي الساعة.", "bad")
            return render_template("documents/compose.html", emps=emps, ai_on=ai.enabled())
        from .. import validate as v
        goal = v.clean_text(request.form.get("goal"), 1000, multiline=True)
        tone = request.form.get("tone") if request.form.get("tone") in ("رسمية ودودة", "رسمية حازمة", "تهنئة وشكر") else "رسمية ودودة"
        if not goal:
            flash("اكتب وش تبي تقول في الإيميل.", "bad")
            return render_template("documents/compose.html", emps=emps, ai_on=ai.enabled())
        subject, body = "رسالة من الموارد البشرية", f"مرحبًا {emp.first_name}،\n\n{goal}\n\nإدارة الموارد البشرية"
        if ai.enabled():
            try:
                subject, body = ai.draft_email(emp, goal, tone)
            except Exception as ex:
                current_app.logger.warning("draft failed: %s", ex)
                flash("تعذّر الوصول للذكاء الاصطناعي، أنشأنا مسودة بسيطة.", "bad")
        m = mailer.queue(emp.email, emp.name, subject, body, kind="draft", employee_id=emp.id, tag="كتابة بالذكاء")
        audit.log("مسودة إيميل", f"إلى {emp.name}")
        db.session.commit()
        return redirect(url_for("documents.email_view", email_id=m.id))
    return render_template("documents/compose.html", emps=emps, ai_on=ai.enabled())
