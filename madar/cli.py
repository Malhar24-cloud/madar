import secrets
from datetime import date, datetime, time, timedelta

import click

from . import policy
from .models import Asset, Employee, Loan, User, db

DEMO_PASSWORD = "Madar@2026!"  # nosec B105: بيانات تجريبية فقط، وأمر seed مرفوض في الإنتاج

# الاسم، الجنسية، القسم، المسمى، الأساسي، أيام منذ التعيين، محدد المدة؟، ينتهي بعد، إجازات مستخدمة، انتهاء الإقامة بعد، البريد
SEED = [
    ("منيرة السهلي", "سعودية", "الإدارة العليا", "المديرة التنفيذية", 28000, 3120, False, None, 9, None, "m.alsahli"),
    ("فيصل العتيبي", "سعودي", "تقنية المعلومات", "رئيس قسم تقنية المعلومات", 15000, 1460, False, None, 14, None, "f.alotaibi"),
    ("سارة الزهراني", "سعودية", "تقنية المعلومات", "محللة أمن سيبراني", 14000, 610, True, 64, 6, None, "s.alzahrani"),
    ("راجيش كومار", "هندي", "تقنية المعلومات", "مطور أنظمة", 9000, 980, True, 41, 19, 38, "r.kumar"),
    ("نورة القحطاني", "سعودية", "الموارد البشرية", "أخصائية توظيف", 9500, 1210, False, None, 4, None, "n.alqahtani"),
    ("هيفاء العنزي", "سعودية", "الموارد البشرية", "مسؤولة رواتب", 8800, 2240, False, None, 25, None, "h.alanazi"),
    ("عبدالله الشهري", "سعودي", "المالية", "مدير المالية", 16000, 2020, False, None, 10, None, "a.alshehri"),
    ("لمى الحربي", "سعودية", "المالية", "محاسبة", 8000, 340, True, 25, 3, None, "l.alharbi"),
    ("يوسف الأنصاري", "أردني", "المالية", "مدقق داخلي", 12500, 1830, True, 210, 20, 24, "y.alansari"),
    ("ريم الدوسري", "سعودية", "المبيعات", "مديرة المبيعات", 16000, 2600, False, None, 12, None, "r.aldosari"),
    ("تركي السبيعي", "سعودي", "المبيعات", "مندوب مبيعات", 6500, 38, True, 327, 0, None, "t.alsubaie"),
    ("جود البقمي", "سعودية", "المبيعات", "أخصائية تسويق", 7500, 79, False, None, 0, None, "j.albuqami"),
    ("بندر الرشيدي", "سعودي", "العمليات", "مدير العمليات", 19000, 4150, False, None, 18, None, "b.alrashidi"),
    ("خالد المطيري", "سعودي", "العمليات", "مهندس مدني", 13000, 1100, True, 88, 8, None, "k.almutairi"),
    ("محمد الغامدي", "سعودي", "العمليات", "مشرف مواقع", 8500, 760, False, None, 9, None, "m.alghamdi"),
    ("أحمد حسن", "مصري", "العمليات", "مراقب جودة", 7000, 1500, True, 150, 15, 140, "a.hassan"),
    ("عمر عبدالرحمن", "سوداني", "العمليات", "مسّاح", 6800, 420, True, 17, 21, 300, "o.abdelrahman"),
    ("سلطان الشمري", "سعودي", "العمليات", "مشغّل معدات", 5000, 2900, False, None, 30, None, "s.alshammari"),
    ("عبدالرحمن الفيفي", "سعودي", "الموارد البشرية", "مدير الموارد البشرية", 17000, 1900, False, None, 7, None, "a.alfaifi"),
]
NO_EMAIL = {17}  # سلطان الشمري: عامل ميداني بدون بريد، يدخل برقمه الوظيفي
HEADS = {"الإدارة العليا": 0, "تقنية المعلومات": 1, "الموارد البشرية": 18, "المالية": 6, "المبيعات": 9, "العمليات": 12}
ROLE_BY_INDEX = {18: "admin", 5: "payroll", 4: "recruit", 0: "manager", 1: "manager", 6: "manager", 9: "manager", 12: "manager"}
BANKS = [("الراجحي", "80"), ("الأهلي", "10"), ("الرياض", "20"), ("الإنماء", "05")]


def _iban(bank_code, rnd):
    """آيبان تجريبي صحيح الصيغة ورقم التحقق (ISO 13616)، عشان يمر من نفس الفحص اللي يمر منه الحقيقي."""
    bban = bank_code + "".join(str(rnd.randint(0, 9)) for _ in range(18))
    check = 98 - int(bban + "2810" + "00") % 97
    return f"SA{check:02d}{bban}"


