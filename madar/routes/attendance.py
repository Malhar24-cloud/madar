import re
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..models import AttendanceDay, Employee, db
from ..permissions import perm_required, visible_employees
from ..services import attendance as svc, audit, tasks
from ..services.payroll import current_month, month_bounds

bp = Blueprint("attendance", __name__, url_prefix="/attendance")


def _month():
    from .. import policy
    from ..services.payroll import add_months
    default = add_months(current_month(), -1) if policy.today().day <= 7 else current_month()
    m = request.values.get("month") or default
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m):
        abort(400)
    return m


@bp.route("/", methods=["GET"])
@perm_required("attendance", manager=True)
def index():
    month = _month()
    ids = [e.id for e in visible_employees(current_user).filter_by(status="active").all()]
    summ = svc.month_summary(month, ids)
    start, end = month_bounds(month)
    emp_filter = request.args.get("emp", type=int)
    absences = AttendanceDay.query.filter(AttendanceDay.day.between(start, end), AttendanceDay.status == "absent",
                                          AttendanceDay.employee_id.in_([emp_filter] if emp_filter in ids else ids)).order_by(AttendanceDay.day).all()
    return render_template("attendance/index.html", month=month, summ=sorted(summ.values(), key=lambda s: (-s["absent"], -s["late_minutes"])),
                           days_in_month=(end - start).days + 1, absences=absences)


@bp.route("/import", methods=["POST"])
@perm_required("attendance")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("اختر ملف البصمة.", "bad")
        return redirect(url_for("attendance.index"))
    try:
        punches, bad = svc.parse_punches(f)
        result = svc.import_punches(punches, skip_employee_id=None if current_user.base_role == "admin" else current_user.employee_id)
    except ValueError as ex:
        db.session.rollback()
        flash(str(ex), "bad")
        return redirect(url_for("attendance.index"))
    audit.log("استيراد البصمة", f"{len(punches)} بصمة · {result['days']} يوم · {result['employees']} موظف")
    db.session.commit()
    msg = f"استُورد {len(punches)} بصمة لـ{result['employees']} موظف، وطوبقت مع الإجازات والاستئذانات."
    if bad:
        msg += f" تجاهلنا {bad} سطر غير مقروء."
    if result.get("skipped"):
        msg += f" تجاهلنا {result['skipped']} بصمة (أقدم من 120 يوم، أو في المستقبل، أو بصماتك أنت)."
    if result["unknown"]:
        msg += " أرقام غير مربوطة بموظف: " + "، ".join(result["unknown"]) + " (أضفها في حقل رقم البصمة)."
    flash(msg, "good" if not result["unknown"] else "bad")
    return redirect(url_for("attendance.index"))


@bp.route("/day/<int:day_id>", methods=["POST"])
@perm_required("attendance", manager=True)
def decide(day_id):
    a = db.session.get(AttendanceDay, day_id) or abort(404)
    own_team = current_user.is_manager and a.employee.manager_id == current_user.employee_id
    if not (current_user.can("attendance") or own_team) or a.employee_id == current_user.employee_id:
        abort(403)
    if a.status != "absent":
        abort(409)
    from .. import validate as v
    note = v.clean_text(request.form.get("note"), 200)
    if request.form.get("decision") not in ("excuse", "deduct"):
        abort(400)
    if request.form.get("decision") == "excuse":
        a.status, a.note = "excused", "مبرر: " + (note or current_user.display_name)
        audit.log("تبرير غياب", f"{a.employee.name} يوم {a.day}")
    else:
        a.status, a.note = "deducted", "أُقر الخصم: " + (note or current_user.display_name)
        audit.log("إقرار خصم غياب", f"{a.employee.name} يوم {a.day}")
    tasks.close(f"absence:{a.employee_id}:{a.day.isoformat()}")
    db.session.commit()
    flash("حُفظ القرار.", "good")
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for("attendance.index"))
