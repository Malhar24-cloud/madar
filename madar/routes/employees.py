from datetime import date, timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import policy
from ..models import Employee, Letter, Payslip, Request, SalaryHistory, db
from ..permissions import can_see_salary, can_view_employee, can_view_request, perm_required, visible_employees
from ..services import ai, audit, lifecycle, textract
from ..services.workflow import REQUEST_TYPES

from .. import validate as v
from ..security import fresh_required, limited
bp = Blueprint("employees", __name__, url_prefix="/employees")


@bp.route("/")
@perm_required("employees_view", "payroll", "hiring", "attendance", manager=True)
def index():
    q = visible_employees(current_user)
    term = (request.args.get("q") or "").strip()[:60]
    dept = (request.args.get("dept") or "")[:60]
    status = request.args.get("status") if request.args.get("status") in ("active", "left", "all") else "active"
    if term:
        like = f"%{term}%"
        q = q.filter((Employee.name.like(like)) | (Employee.title.like(like)) | (Employee.code.like(like)))
    if dept:
        q = q.filter_by(department=dept)
    if status in ("active", "left"):
        q = q.filter_by(status=status)
    return render_template("employees/index.html", emps=q.order_by(Employee.code).all(), term=term, dept=dept,
                           status=status, depts=policy.DEPARTMENTS)


@bp.route("/<int:emp_id>")
@login_required
def detail(emp_id):
    e = db.session.get(Employee, emp_id) or abort(404)
    if not can_view_employee(current_user, e):
        abort(403)
    show_salary = can_see_salary(current_user, e)
    offboard_preview = None
    if current_user.can("admin") and e.status == "active":
        offboard_preview = {r: policy.end_of_service(e, r) for r in ("term", "resign")}
    return render_template("employees/detail.html", e=e, show_salary=show_salary, p=policy,
                           gosi=policy.gosi_employee(e), preview=offboard_preview,
                           leave_cash=round(e.leave_left * e.gross / 30, 2),
                           loans_left=round(sum(l.remaining for l in e.loans if l.is_active), 2),
                           requests=[r for r in Request.query.filter_by(employee_id=e.id).order_by(Request.created_at.desc()).all()
                                     if can_view_request(current_user, r)],
                           letters=Letter.query.filter_by(employee_id=e.id).order_by(Letter.created_at.desc()).all()
                           if show_salary or current_user.can("letters") else [],
                           payslips=Payslip.query.filter_by(employee_id=e.id).order_by(Payslip.id.desc()).limit(6).all() if show_salary else [],
                           types=REQUEST_TYPES, today=policy.today(),
                           history=[h for h in SalaryHistory.query.filter_by(employee_id=e.id).order_by(SalaryHistory.id.desc()).limit(40)
                                    if show_salary or h.field not in ("basic", "housing", "transport")][:20]
                           if show_salary or current_user.can("hiring") else [],
                           gosi_rates=policy.gosi_rates(e))


