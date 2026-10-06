"""إعدادات الشركة القابلة للتعديل من صفحة الإعدادات. كل شركة لها قيمها بدون تعديل الكود."""
from ..models import Setting, db

DEFAULTS = {
    "cutoff_day": ("20", "يوم إغلاق المسير", "int", "الطلبات المالية بعد هذا اليوم تدخل مسير الشهر الجاي"),
    "remind_hours": ("24", "تذكير الموافق بعد (ساعة)", "int", "إذا بقي الطلب عند شخص هذه المدة يوصله تذكير"),
    "escalate_hours": ("48", "التصعيد بعد (ساعة)", "int", "بعدها يُصعّد الطلب لمدير الموافق أو لمدير الموارد البشرية"),
    "work_start": ("08:00", "بداية الدوام", "time", "يُحسب التأخير منها"),
    "grace_minutes": ("15", "فترة السماح للتأخير (دقيقة)", "int", "تأخير أقل منها لا يُحتسب"),
    "weekend": ("4,5", "أيام العطلة الأسبوعية", "text", "أرقام أيام الأسبوع: الإثنين 0 ... الجمعة 4، السبت 5، الأحد 6"),
    "absence_reminder_days": ("1", "تذكير الموظف بالغياب غير المبرر بعد (يوم)", "int", "يوصله إيميل يطلب منه رفع إجازة أو استئذان"),
    "leave_carry_max": ("30", "أقصى رصيد إجازات يُرحّل للسنة الجديدة (يوم)", "int",
                        "عند ذكرى المباشرة يتجدد الرصيد، والمتبقي يُرحّل لين هذا الحد"),
    "ai_enabled": ("0", "تفعيل الذكاء الاصطناعي", "bool",
                   "معطل افتراضيًا: معالجة البيانات خارج المملكة تحتاج موافقة الشركة وتقييم مخاطر حسب نظام حماية البيانات"),
}


def get(key):
    row = db.session.get(Setting, key)
    return row.value if row else DEFAULTS[key][0]


def get_int(key):
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return int(DEFAULTS[key][0])


def get_bool(key):
    return get(key) in ("1", "true", "on", "yes")


def weekend_days():
    out = set()
    for p in get("weekend").split(","):
        p = p.strip()
        if p.isdigit() and 0 <= int(p) <= 6:
            out.add(int(p))
    return out or {4, 5}


def set_value(key, value):
    if key not in DEFAULTS:
        raise KeyError(key)
    row = db.session.get(Setting, key)
    if row:
        row.value = str(value)
    else:
        db.session.add(Setting(key=key, value=str(value)))
