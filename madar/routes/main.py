from datetime import timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import policy
from ..models import AuditLog, Email, Employee, Letter, Payslip, Request, Task, db, utcnow
from ..permissions import ANY_STAFF, can_act, perm_required, visible_employees
from ..services import audit, tasks as task_svc
from ..services.automation import daily_sweep
from ..services.payroll import current_month
from ..services.workflow import REQUEST_TYPES

bp = Blueprint("main", __name__)


@bp.route("/")
@login_required
def dashboard():
    u = current_user
    if not u.is_staff and not u.is_manager:
        e = u.employee
        return render_template("main/dashboard_employee.html", e=e,
                               requests=Request.query.filter_by(employee_id=e.id).order_by(Request.created_at.desc()).limit(8).all(),
                               letters=Letter.query.filter_by(employee_id=e.id).order_by(Letter.created_at.desc()).limit(6).all(),
                               payslip=Payslip.query.filter_by(employee_id=e.id).order_by(Payslip.id.desc()).first(),
                               loan_left=sum(l.remaining for l in e.loans if l.is_active), types=REQUEST_TYPES)
    emps = visible_employees(u).filter(Employee.status == "active").all()
    my_tasks = task_svc.visible_query(u).filter_by(done=False).order_by(Task.due).limit(8).all()
    pending = [r for r in Request.query.filter_by(status="pending").all() if can_act(u, r)]
    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    auto_count = AuditLog.query.filter(AuditLog.automated.is_(True), AuditLog.at >= month_start).count()
    auto_requests = Request.query.filter_by(auto=True).count()
    limit = policy.today() + timedelta(days=policy.CONTRACT_ALERT_DAYS)
    expiring = [e for e in emps if e.end_date and e.end_date <= limit]
    by_dept = {}
    if u.can("payroll"):
        for e in emps:
            by_dept[e.department] = by_dept.get(e.department, 0) + e.gross
    feed = AuditLog.query.order_by(AuditLog.id.desc()).limit(8).all() if u.can("admin") else []
    return render_template("main/dashboard_hr.html", emps=emps, my_tasks=my_tasks, pending=pending, auto_count=auto_count,
                           auto_requests=auto_requests, expiring=expiring, by_dept=sorted(by_dept.items(), key=lambda x: -x[1]),
                           feed=feed, drafts=Email.query.filter_by(status="pending_approval").count(), month=current_month())


@bp.route("/tasks")
@perm_required(*ANY_STAFF, manager=True)
def tasks():
    show = request.args.get("show", "open")
    q = task_svc.visible_query(current_user)
    if show == "open":
        q = q.filter_by(done=False)
    elif show == "done":
        q = q.filter_by(done=True)
    return render_template("main/tasks.html", items=q.order_by(Task.done, Task.due).limit(200).all(), show=show)


@bp.route("/tasks/<int:task_id>/done", methods=["POST"])
@perm_required(*ANY_STAFF, manager=True)
def task_done(task_id):
    t = task_svc.visible_query(current_user).filter(Task.id == task_id).first() or abort(404)
    t.done, t.done_at = True, utcnow()
    audit.log("إنجاز مهمة", t.title)
    db.session.commit()
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for("main.tasks"))


@bp.route("/automation/run", methods=["POST"])
@perm_required("admin")
def run_daily():
    done = daily_sweep()
    db.session.commit()
    flash(f"نُفّذ {len(done)} إجراء آلي." if done else "لا توجد إجراءات جديدة. كل شيء محدّث.", "good")
    return redirect(url_for("main.dashboard"))


@bp.route("/audit", endpoint="audit")
@perm_required("admin")
def audit_log():
    only = request.args.get("only")
    q = AuditLog.query
    if only == "auto":
        q = q.filter_by(automated=True)
    elif only == "human":
        q = q.filter_by(automated=False)
    return render_template("main/audit.html", rows=q.order_by(AuditLog.id.desc()).limit(300).all(), only=only)


@bp.route("/policy", endpoint="policy")
@login_required
def policy_page():
    return render_template("main/policy.html", p=policy)
