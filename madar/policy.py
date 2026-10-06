"""قواعد نظام العمل وسياسات الشركة. كل قيمة هنا قابلة للتخصيص لكل شركة.
القيم الافتراضية مبنية على فهمنا لنظام العمل السعودي ويجب أن يراجعها مختص قبل الاعتماد عليها."""
from datetime import date

LEAVE_BASE = 21
LEAVE_SENIOR = 30
SENIOR_YEARS = 5
PROBATION_DAYS = 90
PROBATION_MAX = 180
SICK_FULL_PAY_DAYS = 30
WEEKLY_HOURS = 48
DAILY_HOURS = 8
GOSI_EMPLOYEE = 0.0975          # حصة الموظف السعودي (معاشات + ساند) للمسجلين قبل 2024-07-03
GOSI_EMPLOYER_SAUDI = 0.1175    # حصة المنشأة للسعودي (تشمل الأخطار المهنية)
GOSI_EMPLOYER_NON_SAUDI = 0.02  # الأخطار المهنية فقط لغير السعودي
GOSI_NEW_SYSTEM_START = date(2025, 7, 1)  # أول زيادة 0.5% لكل طرف للمسجلين بعد 2024-07-03
GOSI_NEW_SYSTEM_STEP = 0.005
GOSI_NEW_SYSTEM_MAX_STEPS = 4
GOSI_CAP = 45000
HOUSING_PCT = 0.25
TRANSPORT_PCT = 0.10
NOTICE_EMPLOYER = 60
NOTICE_EMPLOYEE = 30
PERMISSION_MONTHLY_MAX = 8       # حد ساعات الاستئذان في الشهر
WPS_UPLOAD_DAYS = 10             # مهلة رفع ملف حماية الأجور بعد اعتماد المسير (تحقق من المهلة الحالية في مُدد)
LOAN_MAX_BASIC_MULTIPLE = 2
LOAN_MAX_MONTHS = 12
CONTRACT_ALERT_DAYS = 90
PROBATION_ALERT_DAYS = 15
IQAMA_ALERT_DAYS = 60

DEPARTMENTS = ["الإدارة العليا", "تقنية المعلومات", "الموارد البشرية", "المالية", "المبيعات", "العمليات"]


def today():
    return date.today()


def years_between(start, end):
    if not start or not end:
        return 0.0
    return max(0.0, (end - start).days / 365.25)


def gross(emp):
    return (emp.basic or 0) + (emp.housing or 0) + (emp.transport or 0)


def entitlement(emp, on=None):
    return LEAVE_SENIOR if years_between(emp.hire_date, on or today()) >= SENIOR_YEARS else LEAVE_BASE


def leave_left(emp, on=None):
    return max(0, entitlement(emp, on) + (getattr(emp, "leave_carry", 0) or 0) - (emp.leave_used or 0))


def leave_year_start(emp, on=None):
    """بداية سنة الإجازات الحالية: آخر ذكرى للمباشرة. الرصيد يتجدد عندها، والمرضية تُحسب من جديد."""
    on = on or today()
    h = emp.hire_date
    y = on.year if (on.month, on.day) >= (h.month, h.day) else on.year - 1
    try:
        start = h.replace(year=y)
    except ValueError:  # 29 فبراير
        start = date(y, 3, 1)
    return max(start, h)


SICK_TIERS = [(30, 1.0), (60, 0.75), (30, 0.0)]  # المادة 117: 30 يوم كامل، 60 بثلاثة أرباع، 30 بدون أجر


def sick_pay_split(already_used, days):
    """يقسم أيام الإجازة المرضية الجديدة على شرائح الأجر حسب ما استُخدم قبلها في نفس السنة.
    يرجع قائمة [(أيام، نسبة الأجر)]."""
    out, pos, left = [], already_used, days
    edge = 0
    for size, rate in SICK_TIERS:
        lo, hi = edge, edge + size
        edge = hi
        if left <= 0 or pos >= hi:
            continue
        take = min(left, hi - max(pos, lo))
        if take > 0:
            out.append((take, rate))
            pos += take
            left -= take
    if left > 0:
        out.append((left, 0.0))
    return out


def _gosi_steps(on):
    if on < GOSI_NEW_SYSTEM_START:
        return 0
    years = on.year - GOSI_NEW_SYSTEM_START.year + (1 if (on.month, on.day) >= (7, 1) else 0)
    return min(GOSI_NEW_SYSTEM_MAX_STEPS, years)


def gosi_rates(emp, on=None):
    """يرجع (نسبة الموظف، نسبة المنشأة). المسجلون بعد 2024-07-03 تزيد نسبتهم 0.5% لكل طرف كل يوليو."""
    on = on or today()
    if not emp.is_saudi:
        return 0.0, GOSI_EMPLOYER_NON_SAUDI
    extra = _gosi_steps(on) * GOSI_NEW_SYSTEM_STEP if emp.gosi_new_system else 0.0
    return round(GOSI_EMPLOYEE + extra, 4), round(GOSI_EMPLOYER_SAUDI + extra, 4)