def _parse_employee_form(form):
    """كل خانة في نموذج الموظف الجديد لها نوع وحدود. الأخطاء ترجع كلها مرة وحدة للمستخدم."""
    errors, data = [], {}
    today = policy.today()

    def take(key, fn, *args, **kw):
        try:
            data[key] = fn(*args, **kw)
        except ValueError as ex:
            errors.append(str(ex))
            data[key] = None

    take("name", v.required_text, form.get("name"), "الاسم", 120)
    take("email", v.email, form.get("email"))
    take("nationality", v.required_text, form.get("nationality"), "الجنسية", 60)
    take("department", v.choice, form.get("department"), policy.DEPARTMENTS, "القسم")
    take("title", v.required_text, form.get("title"), "المسمى الوظيفي", 120)
    take("basic", v.integer, form.get("basic"), "الراتب الأساسي", 1, v.MAX_MONEY)
    take("housing", v.integer, form.get("housing"), "بدل السكن", 0, v.MAX_MONEY, allow_empty=True)
    take("transport", v.integer, form.get("transport"), "بدل النقل", 0, v.MAX_MONEY, allow_empty=True)
    take("hire_date", v.day, form.get("hire_date"), "تاريخ المباشرة", date(1970, 1, 1), today + timedelta(days=365))
    data["contract_type"] = "fixed" if form.get("contract_type") == "fixed" else "open"
    take("end_date", v.day, form.get("end_date"), "تاريخ الانتهاء", date(1970, 1, 1), date(2100, 12, 31), allow_empty=True)
    take("probation_days", v.integer, form.get("probation_days"), "فترة التجربة", 0, policy.PROBATION_MAX, allow_empty=True)
    take("iqama_expiry", v.day, form.get("iqama_expiry"), "انتهاء الإقامة", date(2000, 1, 1), date(2100, 12, 31), allow_empty=True)
    take("national_id", v.national_id, form.get("national_id"))
    data["bank"] = v.clean_text(form.get("bank"), 40) or None
    take("iban", v.iban, form.get("iban"))
    data["clauses"] = [v.clean_text(c, 300) for c in (form.get("clauses") or "").split("\n") if v.clean_text(c, 300)][:10]
    take("phone", v.phone, form.get("phone"))
    take("attendance_id", v.code, form.get("attendance_id"), "رقم البصمة")
    data["gosi_new_system"] = form.get("gosi_new_system") == "1"
    take("manager_id", v.integer, form.get("manager_id"), "المدير", 1, 10 ** 9, allow_empty=True)
    if data.get("manager_id"):
        m = db.session.get(Employee, data["manager_id"])
        if not m or m.status != "active":
            errors.append("المدير المختار غير موجود أو ليس على رأس العمل.")
    if data["contract_type"] == "fixed" and not data.get("end_date"):
        errors.append("العقد محدد المدة يحتاج تاريخ انتهاء.")
    if data.get("end_date") and data.get("hire_date") and data["end_date"] <= data["hire_date"]:
        errors.append("تاريخ انتهاء العقد لازم يكون بعد المباشرة.")
    if data.get("national_id") and any(x.national_id == data["national_id"] for x in Employee.query.all()):
        errors.append("رقم الهوية مسجل لموظف آخر.")
    if data.get("attendance_id") and (Employee.query.filter_by(attendance_id=data["attendance_id"]).first()
                                      or Employee.query.filter_by(code=data["attendance_id"]).first()):
        errors.append("رقم البصمة مستخدم لموظف آخر (كرقم بصمة أو رقم وظيفي).")
    return data, errors


@bp.route("/new", methods=["GET", "POST"])
@perm_required("hiring")
def new():
    managers = Employee.query.filter_by(status="active").order_by(Employee.name).all()
    if request.method == "POST":
        data, errors = _parse_employee_form(request.form)
        if not errors:
            try:
                emp, link = lifecycle.onboard(data, current_user)
                db.session.commit()
                if not emp.email:
                    flash(f"تم تعيين {emp.name} وأُنشئت مهام التجهيز. ما عنده بريد، فاطبع رمز التفعيل وسلّمه له.", "good")
                    return render_template("admin/import_done.html", created=[],
                                           codes=[{"code": emp.code, "name": emp.name, "activation": link}])
                flash(f"تم تعيين {emp.name}. وصلته رسالة الترحيب ورابط تفعيل الحساب، وأُنشئت مهام التجهيز.", "good")
                return redirect(url_for("employees.detail", emp_id=emp.id))
            except ValueError as ex:
                db.session.rollback()
                errors = [str(ex)]
        return render_template("employees/form.html", f=request.form, errors=errors, managers=managers,
                               depts=policy.DEPARTMENTS, risks=[])
    return render_template("employees/form.html", f={}, errors=[], managers=managers, depts=policy.DEPARTMENTS, risks=[])


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else ""
    except (TypeError, ValueError, OverflowError):
        return ""


