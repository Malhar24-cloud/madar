import os
from dotenv import load_dotenv

load_dotenv()

DEV_SECRET = "dev-only-change-me"  # nosec B105: للتطوير فقط، والتشغيل في الإنتاج يرفض هذي القيمة


def _bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    ENV_NAME = os.environ.get("MADAR_ENV", "development")
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET)
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///madar.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    COMPANY_NAME = os.environ.get("COMPANY_NAME", "شركة الرواسي للمقاولات")
    BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:5000").rstrip("/")

    # البريد: console يطبع الرسائل في الطرفية، smtp يرسل فعليًا، memory للاختبارات
    MAIL_BACKEND = os.environ.get("MAIL_BACKEND", "console")
    MAIL_FROM = os.environ.get("MAIL_FROM", "Madar HR <hr@example.com>")
    MAIL_REDIRECT_TO = os.environ.get("MAIL_REDIRECT_TO", "")
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_STARTTLS = _bool("SMTP_STARTTLS", True)

    # الذكاء الاصطناعي
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    ANTHROPIC_MODEL_FAST = os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001")

    # الدخول الموحد بحسابات الشركة (Microsoft Entra ID أو Google Workspace)
    OIDC_PROVIDER = os.environ.get("OIDC_PROVIDER", "")  # microsoft | google
    OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
    OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
    OIDC_TENANT = os.environ.get("OIDC_TENANT", "")
    OIDC_ALLOWED_DOMAIN = os.environ.get("OIDC_ALLOWED_DOMAIN", "")

    REQUIRE_2FA = _bool("REQUIRE_2FA", True)  # التحقق بخطوتين إجباري للإدارة والرواتب والمسؤول التقني
    AUTO_CREATE_TABLES = _bool("AUTO_CREATE_TABLES", True)  # في الإنتاج: false واستخدم flask db upgrade
    BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")
    BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "14"))
    DAILY_RUN_HOUR = int(os.environ.get("DAILY_RUN_HOUR", "6"))

    APPROVAL_TOKEN_HOURS = int(os.environ.get("APPROVAL_TOKEN_HOURS", "48"))
    SET_PASSWORD_TOKEN_HOURS = int(os.environ.get("SET_PASSWORD_TOKEN_HOURS", "72"))
    LOGIN_MAX_FAILS = 5
    LOGIN_LOCK_MINUTES = 15

    DATA_KEY = os.environ.get("DATA_KEY", "")  # مفتاح تشفير الهوية والآيبان: flask gen-data-key
    TRUST_PROXY = _bool("TRUST_PROXY", False)  # true فقط إذا التطبيق خلف Caddy أو بروكسي موثوق
    SESSION_IDLE_MINUTES = int(os.environ.get("SESSION_IDLE_MINUTES", "30"))
    SESSION_MAX_HOURS = int(os.environ.get("SESSION_MAX_HOURS", "10"))

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    REMEMBER_COOKIE_DURATION = 0
    if SESSION_COOKIE_SECURE:
        SESSION_COOKIE_NAME = "__Host-madar"  # الكوكي مربوط بالنطاق نفسه وHTTPS فقط
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 8

    @staticmethod
    def validate(cfg):
        if cfg.get("ENV_NAME") == "production":
            if cfg.get("SECRET_KEY") in (None, "", DEV_SECRET) or len(cfg["SECRET_KEY"]) < 32:
                raise RuntimeError("SECRET_KEY must be a random value of 32+ characters in production.")
            if not cfg.get("SESSION_COOKIE_SECURE"):
                raise RuntimeError("Set SESSION_COOKIE_SECURE=true in production (HTTPS).")
            if not cfg.get("DATA_KEY"):
                raise RuntimeError("Set DATA_KEY in production (run: flask gen-data-key).")
            if cfg.get("DATA_KEY") and cfg["DATA_KEY"] in cfg["SECRET_KEY"]:
                raise RuntimeError("DATA_KEY must be different from SECRET_KEY.")
            if not str(cfg.get("BASE_URL", "")).startswith("https://"):
                raise RuntimeError("BASE_URL must start with https:// in production.")
            if cfg.get("MAIL_BACKEND") not in ("smtp",):
                raise RuntimeError("MAIL_BACKEND must be smtp in production.")
            if not cfg.get("REQUIRE_2FA"):
                raise RuntimeError("REQUIRE_2FA cannot be disabled in production.")
            if cfg.get("WTF_CSRF_ENABLED") is False:
                raise RuntimeError("CSRF protection cannot be disabled in production.")


class TestConfig(Config):
    TESTING = True
    SESSION_COOKIE_NAME = "session"
    REQUIRE_2FA = False
    AUTO_CREATE_TABLES = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    MAIL_BACKEND = "memory"
    ANTHROPIC_API_KEY = ""
    BASE_URL = "http://test"
