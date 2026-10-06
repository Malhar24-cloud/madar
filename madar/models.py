from datetime import date, datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

from . import policy
from .crypto import EncryptedString

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


ROLES = {
    "admin": "مدير الموارد البشرية",
    "hr": "فريق الموارد البشرية",
    "payroll": "مسؤول الرواتب",
    "recruit": "أخصائي التوظيف",
    "manager": "مدير قسم",
    "employee": "موظف",
    "operator": "المسؤول التقني",
}

# الصلاحيات حسب المهمة. مدير الموارد البشرية يوزعها على الأشخاص من صفحة الصلاحيات.
PERMS = {
    "leaves": "اعتماد الإجازات والاستئذانات والمرضية",
    "payroll": "الرواتب والسلف وحماية الأجور ورؤية الرواتب",
    "changes": "اعتماد التعديلات الوظيفية والمالية (يرى المبالغ)",
    "hiring": "التعيين واستيراد الموظفين والعقود والاستقالات",
    "letters": "إصدار الخطابات وشهادات الخبرة (يظهر فيها الراتب)",
    "attendance": "الحضور والبصمة وقرارات الغياب",
    "employees_view": "عرض ملفات كل الموظفين",
    "outbox": "المراسلات والمسودات",
    "reports": "تقرير التأخير",
    "admin": "الإدارة: الصلاحيات والإعدادات والتدقيق وإنهاء الخدمة",
}
ROLE_PRESETS = {
    "admin": ["admin"],
    "payroll": ["payroll", "attendance", "employees_view", "outbox", "reports", "changes"],
    "recruit": ["hiring", "letters", "leaves", "employees_view", "outbox"],
    "hr": ["leaves", "letters", "employees_view"],
    "manager": [],
    "employee": [],
    "operator": [],
}
STAFF_PERMS = set(PERMS)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, index=True)  # فارغ للعمال الميدانيين بدون بريد
    password_hash = db.Column(db.String(255))
    base_role = db.Column("role", db.String(20), nullable=False, default="employee")
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), unique=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    last_login = db.Column(db.DateTime)
    totp_secret = db.Column(db.String(64))
    totp_last_step = db.Column(db.BigInteger)
    sso_subject = db.Column(db.String(255))
    perms = db.Column(db.JSON)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)

    employee = db.relationship("Employee", back_populates="user", foreign_keys=[employee_id])

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return bool(self.password_hash) and check_password_hash(self.password_hash, raw)

    def get_id(self):
        return f"{self.id}:{self.session_version}"

    @property
    def is_active(self):
        return self.active

    @property
    def support_grant(self):
        if self.base_role != "operator":
            return None
        return SupportGrant.query.filter(SupportGrant.operator_id == self.id, SupportGrant.revoked_at.is_(None),
                                         SupportGrant.expires_at > utcnow()).first()

    @property
    def role(self):
        """الدور الفعلي. المسؤول التقني لا يرى بيانات الشركة إلا أثناء إذن دعم فني ساري منحه مدير الموارد البشرية."""
        if self.base_role == "operator" and self.support_grant:
            return "admin"
        return self.base_role

    @role.setter
    def role(self, value):
        self.base_role = value

    @property
    def effective_perms(self):
        if self.role == "admin":
            return set(PERMS)
        if self.base_role == "operator":
            return set()
        base = self.perms if self.perms is not None else ROLE_PRESETS.get(self.base_role, [])
        # صلاحية «الإدارة» تجي من دور مدير الموارد البشرية فقط، ما تنعطى كصلاحية مفردة
        return (set(base) & set(PERMS)) - {"admin"}

    def can(self, *perms):
        """هل عند المستخدم أي صلاحية من هذي؟"""
        mine = self.effective_perms
        return any(p in mine for p in perms)

    @property
    def is_staff(self):
        return bool(self.effective_perms)

    @property
    def is_manager(self):
        return self.base_role == "manager" or bool(self.employee and self.employee.reports)

    @property
    def role_label(self):
        if self.base_role == "operator" and self.role == "admin":
            return "المسؤول التقني (وضع دعم مؤقت)"
        return ROLES.get(self.role, self.role)

    @property
    def display_name(self):
        if self.employee:
            return self.employee.name
        return self.email or f"مستخدم {self.id}"

    @property
    def login_name(self):
        return self.email or (self.employee.code if self.employee else str(self.id))


