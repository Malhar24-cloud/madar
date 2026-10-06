from flask import current_app, render_template

from .. import policy
from ..models import Letter, db

LETTER_TYPES = {
    "salary": "تعريف بالراتب",
    "exp": "شهادة خبرة",
    "warning": "إنذار كتابي",
    "term": "إشعار إنهاء خدمة",
}


def next_number():
    year = policy.today().year
    count = Letter.query.filter(Letter.number.like(f"HR-{year}-%")).count()
    return f"HR-{year}-{count + 1:04d}"


def issue(ltype, emp, addressee="", request_id=None, note="", issued_by="النظام"):
    if ltype not in LETTER_TYPES:
        raise ValueError("unknown letter type")
    number = next_number()
    html = render_template(f"letters/{ltype}.html", emp=emp, number=number, date=policy.today(),
                           addressee=addressee or "من يهمه الأمر", note=note,
                           company=current_app.config["COMPANY_NAME"], policy=policy)
    letter = Letter(number=number, type=ltype, employee_id=emp.id, addressee=addressee, html=html,
                    request_id=request_id, issued_by=issued_by)
    db.session.add(letter)
    db.session.flush()
    return letter
