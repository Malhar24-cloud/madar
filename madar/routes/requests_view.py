from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import policy
from ..models import Employee, Request, db
from ..permissions import can_act, can_view_employee, can_view_request, perm_required
from .. import validate as v
from ..security import client_ip, limited
from ..services import tokens, workflow
from ..services.workflow import REQUEST_TYPES, STAGE_LABELS, STATUS_LABELS

bp = Blueprint("requests", __name__)


@bp.route("/requests")
@login_required
def index():
    show = request.args.get("show") or ("action" if current_user.is_staff or current_user.is_manager else "mine")
    rows = Request.query.order_by(Request.created_at.desc()).limit(300).all()
    rows = [r for r in rows if can_view_request(current_user, r)]
    if show == "action":
        rows = [r for r in rows if can_act(current_user, r)]
    elif show == "mine":
        rows = [r for r in rows if r.employee_id == current_user.employee_id]
    elif show == "pending":
        rows = [r for r in rows if r.status == "pending"]
    elif show == "auto":
        rows = [r for r in rows if r.auto]
    return render_template("requests/index.html", rows=rows, show=show, types=REQUEST_TYPES, stage_labels=STAGE_LABELS,
                           status_labels=STATUS_LABELS, summary=workflow.summary, can_act=can_act)


@bp.route("/requests/new/<rtype>", methods=["GET", "POST"])
@login_required
def new(rtype):
    if rtype not in REQUEST_TYPES or not REQUEST_TYPES[rtype]["self"] or not current_user.employee:
        abort(404)
    e = current_user.employee
    if request.method == "POST":
        try:
            data = workflow.clean_data(rtype, request.form)
            req, message = workflow.submit(e, rtype, data, e.name)
            db.session.commit()
            flash(message, "bad" if req.status == "rejected" else "good")
            return redirect(url_for("requests.detail", req_id=req.id))
        except workflow.RequestError as ex:
            db.session.rollback()
            flash(str(ex), "bad")
    try:
        default_from = date.fromisoformat(request.args.get("from", ""))
    except ValueError:
        default_from = policy.today() + timedelta(days=7)
    return render_template("requests/new.html", rtype=rtype, label=REQUEST_TYPES[rtype]["label"], e=e, p=policy,
                           today=policy.today(), default_from=default_from,
                           default_last=policy.today() + timedelta(days=policy.NOTICE_EMPLOYEE),
                           eos=policy.end_of_service(e, "resign"))


@bp.route("/requests/change", methods=["GET", "POST"])
@perm_required("changes", "hiring", "payroll", manager=True)
def change():
    """طلب تعديل وظيفي أو مالي يرفعه المدير أو الموارد البشرية بتاريخ سريان، ويمر على الموارد البشرية ثم الرواتب."""
    from ..permissions import visible_employees
    emps = visible_employees(current_user).filter(Employee.status == "active", Employee.id != (current_user.employee_id or -1)) \
        .order_by(Employee.name).all()
    if request.method == "POST":
        emp = db.session.get(Employee, request.form.get("employee_id", type=int) or 0)
        if not emp or emp not in emps:
            abort(403)
        try:
            data = workflow.clean_data("change", request.form)
            req, message = workflow.submit(emp, "change", data, current_user.display_name, requested_by=current_user)
            db.session.commit()
            flash(message, "bad" if req.status == "rejected" else "good")
            return redirect(url_for("requests.detail", req_id=req.id))
        except workflow.RequestError as ex:
            db.session.rollback()
            flash(str(ex), "bad")
    return render_template("requests/change.html", emps=emps, kinds=workflow.CHANGE_KINDS, depts=policy.DEPARTMENTS,
                           managers=Employee.query.filter_by(status="active").order_by(Employee.name).all(),
                           today=policy.today(), selected=request.args.get("emp", type=int))


@bp.route("/requests/<int:req_id>")
@login_required
def detail(req_id):
    r = db.session.get(Request, req_id) or abort(404)
    if not can_view_request(current_user, r):
        abort(403)
    return render_template("requests/detail.html", r=r, label=workflow.label(r), summary=workflow.summary(r),
                           stage_labels=STAGE_LABELS, status_labels=STATUS_LABELS, can=can_act(current_user, r), p=policy)


