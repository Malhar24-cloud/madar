"""سجل التدقيق: للإضافة فقط، وكل سطر يحمل بصمة السطر اللي قبله (سلسلة)، فأي تعديل أو حذف لسطر قديم
مباشرة من قاعدة البيانات ينكشف بأمر: flask verify-audit"""
import hashlib
import json

from flask import has_request_context, request
from flask_login import current_user

from ..models import AuditLog, db, utcnow


def _digest(prev, row):
    payload = json.dumps([prev or "", row.at.isoformat(timespec="seconds"), row.actor, row.action, row.detail or "",
                          row.ip or "", bool(row.automated)], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def log(action, detail="", actor=None, automated=False):
    if actor is None:
        if automated:
            actor = "الأتمتة"
        elif has_request_context() and current_user.is_authenticated:
            actor = current_user.login_name + (" (دعم فني)" if current_user.base_role == "operator" else "")
        else:
            actor = "النظام"
    ip = request.remote_addr if has_request_context() else None
    if db.engine.dialect.name == "postgresql":
        # قفل قصير داخل المعاملة حتى ما يكتب طلبان متزامنان سطرين على نفس البصمة السابقة
        db.session.execute(db.text("SELECT pg_advisory_xact_lock(724001)"))
    last = AuditLog.query.order_by(AuditLog.id.desc()).first()  # يدفع السطور المعلقة أولًا (autoflush)
    row = AuditLog(at=utcnow().replace(microsecond=0), actor=str(actor)[:160], action=action[:120],
                   detail=(detail or "")[:1000], ip=ip, automated=automated)
    row.prev_hash = last.hash if last else None
    row.hash = _digest(row.prev_hash, row)
    db.session.add(row)
    db.session.flush()


def verify_chain():
    prev, started, saw_legacy = None, False, False
    for row in AuditLog.query.order_by(AuditLog.id).yield_per(500):
        if row.hash is None:
            if started:
                return False, row.id
            saw_legacy = True
            continue
        if started and row.prev_hash != prev:
            return False, row.id
        if not started and row.prev_hash is not None and not saw_legacy:
            return False, row.id
        if _digest(row.prev_hash, row) != row.hash:
            return False, row.id
        prev, started = row.hash, True
    return True, None
