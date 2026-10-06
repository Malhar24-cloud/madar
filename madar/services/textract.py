"""استخراج النص من الملفات في الذاكرة. لا يُحفظ الملف على القرص."""
import io
import re
from datetime import date

from .. import policy

ALLOWED = (".pdf", ".docx", ".txt", ".md")


def extract_text(file_storage):
    name = (file_storage.filename or "").lower()
    if not name.endswith(ALLOWED):
        raise ValueError("نوع الملف غير مدعوم. ارفع PDF أو Word أو نص.")
    data = file_storage.read()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("الملف محمي بكلمة مرور.")
        text = "\n".join((p.extract_text() or "")[:20000] for p in reader.pages[:15])
        if len(text.strip()) < 20:
            raise ValueError("الملف يبدو ممسوحًا ضوئيًا. ارفع نسخة نصية أو الصق النص.")
        return text
    if name.endswith(".docx"):
        from ..filesafe import check_zip
        check_zip(data)
        import docx
        d = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs)
    return data.decode("utf-8", errors="replace")


_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _num(s):
    if not s:
        return None
    m = re.search(r"\d+(?:\.\d+)?", s.translate(_AR_DIGITS).replace(",", "").replace("٬", ""))
    return float(m.group()) if m else None


def _date(s):
    if not s:
        return None
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s.translate(_AR_DIGITS))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def local_contract_extract(text):
    """استخراج بالقواعد عندما لا يتوفر الذكاء الاصطناعي. يناسب العقود المكتوبة بصيغة «العنوان: القيمة»."""
    def lbl(pattern):
        m = re.search(r"(?:" + pattern + r")[^:：\n]*[:：]\s*([^\n]+)", text)
        return m.group(1).strip() if m else None

    type_line = lbl("نوع العقد") or ""
    dept = lbl("القسم") or ""
    name = (lbl("الطرف الثاني") or lbl("اسم الموظف") or "")
    name = re.sub(r"^\(?الموظف\)?\s*[:：]?\s*", "", name).strip() or None
    hours = lbl("ساعات العمل") or ""
    weekly = _num(hours.split("،")[1]) if "،" in hours else None
    return {
        "name": name, "nationality": lbl("الجنسية"), "title": lbl("المسمى الوظيفي"),
        "department": next((d for d in policy.DEPARTMENTS if dept and (d in dept or dept in d)), None),
        "basic_salary": _num(lbl("الراتب الأساسي")), "housing_allowance": _num(lbl("بدل السكن")),
        "transport_allowance": _num(lbl("بدل النقل")), "start_date": _date(lbl("تاريخ المباشرة|تاريخ بداية")),
        "contract_type": "open" if "غير محدد" in type_line else ("fixed" if "محدد" in type_line else None),
        "end_date": _date(lbl("تاريخ انتهاء") or type_line), "probation_days": _num(lbl("فترة التجربة")),
        "annual_leave_days": _num(lbl("الإجازة السنوية")), "notice_days": _num(lbl("مهلة الإشعار")), "weekly_hours": weekly,
        "special_clauses": [c.strip() for c in re.findall(r"^\s*(بند[^:：\n]*)[:：]", text, flags=re.M)],
    }
