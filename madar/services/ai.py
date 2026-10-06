"""الذكاء الاصطناعي عبر Claude API.
مبدأ أمني أساسي: الصلاحيات تُطبق هنا في الخادم داخل كل أداة. النموذج لا يرى إلا ما تعيده الأداة لهذا المستخدم،
فلا يمكن خداعه ليكشف بيانات لا يملك المستخدم صلاحية رؤيتها. أرقام الهوية والآيبان لا تُرسل للنموذج أبدًا."""
import json
import re
from datetime import timedelta

from flask import current_app

from .. import policy
from ..models import Employee, Request
from ..permissions import can_act, can_see_salary, can_view_employee, can_view_request, visible_employees
from .workflow import STAGE_LABELS, STATUS_LABELS, label, summary


def enabled():
    """الذكاء يعمل فقط إذا وُجد المفتاح وفعّلته الشركة من الإعدادات بعد موافقتها على معالجة البيانات خارج المملكة."""
    from . import settings
    return bool(current_app.config.get("ANTHROPIC_API_KEY")) and settings.get_bool("ai_enabled")


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=current_app.config["ANTHROPIC_API_KEY"])


# ---------- أدوات المساعد ----------
TOOLS = [
    {"name": "get_employee",
     "description": "ملف موظف: القسم، المسمى، المدير، تاريخ المباشرة، مدة الخدمة، رصيد الإجازات، حالة العقد والتجربة. "
                    "الراتب يظهر فقط إذا سمحت صلاحية المستخدم. اترك code فارغًا لبيانات المستخدم نفسه.",
     "input_schema": {"type": "object", "properties": {"code": {"type": "string", "description": "مثل EMP-1004"}}}},
    {"name": "search_employees",
     "description": "بحث عن موظفين بالاسم أو المسمى أو القسم ضمن صلاحية المستخدم. يرجع حتى 20 نتيجة مع رموزهم.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "department": {"type": "string"}}}},
    {"name": "list_requests",
     "description": "الطلبات المرئية للمستخدم. status: pending أو approved أو rejected أو all. awaiting_me=true للطلبات التي تنتظر قراره.",
     "input_schema": {"type": "object", "properties": {"status": {"type": "string"}, "awaiting_me": {"type": "boolean"}}}},
    {"name": "end_of_service",
     "description": "تقدير مكافأة نهاية الخدمة لموظف حتى اليوم. reason: term للإنهاء أو resign للاستقالة. يتطلب صلاحية رؤية الراتب.",
     "input_schema": {"type": "object", "properties": {"code": {"type": "string"}, "reason": {"type": "string", "enum": ["term", "resign"]}},
                      "required": ["reason"]}},
    {"name": "expiring_contracts",
     "description": "العقود محددة المدة التي تنتهي خلال عدد أيام، ضمن صلاحية المستخدم.",
     "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "company_policy", "description": "سياسات الشركة وقواعد نظام العمل المطبقة في النظام.",
     "input_schema": {"type": "object", "properties": {}}},
]


def _resolve(user, code):
    if not code:
        return user.employee
    return Employee.query.filter_by(code=str(code).strip().upper()).first()


def run_tool(user, name, args):
    args = args or {}
    if name == "get_employee":
        e = _resolve(user, args.get("code"))
        if not e or not can_view_employee(user, e):
            return {"error": "الموظف غير موجود أو خارج صلاحيتك."}
        out = {"code": e.code, "name": e.name, "department": e.department, "title": e.title,
               "manager": e.manager.name if e.manager else None, "status": "على رأس العمل" if e.status == "active" else "منتهية خدمته",
               "hire_date": str(e.hire_date), "tenure_years": round(e.tenure_years, 2),
               "leave_entitlement": e.entitlement, "leave_used": e.leave_used, "leave_left": e.leave_left, "sick_used": e.sick_used,
               "contract": e.contract_label, "contract_end": str(e.end_date) if e.end_date else None,
               "in_probation": e.in_probation, "probation_days_left": e.probation_left if e.in_probation else None}
        if can_see_salary(user, e):
            out.update({"basic": e.basic, "housing": e.housing, "transport": e.transport, "gross": e.gross,
                        "gosi_employee": policy.gosi_employee(e),
                        "active_loan_remaining": round(sum(l.remaining for l in e.loans if l.is_active), 2)})
        else:
            out["salary"] = "غير متاح لصلاحيتك"
        return out
    if name == "search_employees":
        q = visible_employees(user)
        if args.get("department"):
            q = q.filter(Employee.department.contains(args["department"]))
        if args.get("query"):
            term = f"%{args['query']}%"
            q = q.filter((Employee.name.like(term)) | (Employee.title.like(term)) | (Employee.code.like(term)))
        return [{"code": e.code, "name": e.name, "title": e.title, "department": e.department,
                 "status": e.status, "leave_left": e.leave_left} for e in q.limit(20)]
    if name == "list_requests":
        status = args.get("status") or "pending"
        q = Request.query.order_by(Request.created_at.desc())
        if status != "all":
            q = q.filter_by(status=status)
        rows = []
        for r in q.limit(200):
            if not can_view_request(user, r):
                continue
            if args.get("awaiting_me") and not can_act(user, r):
                continue
            rows.append({"id": r.id, "employee": r.employee.name, "type": label(r), "details": summary(r),
                         "status": STATUS_LABELS[r.status], "waiting_for": STAGE_LABELS.get(r.current_stage),
                         "auto": r.auto, "created": r.created_at.strftime("%Y-%m-%d")})
            if len(rows) >= 25:
                break
        return rows
    if name == "end_of_service":
        e = _resolve(user, args.get("code"))
        if not e or not can_view_employee(user, e) or not can_see_salary(user, e):
            return {"error": "غير متاح لصلاحيتك."}
        res = policy.end_of_service(e, args.get("reason", "term"))
        return {"employee": e.name, **res}
    if name == "expiring_contracts":
        try:
            days = max(1, min(int(args.get("days") or 90), 365))
        except (TypeError, ValueError):
            days = 90
        limit = policy.today() + timedelta(days=days)
        q = visible_employees(user).filter(Employee.status == "active", Employee.end_date.isnot(None), Employee.end_date <= limit)
        return [{"code": e.code, "name": e.name, "end_date": str(e.end_date), "days_left": e.days_to_end} for e in q]
    if name == "company_policy":
        from . import rules
        from .workflow import REQUEST_TYPES
        auto = "\n".join(f"{REQUEST_TYPES[t]['label']}: {rules.describe(t)}" for t in rules.DEFS)
        return {"policy": policy.POLICY_SUMMARY, "approval_rules": auto}
    return {"error": "أداة غير معروفة."}


def _system(user):
    return (f"أنت «مساعد مدار» للموارد البشرية في {current_app.config['COMPANY_NAME']}. تاريخ اليوم {policy.today()}.\n"
            f"المستخدم: {user.display_name}، صفته: {user.role_label}"
            + (f"، رمزه {user.employee.code}" if user.employee else "") + ".\n"
            "استخدم الأدوات للحصول على أي رقم أو معلومة عن موظف أو طلب، ولا تخمّن أرقامًا أبدًا. "
            "إذا أرجعت الأداة خطأ صلاحية فاعتذر بلطف ولا تحاول الالتفاف عليه. "
            "أجب بالعربية بإيجاز (2-6 أسطر غالبًا) وبأسلوب مهني ودود. استخدم قوائم قصيرة عند الحاجة. "
            "إذا طُلب إجراء (اعتماد أو إنهاء أو تعديل) فوضّح أنه يتم من الصفحة المختصة في النظام. "
            "للأسئلة القانونية أعطِ القاعدة العامة ونبّه لمراجعة مختص.")


def assistant_reply(user, history):
    """history: قائمة {"role": "user"|"assistant", "text": str} تنتهي بسؤال المستخدم."""
    if not enabled():
        return local_reply(user, history[-1]["text"])
    client = _client()
    messages = [{"role": h["role"], "content": h["text"]} for h in history[-12:]]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    for _ in range(6):
        resp = client.messages.create(model=current_app.config["ANTHROPIC_MODEL"], max_tokens=1024,
                                      system=_system(user), tools=TOOLS, messages=messages)
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip() or "لم أجد إجابة مناسبة."
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                try:
                    out = run_tool(user, b.name, b.input)
                except Exception as ex:  # نعيد الخطأ للنموذج بدل إيقاف المحادثة
                    current_app.logger.warning("tool %s failed: %s", b.name, ex)
                    out = {"error": "تعذّر تنفيذ الأداة."}
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, ensure_ascii=False, default=str)})
        messages.append({"role": "user", "content": results})
    return "السؤال يحتاج خطوات كثيرة. جرّب تسأل بشكل أدق."