class Employee(db.Model):
    __tablename__ = "employees"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True)
    phone = db.Column(db.String(20))
    nationality = db.Column(db.String(60), nullable=False, default="سعودي")
    is_saudi = db.Column(db.Boolean, nullable=False, default=True)
    gosi_new_system = db.Column(db.Boolean, nullable=False, default=False)  # مسجل في التأمينات بعد 2024-07-03
    department = db.Column(db.String(60), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    manager_id = db.Column(db.Integer, db.ForeignKey("employees.id"))
    delegate_id = db.Column(db.Integer, db.ForeignKey("employees.id"))
    delegate_until = db.Column(db.Date)
    basic = db.Column(db.Integer, nullable=False)
    housing = db.Column(db.Integer, nullable=False, default=0)
    transport = db.Column(db.Integer, nullable=False, default=0)
    hire_date = db.Column(db.Date, nullable=False)
    contract_type = db.Column(db.String(10), nullable=False, default="open")
    contract_no = db.Column(db.String(40))
    end_date = db.Column(db.Date)
    probation_days = db.Column(db.Integer, nullable=False, default=policy.PROBATION_DAYS)
    leave_used = db.Column(db.Integer, nullable=False, default=0)
    sick_used = db.Column(db.Integer, nullable=False, default=0)
    leave_carry = db.Column(db.Integer, nullable=False, default=0)  # رصيد مرحّل من السنة السابقة
    leave_year_start = db.Column(db.Date)  # بداية سنة الإجازات الحالية (ذكرى المباشرة)
    iqama_expiry = db.Column(db.Date)
    national_id = db.Column(EncryptedString())
    bank = db.Column(db.String(40))
    iban = db.Column(EncryptedString())
    attendance_id = db.Column(db.String(20))  # رقم الموظف في جهاز البصمة
    clauses = db.Column(db.JSON, default=list)
    status = db.Column(db.String(10), nullable=False, default="active")
    left_date = db.Column(db.Date)
    left_reason = db.Column(db.String(20))
    final_amount = db.Column(db.Float)

    manager = db.relationship("Employee", remote_side=[id], foreign_keys=[manager_id], backref="reports")
    delegate = db.relationship("Employee", remote_side=[id], foreign_keys=[delegate_id])
    user = db.relationship("User", back_populates="employee", uselist=False, foreign_keys="User.employee_id")

    @property
    def gross(self):
        return policy.gross(self)

    @property
    def entitlement(self):
        return policy.entitlement(self)

    @property
    def leave_left(self):
        return policy.leave_left(self)

    @property
    def tenure_years(self):
        return policy.years_between(self.hire_date, policy.today())

    @property
    def days_since_hire(self):
        return (policy.today() - self.hire_date).days

    @property
    def in_probation(self):
        return self.status == "active" and 0 <= self.days_since_hire < self.probation_days

    @property
    def probation_left(self):
        return self.probation_days - self.days_since_hire

    @property
    def days_to_end(self):
        return (self.end_date - policy.today()).days if self.end_date else None

    @property
    def contract_label(self):
        return "محدد المدة" if self.contract_type == "fixed" else "غير محدد المدة"

    @property
    def active_delegate(self):
        if self.delegate and self.delegate_until and self.delegate_until >= policy.today() and self.delegate.status == "active":
            return self.delegate
        return None

    @property
    def initials(self):
        parts = [p for p in self.name.split() if p][:2]
        return " ".join((p[2:] if p.startswith("ال") and len(p) > 2 else p)[0] for p in parts)

    @property
    def first_name(self):
        return self.name.split()[0]


class Request(db.Model):
    __tablename__ = "requests"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False, index=True)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by_ids = db.Column(db.JSON)  # من اعتمد مراحل سابقة، لمنع نفس الشخص يعتمد مرحلتين
    type = db.Column(db.String(20), nullable=False)
    data = db.Column(db.JSON, nullable=False, default=dict)
    stages = db.Column(db.JSON, nullable=False, default=list)
    stage_index = db.Column(db.Integer, nullable=False, default=0)
    stage_since = db.Column(db.DateTime, nullable=False, default=utcnow)
    reminders_sent = db.Column(db.Integer, nullable=False, default=0)
    escalated_to_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    payroll_month = db.Column(db.String(7))
    status = db.Column(db.String(12), nullable=False, default="pending")
    auto = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    closed_at = db.Column(db.DateTime)

    employee = db.relationship("Employee")
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])
    escalated_to = db.relationship("User", foreign_keys=[escalated_to_id])
    events = db.relationship("RequestEvent", backref="request", order_by="RequestEvent.id", cascade="all, delete-orphan")

    @property
    def current_stage(self):
        return self.stages[self.stage_index] if self.status == "pending" and self.stage_index < len(self.stages) else None

    @property
    def hours_waiting(self):
        return (utcnow() - self.stage_since).total_seconds() / 3600 if self.status == "pending" else 0


