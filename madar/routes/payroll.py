import re

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..models import PayrollRun, Payslip, db
from ..permissions import perm_required
from ..services import payroll as svc, settings

bp = Blueprint("payroll", __name__, url_prefix="/payroll")


def _month():
    m = request.args.get("month") or request.form.get("month") or svc.next_open_month()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m):
        abort(400)
    return m


@bp.route("/")
@perm_required("payroll")
def index():
    month = _month()
    run = PayrollRun.query.filter_by(month=month).first()
    if run:
        rows = [{"emp": p.employee, "gross": p.gross, "gosi": p.gosi, "loan": p.loan, "net": p.net, "slip": p,
                 "additions": p.additions, "deductions": p.deductions + p.absence, "notes": p.notes or [], "unexplained": 0}
                for p in sorted(run.payslips, key=lambda x: x.employee.code)]
        ready = []
    else:
        rows = svc.lines(month)
        for r in rows:
            r["deductions"] = round(r["deductions"] + r["absence"], 2)
        ready = svc.readiness(month)
    t = svc.totals(rows)
    return render_template("payroll/index.html", month=month, run=run, rows=rows, totals=t, ready=ready,
                           cutoff=svc.cutoff_date(month), runs=PayrollRun.query.order_by(PayrollRun.month.desc()).limit(12).all(),
                           blockers=sum(1 for i in ready if i["level"] == "bad"),
                           unexplained=sum(r.get("unexplained", 0) for r in rows))


@bp.route("/approve", methods=["POST"])
@perm_required("payroll")
def approve():
    month = _month()
    if request.form.get("confirm") != "yes":
        flash("أكّد الاعتماد بوضع علامة في مربع التأكيد.", "bad")
        return redirect(url_for("payroll.index", month=month))
    try:
        run = svc.approve(month, current_user, deduct_absence=request.form.get("deduct_absence") == "yes")
    except ValueError as ex:
        flash(str(ex), "bad")
        return redirect(url_for("payroll.index", month=month))
    db.session.commit()
    flash(f"اعتُمد مسير {month}، وأُرسلت {len(run.payslips)} كشوف رواتب. نزّل ملف حماية الأجور وارفعه في مُدد.", "good")
    return redirect(url_for("payroll.index", month=month))


@bp.route("/<month>/wps.<fmt>")
@perm_required("payroll")
def wps(month, fmt):
    if not re.fullmatch(r"\d{4}-\d{2}", month) or fmt not in ("csv", "xlsx"):
        abort(400)
    run = PayrollRun.query.filter_by(month=month).first() or abort(404)
    if fmt == "csv":
        return Response(svc.wps_csv(run), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=WPS-{month}.csv"})
    return Response(svc.wps_xlsx(run), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=WPS-{month}.xlsx"})


@bp.route("/<month>/uploaded", methods=["POST"])
@perm_required("payroll")
def uploaded(month):
    run = PayrollRun.query.filter_by(month=month).first() or abort(404)
    svc.mark_wps_uploaded(run, current_user)
    db.session.commit()
    flash("سُجّل رفع ملف حماية الأجور.", "good")
    return redirect(url_for("payroll.index", month=month))


@bp.route("/slip/<int:slip_id>")
@login_required
def slip(slip_id):
    p = db.session.get(Payslip, slip_id) or abort(404)
    if not (current_user.can("payroll") or p.employee_id == current_user.employee_id):
        abort(403)
    return render_template("payroll/payslip.html", p=p)
