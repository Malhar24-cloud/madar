import os

from flask import Flask, abort, flash, has_request_context, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, logout_user
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFError, CSRFProtect

from .config import Config
from .models import User, db

login_manager = LoginManager()
csrf = CSRFProtect()
migrate = Migrate()

ANY = lambda u: True  # noqa: E731
STAFF_OR_MGR = lambda u: u.is_staff or u.is_manager  # noqa: E731


def _p(*perms, manager=False):
    return lambda u: u.can(*perms) or (manager and u.is_manager)


# كل عنصر في القائمة يظهر حسب الصلاحيات، والحماية الفعلية في الخادم (perm_required)
NAV = [
    ("main.dashboard", "لوحة التحكم", ANY),
    ("main.tasks", "صندوق المهام", STAFF_OR_MGR),
    ("requests.index", "الطلبات", ANY),
    ("employees.index", "الموظفون", _p("employees_view", "payroll", "hiring", "attendance", manager=True)),
    ("attendance.index", "الحضور", _p("attendance", manager=True)),
    ("payroll.index", "الرواتب", _p("payroll")),
    ("admin.delays", "تقرير التأخير", _p("reports")),
    ("admin.import_employees", "استيراد موظفين", _p("hiring")),
    ("employees.import_contract", "قراءة عقد بالذكاء", _p("hiring")),
    ("documents.letters", "الخطابات", ANY),
    ("documents.outbox", "المراسلات", _p("outbox")),
    ("assistant.chat", "المساعد الذكي", ANY),
    ("admin.permissions", "الصلاحيات", lambda u: u.base_role == "admin"),
    ("admin.rules_page", "قواعد الطلبات", lambda u: u.base_role == "admin"),
    ("main.audit", "سجل التدقيق", _p("admin")),
    ("admin.settings_page", "الإعدادات", _p("admin")),
    ("admin.system", "حالة النظام", lambda u: u.can("admin") or u.base_role == "operator"),
    ("main.policy", "السياسات", ANY),
]


# صفحات التفاصيل: زر «رجوع» يودّي للقائمة الأم (أو للوحة التحكم إذا المستخدم ما يحق له يشوف القائمة)
BACK = {
    "requests.detail": ("requests.index", "الطلبات", ANY),
    "requests.new": ("requests.index", "الطلبات", ANY),
    "requests.change": ("requests.index", "الطلبات", ANY),
    "employees.detail": ("employees.index", "الموظفون", _p("employees_view", "payroll", "hiring", "attendance", manager=True)),
    "employees.new": ("employees.index", "الموظفون", ANY),
    "employees.import_contract": ("employees.index", "الموظفون", ANY),
    "payroll.slip": ("payroll.index", "الرواتب", _p("payroll")),
    "documents.letter_view": ("documents.letters", "الخطابات", ANY),
    "documents.email_view": ("documents.outbox", "المراسلات", ANY),
    "documents.compose": ("documents.outbox", "المراسلات", ANY),
    "admin.delegate": ("main.dashboard", "لوحة التحكم", ANY),
    "auth.change_password": ("main.dashboard", "لوحة التحكم", ANY),
    "auth.reauth": ("main.dashboard", "لوحة التحكم", ANY),
}


@login_manager.user_loader
def load_user(uid):
    try:
        user_id, version = uid.split(":")
        user = db.session.get(User, int(user_id))
    except (ValueError, AttributeError):
        return None
    if user and user.active and str(user.session_version) == version:
        return user
    return None


