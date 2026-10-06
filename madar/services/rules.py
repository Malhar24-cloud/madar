"""قواعد الطلبات: لكل نوع طلب يحدد مدير الموارد البشرية من الواجهة:
- هل يُعتمد تلقائيًا؟ وإلى أي حد (عدد أيام أو ساعات)؟
- من يعتمده إذا ما انطبق عليه الاعتماد التلقائي (المدير المباشر، الموارد البشرية، الرواتب).
الحدود الصارمة (مثل: السلفة والتعديل المالي ما تنقبل تلقائيًا أبدًا) مكتوبة هنا في الكود ولا تتغير من الواجهة."""
import json

from ..models import Setting, db

STAGE_ORDER = ["manager", "hr", "payroll"]

# can_auto: هل يسمح النظام أصلًا بالاعتماد التلقائي لهذا النوع
# unit: وحدة الحد (days/hours) أو None إذا ما له حد
# max_limit: أعلى حد يقدر المدير يختاره
# required: مراحل لازم تبقى (ما يقدر يشيلها من الواجهة)
DEFS = {
    "leave": {"can_auto": True, "unit": "days", "max_limit": 30,
              "default": {"auto": True, "limit": 3, "stages": ["manager", "hr"]}},
    "sick": {"can_auto": True, "unit": "days", "max_limit": 7,
             "default": {"auto": False, "limit": 1, "stages": ["manager", "hr"]}},
    "permission": {"can_auto": True, "unit": "hours", "max_limit": 4,
                   "default": {"auto": True, "limit": 1, "stages": ["manager"]}},
    "salary_cert": {"can_auto": True, "unit": None, "max_limit": None,
                    "default": {"auto": True, "limit": None, "stages": ["hr"]}},
    "exp_cert": {"can_auto": True, "unit": None, "max_limit": None,
                 "default": {"auto": False, "limit": None, "stages": ["hr"]}},
    "loan": {"can_auto": False, "unit": None, "max_limit": None, "required": ["payroll"],
             "default": {"auto": False, "limit": None, "stages": ["manager", "payroll"]}},
    "resign": {"can_auto": False, "unit": None, "max_limit": None, "required": ["hr"],
               "default": {"auto": False, "limit": None, "stages": ["manager", "hr"]}},
    "change": {"can_auto": False, "unit": None, "max_limit": None, "required": ["payroll"],
               "default": {"auto": False, "limit": None, "stages": ["hr", "payroll"]}},
}
UNIT_LABELS = {"days": "يوم", "hours": "ساعة"}


def _key(rtype):
    return f"rule:{rtype}"


def get(rtype):
    d = DEFS[rtype]
    rule = dict(d["default"])
    row = db.session.get(Setting, _key(rtype))
    if row:
        try:
            saved = json.loads(row.value)
            rule.update({k: saved[k] for k in ("auto", "limit", "stages") if k in saved})
        except (ValueError, TypeError):
            pass
    return normalize(rtype, rule)


def normalize(rtype, rule):
    """يطبق الحدود الصارمة مهما كان المحفوظ، فلو انعبث بقاعدة البيانات ما تنفتح ثغرة."""
    d = DEFS[rtype]
    stages = [s for s in STAGE_ORDER if s in (rule.get("stages") or [])]
    if rtype == "change":
        stages = [s for s in stages if s != "manager"]  # التعديل يرفعه المدير نفسه أو الموارد البشرية
    for s in d.get("required", []):
        if s not in stages:
            stages.append(s)
    stages = [s for s in STAGE_ORDER if s in stages]
    if not stages:
        stages = list(d["default"]["stages"])
    auto = bool(rule.get("auto")) and d["can_auto"]
    limit = None
    if d["unit"]:
        import math
        try:
            limit = float(rule.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        if not math.isfinite(limit):
            limit = 0
        step = 0.5 if d["unit"] == "hours" else 1
        limit = max(step, min(round(limit / step) * step, d["max_limit"]))
        if d["unit"] == "days":
            limit = int(limit)
    return {"auto": auto, "limit": limit, "stages": stages}


def save(rtype, auto, limit, stages):
    rule = normalize(rtype, {"auto": auto, "limit": limit, "stages": stages})
    row = db.session.get(Setting, _key(rtype))
    value = json.dumps(rule)
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=_key(rtype), value=value))
    return rule


def describe(rtype, rule=None):
    from .workflow import STAGE_LABELS
    rule = rule or get(rtype)
    d = DEFS[rtype]
    chain = " ← ".join(STAGE_LABELS[s] for s in rule["stages"])
    if rule["auto"]:
        if d["unit"]:
            return f"تلقائي حتى {rule['limit']:g} {UNIT_LABELS[d['unit']]}، وأكثر من كذا يمر على: {chain}"
        return "تلقائي دائمًا"
    return f"يمر على: {chain}"
