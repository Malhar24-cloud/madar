"""تشفير البيانات الحساسة داخل قاعدة البيانات (رقم الهوية والآيبان).
لو تسربت نسخة من قاعدة البيانات أو من النسخ الاحتياطية، هذي الحقول تكون غير مقروءة بدون مفتاح DATA_KEY
اللي يُحفظ منفصلًا في متغيرات البيئة."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from flask import current_app, has_app_context
from sqlalchemy.types import String, TypeDecorator

PREFIX = "enc:"


def _fernet():
    cfg = current_app.config
    keys = [k.strip() for k in (cfg.get("DATA_KEY") or "").split(",") if k.strip()]
    if not keys:
        # وضع التطوير فقط: مفتاح مشتق من SECRET_KEY. الإنتاج يرفض التشغيل بدون DATA_KEY.
        keys = [base64.urlsafe_b64encode(hashlib.sha256(("madar-data:" + cfg["SECRET_KEY"]).encode()).digest()).decode()]
    # أكثر من مفتاح مفصولة بفاصلة: الأول للتشفير، والباقي لفك القديم أثناء تدوير المفاتيح
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt(value):
    if value in (None, ""):
        return value
    return PREFIX + _fernet().encrypt(str(value).encode()).decode()


def decrypt(value):
    if not value or not str(value).startswith(PREFIX):
        return value  # قيمة قديمة غير مشفرة: تُقرأ كما هي وتُشفّر عند أول حفظ
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        if has_app_context():
            current_app.logger.error("DATA_KEY لا يفك تشفير بيانات محفوظة. تأكد من المفتاح.")
        return "[تعذّر فك التشفير]"


class EncryptedString(TypeDecorator):
    impl = String(255)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)


def generate_key():
    return Fernet.generate_key().decode()
