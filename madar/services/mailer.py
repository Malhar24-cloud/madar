"""إرسال البريد. الرسائل الروتينية تُرسل فورًا، والحساسة تُحفظ مسودة وتنتظر موافقة بشرية.
في الإنتاج يُفضّل نقل الإرسال إلى طابور خلفي (مثل RQ أو Celery) حتى لا ينتظر المستخدم خادم البريد."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from flask import current_app, render_template

from ..models import Email, db, utcnow
from . import tasks


SCRUBBED = "[أُرسل للمستلم]"


def _clean_header(value):
    return " ".join(str(value).split())[:250]


def queue(to_addr, to_name, subject, body, kind="auto", sensitive=False, employee_id=None, actions=None, tag=""):
    e = Email(to_addr=to_addr or "", to_name=to_name or "", subject=_clean_header(subject), body=body, actions=actions or [],
              kind=kind, sensitive=sensitive, employee_id=employee_id, tag=tag)
    db.session.add(e)
    db.session.flush()
    if kind == "auto" and not to_addr:
        e.status = "no_address"  # موظف بدون بريد: يشوف التحديث في بوابته فقط
    elif kind == "auto":
        deliver(e)
    else:
        e.status = "pending_approval"
        tasks.ensure(f"mail:{e.id}", "admin", f"مراجعة مسودة: {e.subject}", f"إلى {e.to_name} · لا تُرسل بدون موافقتك",
                     employee_id=employee_id, link=f"/outbox/{e.id}")
    return e


def build_message(e):
    cfg = current_app.config
    msg = EmailMessage()
    msg["Subject"] = _clean_header(e.subject)
    msg["From"] = cfg["MAIL_FROM"]
    to = cfg.get("MAIL_REDIRECT_TO") or e.to_addr
    msg["To"] = formataddr((_clean_header(e.to_name), to)) if e.to_name else to
    actions_txt = "".join(f"\n{label}: {url}" for label, url in (e.actions or []))
    msg.set_content(e.body + ("\n" + actions_txt if actions_txt else ""))
    msg.add_alternative(render_template("email/base.html", email=e, company=cfg["COMPANY_NAME"]), subtype="html")
    return msg


def deliver(e):
    cfg = current_app.config
    backend = cfg.get("MAIL_BACKEND", "console")
    try:
        msg = build_message(e)
        if backend == "smtp":
            with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=20) as s:
                if cfg.get("SMTP_STARTTLS"):
                    s.starttls(context=ssl.create_default_context())
                if cfg.get("SMTP_USER"):
                    s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
                s.send_message(msg)
        elif backend == "memory":
            current_app.extensions.setdefault("outbox", []).append(msg)
        else:
            _console(e, msg)
        e.status, e.sent_at, e.error = "sent", utcnow(), None
        # بعد الإرسال نمسح الروابط الشخصية من قاعدة البيانات: لو تسربت نسخة منها ما فيها روابط صالحة
        e.actions = [[label, SCRUBBED] for label, _url in (e.actions or [])]
    except Exception as ex:  # نسجل الفشل ولا نوقف العملية الأصلية
        e.status, e.error = "failed", str(ex)[:500]
        current_app.logger.warning("Email %s failed: %s", e.id, ex)
    return e


def _console(e, msg):
    """وضع التجربة: يطبع الرسالة في الطرفية. أي مشكلة في الطباعة لا تُعتبر فشل إرسال."""
    to = e.to_name + " <" + (current_app.config.get("MAIL_REDIRECT_TO") or e.to_addr) + ">"
    lines = [f"\n----- EMAIL → {to} -----", e.subject, e.body] + [f"[{l}] {u}" for l, u in (e.actions or [])] + ["-" * 36]
    text = "\n".join(lines)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "backslashreplace").decode(), flush=True)
    except OSError:
        pass