def local_reply(user, q):
    """ردود بسيطة بدون ذكاء اصطناعي، تستخدم نفس الأدوات ونفس الصلاحيات."""
    target = None
    for e in visible_employees(user).all():
        if e.first_name in q or e.code in q.upper():
            target = e
            break
    if not target and user.employee and any(w in q for w in ("إجاز", "راتب", "مكافأ", "استق", "رصيد")):
        target = user.employee
    if target:
        info = run_tool(user, "get_employee", {"code": target.code})
        if "error" in info:
            return info["error"]
        if "إجاز" in q or "رصيد" in q:
            return f"رصيد {info['name']}: {info['leave_left']} يوم متبقي من {info['leave_entitlement']}."
        if "مكافأ" in q or "استق" in q or "نهاية" in q:
            r = run_tool(user, "end_of_service", {"code": target.code, "reason": "resign" if "استق" in q else "term"})
            return r.get("error") or f"مكافأة {r['employee']} التقديرية: {r['amount']:,.0f} ريال. {r['why']}"
        if "راتب" in q:
            return f"إجمالي راتب {info['name']}: {info['gross']:,} ريال." if "gross" in info else "الراتب غير متاح لصلاحيتك."
        return f"{info['name']}، {info['title']} في {info['department']}، رصيد الإجازات {info['leave_left']} يوم."
    if "تنتهي" in q or "انتهاء" in q:
        rows = run_tool(user, "expiring_contracts", {"days": 90})
        return "العقود التي تنتهي خلال 90 يومًا:\n" + "\n".join(f"- {r['name']}: بعد {r['days_left']} يوم" for r in rows) if rows else "لا توجد عقود تنتهي قريبًا."
    if "طلب" in q or "موافق" in q:
        rows = run_tool(user, "list_requests", {"status": "pending", "awaiting_me": True})
        return "بانتظار قرارك:\n" + "\n".join(f"- {r['type']} · {r['employee']} ({r['details']})" for r in rows) if rows else "لا توجد طلبات بانتظار قرارك."
    return ("المساعد يعمل بوضع محدود لأن مفتاح Claude API غير مضبوط. اسأل عن موظف بالاسم مع: إجازة، راتب، أو مكافأة، "
            "أو عن العقود المنتهية أو الطلبات المعلّقة.")