@bp.route("/requests/<int:req_id>/decide", methods=["POST"])
@login_required
def decide(req_id):
    r = db.session.get(Request, req_id) or abort(404)
    if not can_act(current_user, r):
        abort(403)
    note = v.clean_text(request.form.get("note"), 300)
    decision = request.form.get("decision")
    if decision not in ("approve", "reject"):
        abort(400)
    if decision == "approve":
        try:
            workflow.approve(r, current_user, note)
        except workflow.RequestError as ex:
            db.session.rollback()
            flash(f"ما يمكن الاعتماد: {ex} تقدر ترفضه مع ذكر السبب.", "bad")
            return redirect(url_for("requests.detail", req_id=r.id))
        msg = "اعتُمد الطلب ونُفذ أثره تلقائيًا." if r.status == "approved" else f"انتقل الطلب إلى {STAGE_LABELS[r.current_stage]}."
    else:
        workflow.reject(r, current_user, note)
        msg = "رُفض الطلب وأُبلغ الموظف."
    db.session.commit()
    flash(msg, "good")
    return redirect(url_for("requests.detail", req_id=r.id))


# ---------- الموافقة من الإيميل ----------
# الرابط يفتح صفحة تأكيد (GET) ولا ينفذ شيئًا، لأن برامج البريد وفاحصات الروابط تفتح الروابط تلقائيًا.
# التنفيذ يتم فقط بضغط زر (POST)، والرمز يُستخدم مرة واحدة وينتهي بعد المدة المحددة.
# الطلبات المالية والاستقالة: الرابط يفتح الطلب بعد تسجيل الدخول، لأن الإيميل ممكن ينحوّل أو ينقرأ من جهاز غير آمن
LOGIN_REQUIRED_TYPES = {"loan", "change", "resign"}


@bp.route("/a/<token>", methods=["GET", "POST"])
def email_action(token):
    if limited("token-ip", client_ip(), 30, 900):
        db.session.commit()
        abort(429)
    t = tokens.lookup(token, "approve")
    r = t.request if t else None
    db.session.commit()
    if not t or not r or r.status != "pending" or r.stage_index != t.stage_index or not can_act(t.user, r):
        return render_template("requests/email_done.html", ok=False,
                               text="الرابط منتهي أو مستخدم، أو اتُّخذ القرار على الطلب مسبقًا."), 410
    if r.type in LOGIN_REQUIRED_TYPES or (current_user.is_authenticated and current_user.id != t.user.id):
        # يفتح الطلب داخل النظام: لازم يكون الداخل هو نفس الموافق
        if current_user.is_authenticated and current_user.id == t.user.id:
            return redirect(url_for("requests.detail", req_id=r.id))
        return redirect(url_for("auth.login", next=url_for("requests.detail", req_id=r.id)))
    if request.method == "POST":
        note = v.clean_text(request.form.get("note"), 300)
        decision = request.form.get("decision")
        if decision == "approve":
            try:
                workflow.approve(r, t.user, note, via="email")
            except workflow.RequestError as ex:
                db.session.rollback()
                return render_template("requests/email_done.html", ok=False, text=f"ما يمكن الاعتماد: {ex}"), 409
            text = "تم تسجيل موافقتك." + (" اعتُمد الطلب ونُفذ أثره." if r.status == "approved" else f" انتقل الطلب إلى {STAGE_LABELS[r.current_stage]}.")
        elif decision == "reject":
            workflow.reject(r, t.user, note, via="email")
            text = "تم تسجيل الرفض وإبلاغ الموظف."
        else:
            abort(400)
        tokens.mark_used(t)
        db.session.commit()
        return render_template("requests/email_done.html", ok=True, text=text)
    return render_template("requests/email_action.html", r=r, approver=t.user, label=workflow.label(r),
                           summary=workflow.summary(r), stage=STAGE_LABELS[r.current_stage], p=policy)