def seed_data(domain="alrawasi.example", with_requests=True):
    from .services import workflow
    rnd = secrets.SystemRandom()
    today = policy.today()
    emps = []
    for i, r in enumerate(SEED):
        name, nat, dept, title, basic, ago, fixed, end_in, used, iqama_in, mail = r
        saudi = nat.startswith("سعودي")
        bank = BANKS[i % 4]
        e = Employee(code=f"EMP-{1001 + i}", name=name, email=None if i in NO_EMAIL else f"{mail}@{domain}",
                     phone=f"05{rnd.randint(10000000, 99999999)}", attendance_id=str(101 + i),
                     gosi_new_system=saudi and ago < (today - date(2024, 7, 3)).days, nationality=nat, is_saudi=saudi,
                     department=dept, title=title, basic=basic, housing=round(basic * policy.HOUSING_PCT),
                     transport=round(basic * policy.TRANSPORT_PCT), hire_date=today - timedelta(days=ago),
                     contract_type="fixed" if fixed else "open", end_date=today + timedelta(days=end_in) if end_in else None,
                     leave_used=used, iqama_expiry=today + timedelta(days=iqama_in) if iqama_in else None,
                     national_id=("1" if saudi else "2") + "".join(str(rnd.randint(0, 9)) for _ in range(9)),
                     bank=bank[0], iban=_iban(bank[1], rnd),
                     clauses=["بند سرية المعلومات"] if i % 4 == 0 else [])
        e.contract_no = f"CT-{e.hire_date.year}-{1001 + i}"
        db.session.add(e)
        emps.append(e)
    db.session.flush()
    for i, e in enumerate(emps):
        head = emps[HEADS[e.department]]
        if head is e:
            e.manager_id = emps[0].id if i != 0 else None
        else:
            e.manager_id = head.id
        u = User(email=e.email, role=ROLE_BY_INDEX.get(i, "employee"), employee_id=e.id)
        u.set_password(DEMO_PASSWORD)
        db.session.add(u)
        db.session.add(Asset(employee_id=e.id, type="لابتوب", tag=f"LT-{1001 + i}"))
        if "مدير" in e.title or "المديرة" in e.title:
            db.session.add(Asset(employee_id=e.id, type="جوال عمل", tag=f"MB-{1001 + i}"))
    op = User(email=f"it.support@{domain}", role="operator")
    op.set_password(DEMO_PASSWORD)
    db.session.add(op)
    db.session.add(Loan(employee_id=emps[17].id, amount=6000, months=6, paid=2))
    db.session.add(Loan(employee_id=emps[15].id, amount=3500, months=5, paid=4))
    db.session.flush()
    if with_requests:
        workflow.submit(emps[14], "leave", {"days": 5, "from": (today + timedelta(days=10)).isoformat(), "reason": "سفر عائلي"}, emps[14].name)
        workflow.submit(emps[13], "loan", {"amount": 8000, "months": 4, "reason": "ظرف عائلي"}, emps[13].name)
        workflow.submit(emps[15], "exp_cert", {}, emps[15].name)
        workflow.submit(emps[3], "salary_cert", {"to": "بنك الراجحي"}, emps[3].name)
        _seed_attendance(emps)
    db.session.commit()
    return emps


def _seed_attendance(emps):
    """بصمات تجريبية لموظفي العمليات آخر 3 أسابيع: بعض التأخير ويوم غياب بدون طلب."""
    from .services import attendance
    rnd = secrets.SystemRandom()
    today = policy.today()
    punches = []
    ops = [e for e in emps if e.department == "العمليات"]
    absent_back = next(b for b in range(4, 12) if (today - timedelta(days=b)).weekday() not in (4, 5))
    for back in range(21, 0, -1):
        day = today - timedelta(days=back)
        if day.weekday() in (4, 5):
            continue
        for e in ops:
            if e.code == "EMP-1017" and back == absent_back:
                continue  # غياب بدون طلب
            late = rnd.choice([0, 0, 0, 0, 5, 25, 40]) if e.code in ("EMP-1017", "EMP-1018", "EMP-1015") else rnd.choice([0, 0, 0, 3])
            cin = datetime.combine(day, time(7, 50)) + timedelta(minutes=rnd.randint(0, 12) + late)
            punches.append((e.attendance_id, cin))
            punches.append((e.attendance_id, cin + timedelta(hours=8, minutes=rnd.randint(0, 40))))
    attendance.import_punches(punches)