# ---------- استخراج العقود ----------
def _json_from(text):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def extract_contract(text):
    prompt = (
        "استخرج بيانات عقد العمل التالي. أعد كائن JSON واحد فقط بهذه الحقول:\n"
        '{"name":string|null,"nationality":string|null,"title":string|null,'
        f'"department":واحد من {json.dumps(policy.DEPARTMENTS, ensure_ascii=False)} أو null,'
        '"basic_salary":number|null,"housing_allowance":number|null,"transport_allowance":number|null,'
        '"start_date":"YYYY-MM-DD"|null,"contract_type":"fixed"|"open"|null,"end_date":"YYYY-MM-DD"|null,'
        '"probation_days":number|null,"annual_leave_days":number|null,"notice_days":number|null,"weekly_hours":number|null,'
        '"special_clauses":[عناوين قصيرة],"risks":[جمل عربية قصيرة لكل بند يخالف نظام العمل السعودي أو ناقص أو غامض]}\n'
        "fixed = محدد المدة، open = غير محدد المدة. استخدم null لأي قيمة غير موجودة.\n"
        f"مرجع القواعد:\n{policy.POLICY_SUMMARY}\n\nنص العقد:\n\"\"\"\n{text[:20000]}\n\"\"\""
    )
    resp = _client().messages.create(model=current_app.config["ANTHROPIC_MODEL"], max_tokens=1500,
                                     messages=[{"role": "user", "content": prompt}])
    return _json_from("".join(b.text for b in resp.content if b.type == "text"))


# ---------- كتابة الإيميلات ----------
def draft_email(emp, goal, tone):
    prompt = (f"اكتب إيميلًا من إدارة الموارد البشرية في {current_app.config['COMPANY_NAME']} إلى الموظف {emp.name} "
              f"({emp.title}، {emp.department}). النبرة: {tone}. المطلوب: {goal}.\n"
              "عربية فصحى مهنية موجزة. ابدأ بالتحية باسمه الأول واختم بـ«إدارة الموارد البشرية». "
              "لا تخترع أرقامًا أو تواريخ أو وعودًا غير مذكورة في المطلوب.\n"
              'أعد JSON فقط: {"subject": "...", "body": "..."}')
    resp = _client().messages.create(model=current_app.config["ANTHROPIC_MODEL_FAST"], max_tokens=900,
                                     messages=[{"role": "user", "content": prompt}])
    data = _json_from("".join(b.text for b in resp.content if b.type == "text"))
    return str(data.get("subject") or "رسالة من الموارد البشرية")[:200], str(data.get("body") or "")


def rewrite_email(subject, body):
    prompt = ("أعد صياغة هذا الإيميل الرسمي من إدارة الموارد البشرية بعربية فصحى مهنية موجزة ومحترمة. "
              "حافظ على كل الوقائع والأرقام والتواريخ والأسماء كما هي، ولا تضف معلومات ولا وعودًا. "
              f"أعد نص الإيميل فقط بدون أي تعليق.\n\nالموضوع: {subject}\n\nالإيميل:\n{body}")
    resp = _client().messages.create(model=current_app.config["ANTHROPIC_MODEL_FAST"], max_tokens=900,
                                     messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if b.type == "text").strip()
