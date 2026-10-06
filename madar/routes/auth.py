import io
import re
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from ..models import Employee, User, db, utcnow
from ..security import client_ip, hit, limited, mark_fresh, password_problem, too_many
from ..services import audit, tokens

bp = Blueprint("auth", __name__)
_DUMMY_HASH = generate_password_hash("timing-equalizer")
GENERIC_FAIL = "بيانات الدخول غير صحيحة، أو الحساب مقفل مؤقتًا بعد محاولات متكررة."


def _safe_next(target):
    """يقبل مسارًا داخليًا فقط. يرفض //evil.com و /\\evil.com وأي رموز تحكم مخفية (مثل /\t/evil.com)."""
    if not target or len(target) > 500 or any(ord(c) < 32 or c == "\x7f" for c in target):
        return None
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    from urllib.parse import urlparse
    p = urlparse(target)
    return target if not p.scheme and not p.netloc else None


def find_user(identifier):
    """الدخول بالبريد أو بالرقم الوظيفي (للعمال اللي ما عندهم بريد)."""
    ident = (identifier or "").strip()
    if "@" in ident:
        return User.query.filter_by(email=ident.lower()).first()
    emp = Employee.query.filter_by(code=ident.upper()).first()
    return emp.user if emp else None


def _needs_2fa(user):
    """أي أحد عنده صلاحية على بيانات الموظفين (رواتب، هويات، آيبانات) أو المسؤول التقني: التحقق بخطوتين إجباري.
    والموظف العادي اللي فعّله بنفسه يُطلب منه أيضًا."""
    if user.totp_secret:
        return True
    return bool(current_app.config.get("REQUIRE_2FA")) and (user.is_staff or user.base_role == "operator")


def _fail(user, identifier):
    cfg = current_app.config
    if user:
        user.failed_logins += 1
        if user.failed_logins >= cfg["LOGIN_MAX_FAILS"]:
            user.locked_until = utcnow() + timedelta(minutes=cfg["LOGIN_LOCK_MINUTES"])
            audit.log("قفل حساب", f"{user.failed_logins} محاولات فاشلة", actor=user.login_name)
    audit.log("محاولة دخول فاشلة", actor=(identifier or "مجهول")[:160])


