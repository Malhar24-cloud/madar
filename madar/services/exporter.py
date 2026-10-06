"""تصدير كامل لبيانات الشركة (خطة الخروج) ونسخ احتياطي لقاعدة البيانات."""
import io
import os
import shutil
import sqlite3
from datetime import datetime

from flask import current_app

from ..security import safe_cell
from ..models import (AttendanceDay, AuditLog, Employee, Letter, Loan, PayrollAdjustment, Payslip, Request, SalaryHistory, db)


def export_xlsx():
    from openpyxl import Workbook
    wb = Workbook()
    sheets = [
        ("employees", Employee, ["code", "name", "email", "phone", "national_id", "nationality", "department", "title",
                                 "manager_id", "basic", "housing", "transport", "hire_date", "contract_type", "end_date",
                                 "leave_used", "sick_used", "iqama_expiry", "bank", "iban", "status", "left_date"]),
        ("requests", Request, ["id", "employee_id", "type", "data", "status", "auto", "created_at", "closed_at", "payroll_month"]),
        ("payslips", Payslip, ["id", "run_id", "employee_id", "basic", "housing", "transport", "proration", "gosi", "loan",
                               "absence", "additions", "deductions", "net"]),
        ("loans", Loan, ["id", "employee_id", "amount", "months", "paid"]),
        ("adjustments", PayrollAdjustment, ["id", "employee_id", "month", "amount", "reason", "applied_run_id"]),
        ("salary_history", SalaryHistory, ["employee_id", "effective", "field", "old", "new", "request_id"]),
        ("attendance", AttendanceDay, ["employee_id", "day", "check_in", "check_out", "late_minutes", "status"]),
        ("letters", Letter, ["number", "type", "employee_id", "addressee", "created_at"]),
        ("audit_log", AuditLog, ["at", "actor", "action", "detail", "ip", "automated"]),
    ]
    first = True
    for name, model, cols in sheets:
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = name
        ws.append(cols)
        for obj in model.query.all():
            row = []
            for c in cols:
                v = getattr(obj, c)
                row.append(safe_cell(str(v) if isinstance(v, (dict, list)) else v))
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def backup():
    """نسخة احتياطية لقاعدة SQLite مع الاحتفاظ بآخر N نسخة. لـ PostgreSQL استخدم pg_dump (موضح في README)."""
    uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
    bdir = current_app.config["BACKUP_DIR"]
    os.makedirs(bdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if uri.startswith("sqlite:///") and ":memory:" not in uri:
        src = db.engine.url.database
        dest = os.path.join(bdir, f"madar-{stamp}.db")
        with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
            s.backup(d)
    else:
        return None
    files = sorted(f for f in os.listdir(bdir) if f.startswith("madar-"))
    for old in files[:-current_app.config["BACKUP_KEEP"]]:
        os.remove(os.path.join(bdir, old))
    return dest


def restore(path):
    src = db.engine.url.database
    db.session.remove()
    db.engine.dispose()
    shutil.copyfile(path, src)