class RequestEvent(db.Model):
    __tablename__ = "request_events"
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("requests.id"), nullable=False)
    at = db.Column(db.DateTime, nullable=False, default=utcnow)
    actor = db.Column(db.String(160), nullable=False)
    action = db.Column(db.String(200), nullable=False)
    note = db.Column(db.String(500), default="")
    hours = db.Column(db.Float)  # كم ساعة بقي الطلب عند هذه المرحلة قبل القرار


class ActionToken(db.Model):
    """رموز لمرة واحدة. نخزن البصمة فقط، لا الرمز نفسه."""
    __tablename__ = "action_tokens"
    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    purpose = db.Column(db.String(20), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    request_id = db.Column(db.Integer, db.ForeignKey("requests.id"))
    stage_index = db.Column(db.Integer)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)

    user = db.relationship("User")
    request = db.relationship("Request")


class Email(db.Model):
    __tablename__ = "emails"
    id = db.Column(db.Integer, primary_key=True)
    to_addr = db.Column(db.String(255), nullable=False, default="")
    to_name = db.Column(db.String(160), default="")
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    actions = db.Column(db.JSON, default=list)
    kind = db.Column(db.String(10), nullable=False, default="auto")
    sensitive = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(20), nullable=False, default="queued")  # queued|sent|failed|pending_approval|cancelled|no_address
    tag = db.Column(db.String(40), default="")
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    sent_at = db.Column(db.DateTime)
    approved_by = db.Column(db.String(160))
    error = db.Column(db.String(500))

    employee = db.relationship("Employee")


class Letter(db.Model):
    __tablename__ = "letters"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True, nullable=False)
    type = db.Column(db.String(20), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    addressee = db.Column(db.String(200), default="")
    html = db.Column(db.Text, nullable=False)
    request_id = db.Column(db.Integer, db.ForeignKey("requests.id"))
    issued_by = db.Column(db.String(160))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    employee = db.relationship("Employee")


class Loan(db.Model):
    __tablename__ = "loans"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    months = db.Column(db.Integer, nullable=False)
    paid = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    employee = db.relationship("Employee", backref="loans")

    @property
    def installment(self):
        return round(self.amount / self.months, 2)

    @property
    def remaining(self):
        return round(self.amount * (self.months - self.paid) / self.months, 2)

    @property
    def is_active(self):
        return self.paid < self.months


class Asset(db.Model):
    __tablename__ = "assets"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    type = db.Column(db.String(60), nullable=False)
    tag = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="مع الموظف")

    employee = db.relationship("Employee", backref="assets")


class PayrollRun(db.Model):
    __tablename__ = "payroll_runs"
    id = db.Column(db.Integer, primary_key=True)
    month = db.Column(db.String(7), unique=True, nullable=False)
    status = db.Column(db.String(12), nullable=False, default="approved")
    approved_by = db.Column(db.String(160))
    approved_at = db.Column(db.DateTime, default=utcnow)
    total_net = db.Column(db.Float, default=0)
    wps_uploaded_at = db.Column(db.DateTime)
    wps_uploaded_by = db.Column(db.String(160))

    payslips = db.relationship("Payslip", backref="run", cascade="all, delete-orphan")