def register(app):
    @app.cli.command("seed")
    @click.option("--reset", is_flag=True, help="احذف كل البيانات وابدأ من جديد")
    def seed_cmd(reset):
        """تعبئة بيانات تجريبية (19 موظف وهمي + بصمات + طلبات)."""
        if app.config.get("ENV_NAME") == "production":
            raise click.ClickException("ممنوع تعبئة بيانات تجريبية في الإنتاج (كلمة مرورها معروفة).")
        if reset:
            db.drop_all()
            db.create_all()
        elif Employee.query.first():
            click.echo("البيانات موجودة. استخدم --reset لإعادة التعبئة.")
            return
        seed_data()
        click.echo(f"تمت التعبئة. كلمة المرور لكل الحسابات التجريبية: {DEMO_PASSWORD}")
        for u in User.query.filter(User.base_role != "employee").order_by(User.base_role):
            click.echo(f"  {u.role_label:<22} {u.email}")
        click.echo("  موظف (مثال)              m.alghamdi@alrawasi.example")
        click.echo("  عامل بدون بريد            EMP-1018 (يدخل بالرقم الوظيفي)")

    @app.cli.command("hourly")
    def hourly_cmd():
        """تذكير الموافقين المتأخرين وتصعيد الطلبات."""
        from .security import cleanup_rate_hits
        from .services.automation import hourly_sweep
        done = hourly_sweep()
        cleanup_rate_hits()
        db.session.commit()
        click.echo(f"نُفّذ {len(done)} إجراء")

    @app.cli.command("backup")
    def backup_cmd():
        """نسخة احتياطية لقاعدة البيانات (SQLite). لـ PostgreSQL استخدم pg_dump."""
        from .services.exporter import backup
        path = backup()
        click.echo(f"النسخة: {path}" if path else "قاعدة البيانات ليست SQLite. استخدم: pg_dump \"$DATABASE_URL\" > backup.sql")

    @app.cli.command("export")
    @click.argument("path", default="madar-export.xlsx")
    def export_cmd(path):
        """تصدير كامل لبيانات الشركة إلى Excel (خطة الخروج)."""
        from .services.exporter import export_xlsx
        with open(path, "wb") as f:
            f.write(export_xlsx())
        click.echo(f"صُدّرت البيانات إلى {path}")

    @app.cli.command("create-operator")
    @click.argument("email")
    def create_operator(email):
        """حساب المسؤول التقني: يشوف حالة النظام فقط، ولا يطلع على بيانات الشركة إلا بإذن مؤقت."""
        pw = secrets.token_urlsafe(12)
        u = User.query.filter_by(email=email.lower()).first() or User(email=email.lower())
        u.role, u.active = "operator", True
        u.set_password(pw)
        u.must_change_password = True  # كلمة المرور المؤقتة تُغيَّر أول دخول
        u.session_version = (u.session_version or 1) + 1
        db.session.add(u)
        db.session.commit()
        click.echo(f"الحساب جاهز: {email}\nكلمة المرور المؤقتة: {pw}")

    @app.cli.command("daily")
    def daily_cmd():
        """الفحص اليومي: العقود والتجربة والإقامات والمسير."""
        from .services.automation import daily_sweep
        done = daily_sweep()
        db.session.commit()
        click.echo(f"نُفّذ {len(done)} إجراء")
        for d in done:
            click.echo(f"  - {d}")

    @app.cli.command("create-admin")
    @click.argument("email")
    def create_admin(email):
        """إنشاء حساب مدير موارد بشرية وطباعة كلمة مرور مؤقتة."""
        pw = secrets.token_urlsafe(12)
        u = User.query.filter_by(email=email.lower()).first() or User(email=email.lower())
        u.role, u.active = "admin", True
        u.set_password(pw)
        u.must_change_password = True  # كلمة المرور المؤقتة تُغيَّر أول دخول
        u.session_version = (u.session_version or 1) + 1
        db.session.add(u)
        db.session.commit()
        click.echo(f"الحساب جاهز: {email}\nكلمة المرور المؤقتة: {pw}")


    @app.cli.command("gen-data-key")
    def gen_data_key():
        """يولّد مفتاح تشفير لرقم الهوية والآيبان. احفظه في DATA_KEY وفي مكان آمن منفصل عن النسخ الاحتياطية."""
        from .crypto import generate_key
        click.echo(generate_key())

    @app.cli.command("encrypt-existing")
    def encrypt_existing():
        """يشفّر البيانات القديمة غير المشفرة، ويعيد التشفير بالمفتاح الأول بعد تدوير DATA_KEY."""
        from .models import Payslip
        n = 0
        for model, cols in ((Employee, ("national_id", "iban")), (Payslip, ("iban",))):
            for obj in model.query.all():
                for c in cols:
                    v = getattr(obj, c)
                    if v:
                        setattr(obj, c, None)
                        db.session.flush()
                        setattr(obj, c, v)
                        n += 1
        db.session.commit()
        click.echo(f"أُعيد تشفير {n} قيمة.")

    @app.cli.command("verify-audit")
    def verify_audit():
        """يتحقق إن سجل التدقيق ما انعدل ولا انحذف منه شيء (سلسلة البصمات)."""
        from .services.audit import verify_chain
        ok, bad = verify_chain()
        if ok:
            click.echo("سجل التدقيق سليم.")
        else:
            raise click.ClickException(f"سجل التدقيق فيه تلاعب أو حذف عند السطر رقم {bad}.")