def gosi_base(emp, factor=1.0):
    return min(((emp.basic or 0) + (emp.housing or 0)) * factor, GOSI_CAP)


def gosi_employee(emp, on=None, factor=1.0):
    return round(gosi_base(emp, factor) * gosi_rates(emp, on)[0], 2)


def gosi_employer(emp, on=None, factor=1.0):
    return round(gosi_base(emp, factor) * gosi_rates(emp, on)[1], 2)


def end_of_service(emp, reason="term", on=None):
    on = on or today()
    wage = gross(emp)
    y = years_between(emp.hire_date, on)
    full = y * wage / 2 if y <= 5 else 2.5 * wage + (y - 5) * wage
    factor, why = 1.0, "إنهاء من صاحب العمل أو انتهاء العقد: المكافأة كاملة."
    if reason == "resign":
        if y < 2:
            factor, why = 0.0, "استقالة قبل سنتين: لا يستحق مكافأة."
        elif y < 5:
            factor, why = 1 / 3, "استقالة بين سنتين وخمس: ثلث المكافأة."
        elif y < 10:
            factor, why = 2 / 3, "استقالة بين خمس وعشر سنوات: ثلثا المكافأة."
        else:
            why = "استقالة بعد عشر سنوات: المكافأة كاملة."
    return {"wage": wage, "years": round(y, 2), "amount": round(full * factor, 2), "why": why}


def contract_risks(d):
    """فحص بنود عقد مستخرج مقابل القواعد. يرجع قائمة ملاحظات عربية."""
    r = []
    p = d.get("probation_days")
    if isinstance(p, (int, float)):
        if p > PROBATION_MAX:
            r.append(f"فترة التجربة {int(p)} يوم تتجاوز الحد الأقصى {PROBATION_MAX} يومًا (المادة 53).")
        elif p > PROBATION_DAYS:
            r.append(f"فترة التجربة أكثر من {PROBATION_DAYS} يومًا وتحتاج موافقة مكتوبة صريحة.")
    lv = d.get("annual_leave_days")
    if isinstance(lv, (int, float)) and lv < LEAVE_BASE:
        r.append(f"الإجازة السنوية {int(lv)} يوم أقل من الحد النظامي {LEAVE_BASE} يومًا.")
    wh = d.get("weekly_hours")
    if isinstance(wh, (int, float)) and wh > WEEKLY_HOURS:
        r.append(f"ساعات العمل الأسبوعية تتجاوز {WEEKLY_HOURS} ساعة.")
    if d.get("contract_type") == "fixed" and not d.get("end_date"):
        r.append("العقد محدد المدة ولا يذكر تاريخ انتهاء.")
    for c in d.get("special_clauses") or []:
        if "منافس" in str(c):
            r.append("بند عدم المنافسة يُشترط أن يكون مكتوبًا ومحددًا بالمدة والمكان ونوع العمل وألا يتجاوز سنتين (المادة 83).")
    for key, label in (("name", "اسم الموظف"), ("start_date", "تاريخ المباشرة"), ("basic_salary", "الراتب الأساسي")):
        if not d.get(key):
            r.append(f"حقل أساسي غير موجود: {label}.")
    return r


POLICY_SUMMARY = f"""الإجازة السنوية {LEAVE_BASE} يومًا وترتفع إلى {LEAVE_SENIOR} بعد {SENIOR_YEARS} سنوات.
فترة التجربة {PROBATION_DAYS} يومًا وتمدد كتابيًا حتى {PROBATION_MAX}.
الإجازة المرضية: 30 يومًا بأجر كامل، ثم 60 بثلاثة أرباع الأجر، ثم 30 بدون أجر.
ساعات العمل {DAILY_HOURS} يوميًا و{WEEKLY_HOURS} أسبوعيًا.
مكافأة نهاية الخدمة: نصف شهر عن كل سنة من أول خمس، ثم شهر عن كل سنة. الاستقالة: أقل من سنتين لا شيء، 2-5 ثلث، 5-10 ثلثان، 10+ كاملة.
التأمينات: {GOSI_EMPLOYEE*100:.2f}% من الأساسي والسكن للموظف السعودي بسقف {GOSI_CAP:,}، وتزيد 0.5% كل يوليو للمسجلين بعد 2024-07-03.
الاستئذان: حده {PERMISSION_MONTHLY_MAX} ساعات في الشهر.
السلفة: حدها {LOAN_MAX_BASIC_MULTIPLE} رواتب أساسية، وسلفة واحدة قائمة فقط، وحتى {LOAN_MAX_MONTHS} قسطًا.
مهلة الإشعار: {NOTICE_EMPLOYER} يومًا من صاحب العمل و{NOTICE_EMPLOYEE} يومًا من الموظف."""
