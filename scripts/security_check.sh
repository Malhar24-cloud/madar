#!/usr/bin/env sh
# فحص أمني دفاعي قبل أي إصدار: تحليل الكود، ثغرات المكتبات، واختبارات الصلاحيات والمدخلات.
# التشغيل: sh scripts/security_check.sh
set -e
pip install -q -r requirements-dev.txt
echo "== 1) تحليل الكود الثابت (Bandit) =="
bandit -r madar -q
echo "== 2) ثغرات معروفة في المكتبات (pip-audit) =="
pip-audit -r requirements.txt
echo "== 3) الاختبارات الآلية (صلاحيات، مدخلات، سلامة البيانات) =="
python -m pytest -q
echo "تم: كل الفحوص نجحت."