def _finish_login(user, method="كلمة المرور"):
    user.failed_logins, user.locked_until, user.last_login = 0, None, utcnow()
    nxt = session.pop("login_next", None)
    session.clear()  # جلسة جديدة بالكامل بعد الدخول (يمنع تثبيت الجلسة Session Fixation)
    login_user(user)
    now = utcnow().timestamp()
    session["auth_at"] = session["last_seen"] = now
    mark_fresh()
    audit.log("تسجيل دخول", f"عبر {method}", actor=user.login_name)
    db.session.commit()
    if user.must_change_password:
        flash("لازم تغيّر كلمة المرور المؤقتة قبل ما تكمل.", "warn")
        return redirect(url_for("auth.change_password"))
    return redirect(_safe_next(nxt) or url_for("main.dashboard"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    error = None
    if request.method == "POST":
        ident = (request.form.get("email") or "").strip()[:255]
        password = (request.form.get("password") or "")[:200]
        if too_many("login-ip", client_ip(), 30, 900) or too_many("login-id", ident, 10, 900):
            audit.log("حظر مؤقت لمحاولات الدخول", actor=ident[:160] or "مجهول")
            db.session.commit()
            return render_template("auth/login.html", error="محاولات كثيرة. انتظر 15 دقيقة وحاول مرة ثانية.",
                                   sso=bool(current_app.config.get("OIDC_PROVIDER"))), 429
        user = find_user(ident)
        if user and user.locked_until and user.locked_until > utcnow():
            check_password_hash(_DUMMY_HASH, password)
            hit("login-ip", client_ip())
            hit("login-id", ident)
            db.session.commit()
            error = GENERIC_FAIL
        elif user and user.active and user.password_hash and user.check_password(password):
            if _needs_2fa(user):
                session.clear()
                session["2fa_uid"] = user.id
                session["2fa_at"] = utcnow().timestamp()
                session["login_next"] = _safe_next(request.args.get("next"))
                db.session.commit()
                return redirect(url_for("auth.two_factor"))
            session["login_next"] = _safe_next(request.args.get("next"))
            return _finish_login(user)
        else:
            if not user or not user.active or not user.password_hash:
                check_password_hash(_DUMMY_HASH, password)  # زمن استجابة متقارب لإخفاء وجود الحساب أو حالته
            _fail(user, ident)
            hit("login-ip", client_ip())
            hit("login-id", ident)
            db.session.commit()
            error = GENERIC_FAIL
    return render_template("auth/login.html", error=error, sso=bool(current_app.config.get("OIDC_PROVIDER")))


def _pending_user():
    uid, at = session.get("2fa_uid"), session.get("2fa_at", 0)
    if not uid or utcnow().timestamp() - at > 600:
        return None
    u = db.session.get(User, uid)
    return u if u and u.active else None


def _qr_svg(uri):
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.find("<svg"):]


def verify_totp(user, secret, code):
    """يقبل الرمز الحالي أو المجاور (فرق ساعة الجوال)، ويرفض إعادة استخدام نفس الرمز مرتين (Replay)."""
    import hmac
    import time

    import pyotp
    code = (code or "").replace(" ", "")
    if not re.fullmatch(r"\d{6}", code):
        return False
    totp = pyotp.TOTP(secret)
    now_step = int(time.time() // totp.interval)
    for step in (now_step - 1, now_step, now_step + 1):
        if user.totp_last_step and step <= user.totp_last_step:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            user.totp_last_step = step
            return True
    return False


@bp.route("/login/2fa", methods=["GET", "POST"])
def two_factor():
    import pyotp
    user = _pending_user()
    if not user:
        flash("انتهت مهلة التحقق. سجّل دخولك من جديد.", "bad")
        return redirect(url_for("auth.login"))
    setup = not user.totp_secret
    if setup and "2fa_secret" not in session:
        session["2fa_secret"] = pyotp.random_base32()
    secret = user.totp_secret or session["2fa_secret"]
    error = None
    if request.method == "POST":
        if (user.locked_until and user.locked_until > utcnow()) or limited("2fa", user.id, 10, 900):
            db.session.commit()
            error = "محاولات كثيرة. الحساب مقفل مؤقتًا."
        elif verify_totp(user, secret, request.form.get("code")):
            if setup:
                user.totp_secret = secret
                audit.log("تفعيل التحقق بخطوتين", actor=user.login_name)
            return _finish_login(user, "كلمة المرور + رمز التحقق")
        else:
            _fail(user, user.login_name)
            db.session.commit()
            error = "الرمز غير صحيح."
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.login_name, issuer_name="Madar HR") if setup else None
    return render_template("auth/two_factor.html", setup=setup, secret=secret if setup else None,
                           qr=_qr_svg(uri) if uri else None, error=error)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit.log("تسجيل خروج")
    db.session.commit()
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/set-password/<token>", methods=["GET", "POST"])
def set_password(token):
    if limited("token-ip", client_ip(), 30, 900):
        db.session.commit()
        abort(429)
    t = tokens.lookup(token, "set_password")
    db.session.commit()
    if not t or not t.user.active:
        return render_template("auth/set_password.html", invalid=True), 410
    error = None
    if request.method == "POST":
        pw, pw2 = request.form.get("password") or "", request.form.get("password2") or ""
        error = password_problem(pw, t.user) or (None if pw == pw2 else "كلمتا المرور غير متطابقتين.")
        if not error:
            t.user.set_password(pw)
            t.user.must_change_password = False
            t.user.session_version += 1
            tokens.mark_used(t)
            audit.log("تعيين كلمة المرور", actor=t.user.login_name)
            db.session.commit()
            flash("تم تفعيل حسابك. سجّل دخولك الآن.", "good")
            return redirect(url_for("auth.login"))
    return render_template("auth/set_password.html", invalid=False, error=error, email=t.user.login_name)


@bp.route("/activate", methods=["GET", "POST"])
def activate():
    """تفعيل حساب العامل اللي ما عنده بريد: الرقم الوظيفي + رمز التفعيل المطبوع من الموارد البشرية."""
    error = None
    if request.method == "POST":
        if limited("activate-ip", client_ip(), 10, 900):
            db.session.commit()
            return render_template("auth/activate.html", error="محاولات كثيرة. انتظر 15 دقيقة."), 429
        code = (request.form.get("code") or "").strip().upper()[:20]
        emp = Employee.query.filter_by(code=code).first()
        user = emp.user if emp else None
        t = tokens.check_code("activate", user.id, request.form.get("activation") or "") if user and user.active else None
        if not t:
            db.session.commit()
            error = "الرقم الوظيفي أو رمز التفعيل غير صحيح، أو انتهت صلاحية الرمز. بعد 5 محاولات خاطئة يُلغى الرمز."
        else:
            pw, pw2 = request.form.get("password") or "", request.form.get("password2") or ""
            error = password_problem(pw, user) or (None if pw == pw2 else "كلمتا المرور غير متطابقتين.")
            if not error:
                user.set_password(pw)
                user.must_change_password = False
                user.session_version += 1
                tokens.mark_used(t)
                audit.log("تفعيل حساب برمز", actor=code)
                db.session.commit()
                flash("تم تفعيل حسابك. ادخل برقمك الوظيفي وكلمة المرور.", "good")
                return redirect(url_for("auth.login"))
    return render_template("auth/activate.html", error=error)


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    error = None
    if request.method == "POST":
        if limited("pwchange", current_user.id, 10, 900):
            db.session.commit()
            abort(429)
        if not current_user.check_password((request.form.get("current") or "")[:200]):
            error = "كلمة المرور الحالية غير صحيحة."
        else:
            pw = request.form.get("password") or ""
            error = password_problem(pw, current_user) or (None if pw == request.form.get("password2") else "كلمتا المرور غير متطابقتين.")
        if not error:
            current_user.set_password(pw)
            current_user.must_change_password = False
            current_user.session_version += 1
            audit.log("تغيير كلمة المرور")
            db.session.commit()
            login_user(current_user)
            session["auth_at"] = session["last_seen"] = utcnow().timestamp()
            mark_fresh()
            flash("تم تغيير كلمة المرور وإخراج الجلسات الأخرى.", "good")
            return redirect(url_for("main.dashboard"))
    return render_template("auth/change_password.html", error=error)


@bp.route("/reauth", methods=["GET", "POST"])
@login_required
def reauth():
    """تأكيد الهوية قبل العمليات الحساسة (الصلاحيات، القواعد، التصدير، إنهاء الخدمة...)."""
    nxt = _safe_next(request.values.get("next")) or url_for("main.dashboard")
    u = current_user._get_current_object()
    if not u.password_hash:
        if current_app.config.get("OIDC_PROVIDER"):
            session["sso_reauth"] = {"uid": u.id, "next": nxt}
            return redirect(url_for("auth.sso_start"))
        abort(403)
    error = None
    if request.method == "POST":
        if limited("reauth", u.id, 8, 900):
            db.session.commit()
            logout_user()
            session.clear()
            flash("محاولات كثيرة. سجّل دخولك من جديد.", "bad")
            return redirect(url_for("auth.login"))
        ok = u.check_password((request.form.get("password") or "")[:200])
        if ok and u.totp_secret:
            ok = verify_totp(u, u.totp_secret, request.form.get("code"))
        if ok:
            mark_fresh()
            audit.log("تأكيد الهوية لعملية حساسة")
            db.session.commit()
            return redirect(nxt)
        audit.log("فشل تأكيد الهوية")
        db.session.commit()
        error = "البيانات غير صحيحة."
    return render_template("auth/reauth.html", error=error, nxt=nxt, needs_code=bool(u.totp_secret))


# ---------- الدخول الموحد (SSO) ----------
_oauth = None


def _sso_client():
    global _oauth
    cfg = current_app.config
    if not cfg.get("OIDC_PROVIDER"):
        return None
    from authlib.integrations.flask_client import OAuth
    if _oauth is None or _oauth.app is not current_app._get_current_object():
        _oauth = OAuth(current_app._get_current_object())
        if cfg["OIDC_PROVIDER"] == "microsoft":
            # لازم معرّف المستأجر (GUID) لشركتك، وإلا أي حساب Microsoft في العالم يقدر يجرب الدخول
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", cfg.get("OIDC_TENANT") or ""):
                current_app.logger.error("OIDC_TENANT must be your Entra tenant GUID")
                return None
            meta = f"https://login.microsoftonline.com/{cfg['OIDC_TENANT']}/v2.0/.well-known/openid-configuration"
        else:
            meta = "https://accounts.google.com/.well-known/openid-configuration"
        _oauth.register("madar", client_id=cfg["OIDC_CLIENT_ID"], client_secret=cfg["OIDC_CLIENT_SECRET"],
                        server_metadata_url=meta, client_kwargs={"scope": "openid email profile"})
    return _oauth.madar


@bp.route("/login/sso")
def sso_start():
    client = _sso_client() or abort(404)
    # عنوان الرجوع من BASE_URL وليس من ترويسة Host اللي يقدر المهاجم يغيرها
    redirect_uri = current_app.config["BASE_URL"] + url_for("auth.sso_callback")
    extra = {"prompt": "login"} if session.get("sso_reauth") else {}
    return client.authorize_redirect(redirect_uri, **extra)


@bp.route("/login/sso/callback")
def sso_callback():
    client = _sso_client() or abort(404)
    try:
        token = client.authorize_access_token()
    except Exception as ex:
        current_app.logger.warning("SSO failed: %s", ex)
        flash("تعذّر الدخول بحساب الشركة. حاول مرة ثانية.", "bad")
        return redirect(url_for("auth.login"))
    info = token.get("userinfo") or {}
    if current_app.config["OIDC_PROVIDER"] == "microsoft" and \
            str(info.get("tid", "")).lower() != current_app.config["OIDC_TENANT"].lower():
        audit.log("رفض دخول موحد", "حساب من خارج مستأجر الشركة")
        db.session.commit()
        flash("هذا الحساب غير مسموح له بالدخول.", "bad")
        return redirect(url_for("auth.login"))
    email = (info.get("email") or info.get("preferred_username") or "").lower()
    domain = current_app.config.get("OIDC_ALLOWED_DOMAIN")
    if current_app.config["OIDC_PROVIDER"] == "google" and not info.get("email_verified"):
        email = ""
    if not email or (domain and not email.endswith("@" + domain.lower())):
        audit.log("رفض دخول موحد", email or "بدون بريد")
        db.session.commit()
        flash("هذا الحساب غير مسموح له بالدخول.", "bad")
        return redirect(url_for("auth.login"))
    user = User.query.filter_by(email=email).first()
    if not user or not user.active:
        audit.log("رفض دخول موحد", f"{email}: لا يوجد حساب مفعّل")
        db.session.commit()
        flash("ما عندك حساب مفعّل في مدار. تواصل مع الموارد البشرية.", "bad")
        return redirect(url_for("auth.login"))
    sub = info.get("sub")
    if user.sso_subject and sub and user.sso_subject != sub:
        audit.log("رفض دخول موحد", f"{email}: معرّف الحساب لا يطابق")
        db.session.commit()
        flash("تعذّر التحقق من الحساب.", "bad")
        return redirect(url_for("auth.login"))
    user.sso_subject = user.sso_subject or sub
    re_auth = session.pop("sso_reauth", None)
    if re_auth and current_user.is_authenticated:
        if re_auth.get("uid") == current_user.id == user.id:
            mark_fresh()
            audit.log("تأكيد الهوية لعملية حساسة", "عبر الدخول الموحد")
            db.session.commit()
            return redirect(_safe_next(re_auth.get("next")) or url_for("main.dashboard"))
        abort(403)
    # التحقق بخطوتين هنا مسؤولية Microsoft أو Google: فعّله إجباريًا من لوحة إدارتهم
    return _finish_login(user, current_app.config["OIDC_PROVIDER"])