@bp.route("/<int:emp_id>/offboard", methods=["POST"])
@perm_required("admin")
@fresh_required
def offboard(emp_id):
    e = db.session.get(Employee, emp_id) or abort(404)
    if request.form.get("confirm") != "yes":
        flash("أكّد الإجراء بوضع علامة في مربع التأكيد.", "bad")
        return redirect(url_for("employees.detail", emp_id=e.id) + "#offboard")
    if current_user.employee_id == e.id:
        abort(403)
    try:
        last_day = v.day(request.form.get("last_day"), "آخر يوم عمل", max(e.hire_date, policy.today() - timedelta(days=60)),
                         policy.today() + timedelta(days=60))
    except ValueError as ex:
        flash(str(ex), "bad")
        return redirect(url_for("employees.detail", emp_id=e.id) + "#offboard")
    reason = "resign" if request.form.get("reason") == "resign" else "term"
    try:
        result = lifecycle.offboard(e, reason, last_day, current_user)
    except ValueError as ex:
        flash(str(ex), "bad")
        return redirect(url_for("employees.detail", emp_id=e.id))
    db.session.commit()
    return render_template("employees/offboarded.html", e=e, r=result)


@bp.route("/import", methods=["GET", "POST"])
@perm_required("hiring")
def import_contract():
    if request.method == "GET":
        return render_template("employees/import.html", ai_on=ai.enabled())
    text = v.clean_text(request.form.get("text"), 50000, multiline=True)
    f = request.files.get("file")
    try:
        if f and f.filename:
            text = textract.extract_text(f)
    except ValueError as ex:
        flash(str(ex), "bad")
        return render_template("employees/import.html", ai_on=ai.enabled(), text=text)
    except Exception:
        flash("تعذّرت قراءة الملف. جرّب ملفًا آخر أو الصق النص.", "bad")
        return render_template("employees/import.html", ai_on=ai.enabled(), text=text)
    if len(text) < 30:
        flash("ارفع ملف العقد أو الصق نصه.", "bad")
        return render_template("employees/import.html", ai_on=ai.enabled(), text=text)
    engine = "القواعد المحلية"
    data = None
    if ai.enabled() and not limited("ai", current_user.id, 60, 3600):
        try:
            data = ai.extract_contract(text)
            engine = "Claude"
        except Exception as ex:
            current_app.logger.warning("AI extract failed: %s", ex)
            flash("تعذّر الوصول للذكاء الاصطناعي، تم الاستخراج بالقواعد المحلية.", "bad")
    if not isinstance(data, dict):
        data = textract.local_contract_extract(text)
    risks = [str(r) for r in (data.get("risks") or []) if r] or policy.contract_risks(data)
    audit.log("قراءة عقد", f"استخراج بيانات {data.get('name') or 'عقد'} عبر {engine}")
    db.session.commit()
    prob = data.get("probation_days")
    f = {
        "name": data.get("name") or "", "nationality": data.get("nationality") or "سعودي", "title": data.get("title") or "",
        "department": data.get("department") or "", "basic": _int(data.get("basic_salary")),
        "housing": _int(data.get("housing_allowance")),
        "transport": _int(data.get("transport_allowance")),
        "hire_date": data.get("start_date") or "", "contract_type": data.get("contract_type") or "open",
        "end_date": data.get("end_date") or "",
        "probation_days": min(int(prob), policy.PROBATION_MAX) if isinstance(prob, (int, float)) else policy.PROBATION_DAYS,
        "clauses": "\n".join(str(c) for c in data.get("special_clauses") or []),
    }
    managers = Employee.query.filter_by(status="active").order_by(Employee.name).all()
    return render_template("employees/form.html", f=f, errors=[], managers=managers, depts=policy.DEPARTMENTS,
                           risks=risks, engine=engine, from_contract=True)