class Payslip(db.Model):
    __tablename__ = "payslips"
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("payroll_runs.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    basic = db.Column(db.Float, nullable=False)
    housing = db.Column(db.Float, nullable=False)
    transport = db.Column(db.Float, nullable=False)
    proration = db.Column(db.Float, nullable=False, default=1.0)
    gosi = db.Column(db.Float, nullable=False)
    loan = db.Column(db.Float, nullable=False)
    absence = db.Column(db.Float, nullable=False, default=0)
    additions = db.Column(db.Float, nullable=False, default=0)
    deductions = db.Column(db.Float, nullable=False, default=0)
    net = db.Column(db.Float, nullable=False)
    bank = db.Column(db.String(40))
    iban = db.Column(EncryptedString())
    notes = db.Column(db.JSON, default=list)

    employee = db.relationship("Employee")

    @property
    def gross(self):
        return round((self.basic + self.housing + self.transport) * self.proration, 2)


class PayrollAdjustment(db.Model):
    """إضافة أو خصم يدخل مسيرًا محددًا: مكافأة، خصم، أو فروقات بأثر رجعي من تعديل اعتُمد متأخرًا."""
    __tablename__ = "payroll_adjustments"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    month = db.Column(db.String(7), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(300), nullable=False)
    request_id = db.Column(db.Integer, db.ForeignKey("requests.id"))
    applied_run_id = db.Column(db.Integer, db.ForeignKey("payroll_runs.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    employee = db.relationship("Employee")


class ScheduledChange(db.Model):
    """تعديل وظيفي أو مالي معتمد يسري في تاريخ مستقبلي. الفحص اليومي يطبّقه في يومه."""
    __tablename__ = "scheduled_changes"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    request_id = db.Column(db.Integer, db.ForeignKey("requests.id"))
    effective = db.Column(db.Date, nullable=False)
    changes = db.Column(db.JSON, nullable=False)
    applied_at = db.Column(db.DateTime)

    employee = db.relationship("Employee")


class SalaryHistory(db.Model):
    __tablename__ = "salary_history"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False, index=True)
    effective = db.Column(db.Date, nullable=False)
    field = db.Column(db.String(30), nullable=False)
    old = db.Column(db.String(120))
    new = db.Column(db.String(120))
    request_id = db.Column(db.Integer, db.ForeignKey("requests.id"))
    at = db.Column(db.DateTime, nullable=False, default=utcnow)


class AttendanceDay(db.Model):
    __tablename__ = "attendance_days"
    __table_args__ = (db.UniqueConstraint("employee_id", "day"),)
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False, index=True)
    day = db.Column(db.Date, nullable=False, index=True)
    check_in = db.Column(db.Time)
    check_out = db.Column(db.Time)
    late_minutes = db.Column(db.Integer, nullable=False, default=0)
    # present | late | absent (غياب غير مبرر) | excused (مغطى بطلب) | deducted (أُقر خصمه)
    status = db.Column(db.String(12), nullable=False, default="present")
    note = db.Column(db.String(200), default="")

    employee = db.relationship("Employee")


class Task(db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True)
    role = db.Column(db.String(20), nullable=False, default="admin")
    assignee_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    title = db.Column(db.String(200), nullable=False)
    detail = db.Column(db.String(500), default="")
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"))
    link = db.Column(db.String(200))
    due = db.Column(db.Date)
    done = db.Column(db.Boolean, nullable=False, default=False)
    done_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    assignee = db.relationship("User")


class AuditLog(db.Model):
    """سجل تدقيق للإضافة فقط: لا توجد في التطبيق أي وظيفة تعدّله أو تحذفه."""
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    actor = db.Column(db.String(160), nullable=False)
    action = db.Column(db.String(120), nullable=False)
    detail = db.Column(db.String(1000), default="")
    ip = db.Column(db.String(64))
    automated = db.Column(db.Boolean, nullable=False, default=False)
    prev_hash = db.Column(db.String(64))
    hash = db.Column(db.String(64))  # سلسلة بصمات: أي تعديل أو حذف لسطر قديم ينكشف بأمر flask verify-audit


class ChatMessage(db.Model):
    """سجل المحادثة في قاعدة البيانات، لا في كوكي الجلسة، لأن الإجابات قد تحتوي بيانات حساسة."""
    __tablename__ = "chat_messages"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    role = db.Column(db.String(10), nullable=False)
    text = db.Column(db.Text, nullable=False)
    at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Setting(db.Model):
    """إعدادات الشركة القابلة للتعديل من الواجهة."""
    __tablename__ = "settings"
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(500), nullable=False)


class SupportGrant(db.Model):
    """إذن مؤقت للمسؤول التقني بالدخول على بيانات الشركة (Break-glass). يمنحه مدير الموارد البشرية ويُسجّل."""
    __tablename__ = "support_grants"
    id = db.Column(db.Integer, primary_key=True)
    operator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    granted_by = db.Column(db.String(160), nullable=False)
    reason = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime)

    operator = db.relationship("User")


class RateHit(db.Model):
    """عدّاد المحاولات لحماية الدخول والروابط والذكاء الاصطناعي من التخمين والإغراق."""
    __tablename__ = "rate_hits"
    id = db.Column(db.Integer, primary_key=True)
    bucket = db.Column(db.String(120), nullable=False, index=True)
    at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
