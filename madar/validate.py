"""تحقق موحّد من كل مدخل يكتبه المستخدم: أرقام، تواريخ، نصوص، بريد، آيبان، هوية.
القاعدة: أي قيمة من خارج النظام تمر من هنا قبل ما توصل لقاعدة البيانات أو الحسابات.
كل دالة ترجع القيمة النظيفة أو ترفع ValueError برسالة عربية واضحة."""
import math
import re
import unicodedata
from datetime import date, datetime, time, timedelta

MAX_MONEY = 1_000_000          # أعلى راتب أو مبلغ يقبله النظام في خانة واحدة
EMAIL_RE = re.compile(r"[a-z0-9._%+\-]{1,64}@[a-z0-9\-]{1,63}(\.[a-z0-9\-]{1,63})+")
_CONTROL = {"Cc", "Cf", "Cs", "Co", "Cn"}


def clean_text(value, max_len, multiline=False):
    """يشيل رموز التحكم المخفية (مثل اتجاه النص المعكوس RLO اللي يُستخدم للتمويه) ويقص الطول."""
    s = "" if value is None else str(value)
    s = unicodedata.normalize("NFC", s)
    out = []
    for ch in s:
        if ch in "\n\t":
            out.append(ch if multiline else " ")
        elif ch == "\r":
            continue
        elif unicodedata.category(ch) in _CONTROL and ch not in ("‌", "‍"):  # نسمح بفواصل الحروف العربية فقط
            continue
        else:
            out.append(ch)
    s = "".join(out).strip()
    if not multiline:
        s = " ".join(s.split())
    return s[:max_len]


def required_text(value, label, max_len, multiline=False):
    s = clean_text(value, max_len, multiline)
    if not s:
        raise ValueError(f"{label} مطلوب.")
    return s


def number(value, label, lo=0.0, hi=MAX_MONEY, step=None, allow_empty=False):
    """رقم محدود: يرفض NaN وما لا نهاية والسالب والقيم الضخمة."""
    s = clean_text(value, 40).replace(",", "").replace("٫", ".")
    s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    if not s:
        if allow_empty:
            return None
        raise ValueError(f"{label} مطلوب.")
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        raise ValueError(f"{label}: أدخل رقمًا صحيحًا.")
    n = float(s)
    if not math.isfinite(n) or n < lo or n > hi:
        raise ValueError(f"{label} لازم يكون بين {lo:g} و{hi:,.0f}.")
    if step and abs(round(n / step) * step - n) > 1e-9:
        raise ValueError(f"{label}: بخطوات {step:g}.")
    return n


def integer(value, label, lo=0, hi=MAX_MONEY, allow_empty=False):
    n = number(value, label, lo, hi, allow_empty=allow_empty)
    if n is None:
        return None
    if not float(n).is_integer():
        raise ValueError(f"{label}: أدخل رقمًا صحيحًا بدون كسور.")
    return int(n)


def day(value, label, lo=None, hi=None, allow_empty=False):
    s = clean_text(value, 20) if not isinstance(value, (date, datetime)) else value
    if isinstance(s, datetime):
        d = s.date()
    elif isinstance(s, date):
        d = s
    elif not s:
        if allow_empty:
            return None
        raise ValueError(f"{label} مطلوب.")
    else:
        try:
            d = date.fromisoformat(s)
        except ValueError:
            raise ValueError(f"{label} غير صحيح.")
    lo = lo or date(1950, 1, 1)
    hi = hi or date(2100, 12, 31)
    if not lo <= d <= hi:
        raise ValueError(f"{label} لازم يكون بين {lo} و{hi}.")
    return d


def clock(value, label):
    s = clean_text(value, 5)
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s):
        raise ValueError(f"{label}: الصيغة HH:MM.")
    return s


def choice(value, allowed, label):
    if value not in allowed:
        raise ValueError(f"{label} غير معروف.")
    return value


def email(value, allow_empty=True):
    s = clean_text(value, 254).lower()
    if not s:
        if allow_empty:
            return None
        raise ValueError("البريد مطلوب.")
    if not EMAIL_RE.fullmatch(s):
        raise ValueError("البريد غير صحيح.")
    return s


def iban(value, allow_empty=True):
    s = clean_text(value, 40).replace(" ", "").upper()
    if not s:
        if allow_empty:
            return None
        raise ValueError("الآيبان مطلوب.")
    if not re.fullmatch(r"SA\d{22}", s):
        raise ValueError("الآيبان السعودي يبدأ بـ SA وبعده 22 رقمًا.")
    # تحقق رقم الفحص (ISO 13616) يكشف أخطاء الكتابة قبل ما يرجع التحويل من البنك
    moved = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in moved)
    if int(digits) % 97 != 1:
        raise ValueError("الآيبان غير صحيح (رقم التحقق لا يطابق). تأكد من الأرقام.")
    return s


def national_id(value, allow_empty=True):
    s = re.sub(r"\s", "", clean_text(value, 20))
    if not s:
        if allow_empty:
            return None
        raise ValueError("رقم الهوية مطلوب.")
    if not re.fullmatch(r"[12]\d{9}", s):
        raise ValueError("رقم الهوية أو الإقامة 10 أرقام ويبدأ بـ 1 أو 2.")
    return s


def phone(value):
    s = re.sub(r"[\s\-]", "", clean_text(value, 20))
    if not s:
        return None
    if s.startswith("+966"):
        s = "0" + s[4:]
    if not re.fullmatch(r"05\d{8}", s):
        raise ValueError("الجوال بصيغة 05xxxxxxxx.")
    return s


def code(value, label="الرقم", max_len=20):
    s = clean_text(value, max_len).upper()
    if s and not re.fullmatch(r"[A-Z0-9\-_]{1,%d}" % max_len, s):
        raise ValueError(f"{label}: حروف إنجليزية وأرقام وشرطة فقط.")
    return s or None


def today_window(today, past_days, future_days):
    return today - timedelta(days=past_days), today + timedelta(days=future_days)


__all__ = ["clean_text", "required_text", "number", "integer", "day", "clock", "choice", "email", "iban",
           "national_id", "phone", "code", "today_window", "MAX_MONEY", "time"]
