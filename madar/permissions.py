"""كل قرارات الصلاحيات في مكان واحد. الواجهة والمساعد الذكي يمرّون من هنا، فلا تعتمد الحماية على ما يُعرض في الصفحة.
الصلاحيات حسب المهمة (PERMS في models.py) ومدير الموارد البشرية يوزعها من صفحة الصلاحيات."""
from functools import wraps

from flask import abort
from flask_login import current_user, login_required

from .models import Employee, User

# أي صلاحية تعتمد مرحلة «الموارد البشرية» في كل نوع طلب
HR_STAGE_PERM = {"leave": "leaves", "sick": "leaves", "permission": "leaves", "salary_cert": "letters",
                 "exp_cert": "letters", "resign": "hiring", "change": "changes", "loan": "payroll"}
STAGE_PERM = {"payroll": "payroll"}
ANY_STAFF = ("leaves", "payroll", "changes", "hiring", "letters", "attendance", "employees_view", "outbox", "reports", "admin")


def perm_required(*perms, manager=False, operator=False):
    """يسمح لمن عنده أي صلاحية من المذكورة. manager=True يسمح أيضًا لمدير القسم. operator=True للمسؤول التقني."""
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapper(*a, **kw):
            u = current_user
            ok = (perms and u.can(*perms)) or (manager and u.is_manager) or (operator and u.base_role == "operator")
            if not ok:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def admin_required(fn):
    return perm_required("admin")(fn)


def is_hr(user):
    return user.is_staff


def visible_employees(user):
    q = Employee.query
    if user.can("employees_view", "payroll", "hiring", "attendance"):
        return q
    if user.is_manager and user.employee_id:
        return q.filter((Employee.manager_id == user.employee_id) | (Employee.id == user.employee_id))
    return q.filter(Employee.id == (user.employee_id or -1))


def can_view_employee(user, emp):
    if emp is None:
        return False
    if user.employee_id == emp.id or user.can("employees_view", "payroll", "hiring", "attendance"):
        return True
    return user.is_manager and emp.manager_id == user.employee_id


def can_see_salary(user, emp):
    return user.can("payroll") or user.employee_id == emp.id


def can_view_request(user, req):
    if user.employee_id == req.employee_id or req.escalated_to_id == user.id or req.requested_by_id == user.id:
        return True
    if user.can("admin"):
        return True
    perm = HR_STAGE_PERM.get(req.type)
    if perm and user.can(perm):
        return True
    if "payroll" in req.stages and user.can("payroll"):
        return True
    if user.is_manager and req.employee.manager_id == user.employee_id:
        # مدير القسم يشوف طلبات فريقه، إلا تعديلات الراتب اللي ما رفعها هو (الرواتب سرية حتى عن المدير)
        if req.type == "change" and req.data.get("kind") in ("salary", "promotion", "bonus", "deduction"):
            return req.requested_by_id == user.id
        return True
    return False


def users_with(perm):
    """المستخدمون النشطون اللي عندهم صلاحية معينة صراحة (بدون مدراء الموارد البشرية)."""
    out = []
    for u in User.query.filter(User.active.is_(True), User.base_role != "operator").all():
        if u.base_role != "admin" and perm in u.effective_perms:
            out.append(u)
    return out


def _active_admins():
    return User.query.filter(User.base_role == "admin", User.active.is_(True)).all()


def _excluded(req, u):
    """صاحب الطلب ومقدمه ما يعتمدونه أبدًا."""
    return u.employee_id == req.employee_id or (req.requested_by_id and u.id == req.requested_by_id)


def subordinates(emp):
    """كل من تحت الموظف في الهيكل (مباشرة أو غير مباشرة)."""
    seen, stack = set(), [emp]
    while stack:
        for r in stack.pop().reports:
            if r.id not in seen:
                seen.add(r.id)
                stack.append(r)
    return seen


def _manager_chain_users(emp, limit=5):
    """مدراء الموظف صعودًا (المدير، ثم مديره...). يُستخدمون آخر خيار، مثلًا لما يكون الطلب عن مدير الموارد البشرية نفسه."""
    out, m, seen = [], emp.manager, set()
    while m and m.id not in seen and len(out) < limit:
        seen.add(m.id)
        if m.status == "active" and m.user and m.user.active:
            out.append(m.user)
        m = m.manager
    return out


def stage_approvers(req, stage):
    """من يحق له القرار في هذه المرحلة، بالترتيب:
    1) المدير المباشر (أو بديله المفوَّض) في مرحلة المدير، أو أصحاب الصلاحية المحددة في المراحل الأخرى
    2) إذا ما بقى منهم أحد مؤهل: مدير الموارد البشرية
    3) إذا الشركة صغيرة وما فيه غير شخص اعتمد مرحلة قبل: يُسمح له، حتى ما يعلق الطلب
    4) آخر شيء: مدراء الموظف في الهيكل (مثل الرئيس التنفيذي)، لما ما يبقى أحد من الموارد البشرية مؤهل أبدًا
    المؤهل = مو صاحب الطلب، ولا مقدمه، ولا اعتمد مرحلة سابقة من نفس الطلب."""
    emp = req.employee
    done = set(req.approved_by_ids or [])
    base = []
    if stage == "manager":
        m = emp.manager
        if m and m.status == "active" and m.user and m.user.active:
            base.append(m.user)
            d = m.active_delegate
            # البديل المفوَّض ما يعتمد طلب أحد فوقه في الهيكل (مثلًا موظف مفوَّض من الرئيس التنفيذي ما يعتمد طلب مديره)
            if d and d.user and d.user.active and d.id != emp.id and d.id not in subordinates(emp):
                base.append(d.user)
    else:
        perm = STAGE_PERM.get(stage) or HR_STAGE_PERM.get(req.type, "admin")
        base = users_with(perm)
    admins = _active_admins()
    # الترتيب: أصحاب الصلاحية، ثم مدير الموارد البشرية، ثم نفسهم حتى لو اعتمدوا مرحلة قبل (شركة صغيرة)،
    # وآخر شيء مدراء الموظف في الهيكل، فقط لما ما يبقى أحد من الموارد البشرية مؤهل أبدًا
    # (مثل طلب عن مدير الموارد البشرية نفسه، أو طلب رفعه هو عن الشخص الوحيد اللي عنده الصلاحية).
    chain = _manager_chain_users(emp)
    out = []
    for tier, relaxed in ((base, False), (admins, False), (base, True), (admins, True), (chain, False), (chain, True)):
        out = [u for u in tier if not _excluded(req, u) and (relaxed or u.id not in done)]
        if out:
            break
    if req.escalated_to and req.escalated_to.active and req.escalated_to not in out \
            and not _excluded(req, req.escalated_to) and req.escalated_to.id not in done:
        out.append(req.escalated_to)
    return out


def can_act(user, req):
    """فصل المهام: لا أحد يعتمد طلبًا عن نفسه، ولا طلبًا قدّمه هو، ولا مرحلتين من نفس الطلب (مبدأ الأربع عيون).
    المسؤول التقني لا يعتمد شيئًا. مدير الموارد البشرية يقدر يعتمد نيابة عن أي مرحلة بنفس الشروط."""
    if req.status != "pending" or not req.current_stage or not user.active:
        return False
    if user.base_role == "operator" or _excluded(req, user):
        return False
    approvers = stage_approvers(req, req.current_stage)
    if any(u.id == user.id for u in approvers):
        return True
    if user.base_role == "admin" and user.id not in (req.approved_by_ids or []):
        return True
    return False
