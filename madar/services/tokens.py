import hashlib
import hmac
import secrets
from datetime import timedelta

from ..models import ActionToken, db, utcnow


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def issue(purpose, user_id, hours, request_id=None, stage_index=None):
    raw = secrets.token_urlsafe(32)
    db.session.add(ActionToken(token_hash=_hash(raw), purpose=purpose, user_id=user_id, request_id=request_id,
                               stage_index=stage_index, expires_at=utcnow() + timedelta(hours=hours)))
    return raw


def lookup(raw, purpose):
    if not raw or len(raw) > 100:
        return None
    t = ActionToken.query.filter_by(token_hash=_hash(raw), purpose=purpose).first()
    if not t or t.used_at or t.expires_at < utcnow():
        return None
    return t


def mark_used(token):
    token.used_at = utcnow()


def revoke_request(request_id, stage_index=None):
    q = ActionToken.query.filter(ActionToken.request_id == request_id, ActionToken.used_at.is_(None))
    if stage_index is not None:
        q = q.filter(ActionToken.stage_index == stage_index)
    q.update({"used_at": utcnow()}, synchronize_session=False)


def revoke_user(user_id):
    ActionToken.query.filter(ActionToken.user_id == user_id, ActionToken.used_at.is_(None)) \
        .update({"used_at": utcnow()}, synchronize_session=False)


def _code_hash(user_id, code):
    return _hash(f"{user_id}:{code.replace('-', '').upper()}")


def issue_code(purpose, user_id, code, hours):
    """رمز قصير يُطبع ويُسلّم يدويًا للعامل اللي ما عنده بريد. نخزن البصمة فقط."""
    ActionToken.query.filter(ActionToken.user_id == user_id, ActionToken.purpose == purpose, ActionToken.used_at.is_(None)) \
        .update({"used_at": utcnow()}, synchronize_session=False)
    db.session.add(ActionToken(token_hash=_code_hash(user_id, code), purpose=purpose, user_id=user_id,
                               expires_at=utcnow() + timedelta(hours=hours)))


def check_code(purpose, user_id, code, max_attempts=5):
    """يرجع الرمز إذا صحيح. خمس محاولات خاطئة تلغي الرمز."""
    t = ActionToken.query.filter(ActionToken.user_id == user_id, ActionToken.purpose == purpose,
                                 ActionToken.used_at.is_(None), ActionToken.expires_at > utcnow()).first()
    if not t:
        return None
    if hmac.compare_digest(t.token_hash, _code_hash(user_id, code or "")):
        return t
    t.attempts += 1
    if t.attempts >= max_attempts:
        t.used_at = utcnow()
    return None