def create_app(config_object=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object or Config)
    Config.validate(app.config)
    os.makedirs(app.instance_path, exist_ok=True)
    if app.config.get("TRUST_PROXY"):
        # خلف Caddy فقط: نأخذ IP الزائر الحقيقي من الترويسة اللي يضيفها البروكسي (مهم لحد المحاولات وسجل التدقيق)
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "سجّل دخولك أولًا."
    login_manager.session_protection = "strong"

    from .routes import admin, assistant, attendance, auth, documents, employees, main, payroll, requests_view
    for mod in (auth, main, employees, requests_view, documents, payroll, assistant, admin, attendance):
        app.register_blueprint(mod.bp)

    from . import cli
    cli.register(app)

    from . import policy
    from .models import ROLES
    from .services.workflow import label as req_label, summary as req_summary
    from .services.rules import describe as rule_text
    app.jinja_env.globals.update(req_label=req_label, req_summary=req_summary, today_date=policy.today, role_names=ROLES,
                                 rule_text=rule_text)

    @app.template_filter("money")
    def money(v):
        return f"{round(v or 0):,}"

    @app.template_filter("money2")
    def money2(v):
        return f"{(v or 0):,.2f}"

    @app.template_filter("d")
    def d(v):
        return v.strftime("%Y/%m/%d") if v else "—"

    @app.template_filter("dt")
    def dt(v):
        return v.strftime("%Y/%m/%d %H:%M") if v else "—"

    @app.context_processor
    def inject():
        nav, badges = [], {}
        if has_request_context() and current_user and current_user.is_authenticated:
            nav = [(ep, lbl) for ep, lbl, rule in NAV if rule(current_user)]
            if current_user.employee and current_user.employee.reports:
                nav.insert(3, ("admin.delegate", "التفويض أثناء الإجازة"))
            from .services import tasks as task_svc
            badges["main.tasks"] = task_svc.visible_query(current_user).filter_by(done=False).count()
            if current_user.can("outbox"):
                from .models import Email
                badges["documents.outbox"] = Email.query.filter_by(status="pending_approval").count()
        support = None
        if has_request_context() and current_user and current_user.is_authenticated and current_user.base_role == "operator":
            support = current_user.support_grant
        back = None
        if has_request_context() and current_user and current_user.is_authenticated and request.endpoint in BACK:
            ep, lbl, rule = BACK[request.endpoint]
            back = (url_for(ep), lbl) if rule(current_user) else (url_for("main.dashboard"), "لوحة التحكم")
        return {"nav": nav, "badges": badges, "company": app.config["COMPANY_NAME"], "support": support, "back": back}

    @app.before_request
    def session_guard():
        """انتهاء الجلسة بعد خمول، وحد أقصى لعمر الجلسة، وإجبار تغيير كلمة المرور المؤقتة."""
        if not current_user.is_authenticated or request.endpoint == "static":
            return None
        from .models import utcnow
        now = utcnow().timestamp()
        idle = app.config["SESSION_IDLE_MINUTES"] * 60
        absolute = app.config["SESSION_MAX_HOURS"] * 3600
        if now - float(session.get("last_seen") or 0) > idle or now - float(session.get("auth_at") or 0) > absolute:
            logout_user()
            session.clear()
            flash("انتهت الجلسة لعدم النشاط. سجّل دخولك من جديد.", "warn")
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
        session["last_seen"] = now
        if current_user.must_change_password and request.endpoint not in ("auth.change_password", "auth.logout"):
            return redirect(url_for("auth.change_password"))
        return None

    @app.before_request
    def operator_guard():
        """المسؤول التقني: بدون إذن يشوف حالة النظام فقط. مع إذن الدعم يقرأ فقط، وكل صفحة يفتحها تُسجل."""
        if not (current_user.is_authenticated and current_user.base_role == "operator"):
            return None
        ep = request.endpoint or ""
        if ep.startswith("auth.") or ep == "static":
            return None
        if request.method != "GET":
            abort(403)
        if current_user.support_grant:
            from .services import audit
            audit.log("اطلاع أثناء الدعم الفني", request.full_path[:300])
            db.session.commit()
            return None
        if ep not in ("admin.system", "main.policy"):
            from flask import redirect, url_for
            return redirect(url_for("admin.system"))
        return None

    @app.after_request
    def security_headers(resp):
        if has_request_context() and current_user and current_user.is_authenticated:
            # بيانات الموظفين ما تنحفظ في كاش المتصفح ولا البروكسي (زر الرجوع بعد الخروج ما يعرضها)
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["Pragma"] = "no-cache"
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' https://fonts.googleapis.com; style-src-attr 'unsafe-inline'; "
            "font-src https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'self'")
        resp.headers.pop("Server", None)
        if app.config.get("SESSION_COOKIE_SECURE"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp

    def err(code, title, text):
        return render_template("errors/error.html", code=code, title=title, text=text), code

    app.register_error_handler(403, lambda e: err(403, "غير مسموح", "هذه الصفحة خارج صلاحياتك."))
    app.register_error_handler(404, lambda e: err(404, "غير موجود", "الصفحة أو السجل المطلوب غير موجود."))
    app.register_error_handler(413, lambda e: err(413, "الملف كبير", "الحد الأقصى لحجم الملف 5 ميجابايت."))
    app.register_error_handler(CSRFError, lambda e: err(400, "انتهت صلاحية النموذج", "حدّث الصفحة وحاول مرة ثانية."))

    if app.config.get("AUTO_CREATE_TABLES"):
        with app.app_context():
            db.create_all()
    return app
