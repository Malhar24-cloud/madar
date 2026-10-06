"""أدوات الحماية المشتركة: تحديد معدل المحاولات، إعادة التحقق قبل العمليات الحساسة، قوة كلمة المرور،
وتعقيم الخلايا قبل التصدير إلى Excel/CSV."""
import hashlib
import re
from datetime import timedelta
from functools import wraps

from flask import current_app, redirect, request, session, url_for
from flask_login import current_user

from .models import RateHit, db, utcnow


# ---------------- تحديد معدل المحاولات ----------------
def client_ip():
    return request.remote_addr or "?"


def _bucket(name, key):
    # نخزن بصمة المفتاح وليس البريد أو الـIP نفسه
    return f"{name}:{hashlib.sha256(str(key).lower().encode()).hexdigest()[:40]}"


def too_many(name, key, limit, seconds):
    """يرجع True إذا تجاوز الحد خلال المدة. ما يسجل محاولة جديدة؛ hit() يسجلها."""
    since = utcnow() - timedelta(seconds=seconds)
    return RateHit.query.filter(RateHit.bucket == _bucket(name, key), RateHit.at > since).count() >= limit


def hit(name, key):
    db.session.add(RateHit(bucket=_bucket(name, key)))


def limited(name, key, limit, seconds):
    """يسجل محاولة ويرجع True إذا الحد متجاوز."""
    if too_many(name, key, limit, seconds):
        return True
    hit(name, key)
    return False


def cleanup_rate_hits():
    RateHit.query.filter(RateHit.at < utcnow() - timedelta(days=2)).delete(synchronize_session=False)


# ---------------- إعادة التحقق للعمليات الحساسة ----------------
FRESH_MINUTES = 10


def mark_fresh():
    session["fresh_at"] = utcnow().timestamp()


def is_fresh():
    return utcnow().timestamp() - float(session.get("fresh_at") or 0) < FRESH_MINUTES * 60


def fresh_required(fn):
    """تغيير الصلاحيات والقواعد والتصدير وإنهاء الخدمة: يطلب كلمة المرور (ورمز التحقق) من جديد إذا مر أكثر من 10 دقائق.
    كذا لو أحد سرق جلسة مفتوحة أو استخدم جهازًا مفتوحًا ما يقدر يسوي عمليات خطيرة."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated:
            return current_app.login_manager.unauthorized()
        if not is_fresh():
            nxt = request.full_path if request.method == "GET" else (request.referrer or url_for("main.dashboard"))
            from urllib.parse import urlparse
            parsed = urlparse(nxt)
            nxt = parsed.path + (("?" + parsed.query) if parsed.query else "")
            return redirect(url_for("auth.reauth", next=nxt))
        return fn(*a, **kw)
    return wrapper


# ---------------- قوة كلمة المرور ----------------
COMMON = {
    "password1", "password123", "qwerty1234", "1234567890", "abc1234567", "p@ssw0rd123", "welcome123", "admin12345",
    "letmein123", "iloveyou12", "qwertyuiop1", "1q2w3e4r5t", "zaq12wsxcde", "saudi12345", "riyadh1234", "ksa1234567",
    "madar12345", "company123", "changeme123", "aa12345678", "a123456789", "q1w2e3r4t5", "passw0rd12",
}


def password_problem(pw, user=None):
    if len(pw) < 10:
        return "كلمة المرور لازم تكون 10 أحرف على الأقل."
    if len(pw) > 128:
        return "كلمة المرور طويلة جدًا."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "كلمة المرور لازم تحتوي على حروف إنجليزية وأرقام."
    low = pw.lower()
    if low in COMMON or len(set(low)) < 5:
        return "كلمة المرور سهلة التخمين. اختر غيرها."
    if user is not None:
        parts = []
        if user.email:
            parts.append(user.email.split("@")[0].lower())
        if user.employee:
            parts.append(user.employee.code.lower())
        if any(p and len(p) >= 3 and p in low for p in parts):
            return "كلمة المرور لا تحتوي على بريدك أو رقمك الوظيفي."
        if user.password_hash and user.check_password(pw):
            return "اختر كلمة مرور مختلفة عن الحالية."
    return None


# ---------------- التصدير الآمن ----------------
def safe_cell(v):
    """يمنع حقن الصيغ في Excel: أي نص يبدأ بـ = + - @ أو تبويب يُسبق بعلامة اقتباس."""
    if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
        try:
            float(v)
            return v  # رقم سالب عادي
        except ValueError:
            return "'" + v
    return v
