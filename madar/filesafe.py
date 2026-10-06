"""فحص الملفات المرفوعة قبل فتحها: حماية من الملفات المضغوطة الملغومة (Zip bomb) اللي تنفجر في الذاكرة."""
import io
import zipfile

MAX_UNCOMPRESSED = 50 * 1024 * 1024   # 50MB بعد فك الضغط
MAX_RATIO = 100                       # نسبة ضغط غير طبيعية
MAX_ENTRIES = 2000


def check_zip(data):
    """xlsx وdocx ملفات zip من الداخل. نتأكد إن حجمها بعد الفك معقول قبل ما نفتحها."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("الملف تالف أو ليس بالصيغة المطلوبة.")
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ValueError("الملف غير مقبول.")
    total = sum(i.file_size for i in infos)
    if total > MAX_UNCOMPRESSED or (len(data) and total / max(len(data), 1) > MAX_RATIO):
        raise ValueError("الملف غير مقبول (حجمه بعد فك الضغط كبير بشكل غير طبيعي).")
    for i in infos:
        if i.filename.startswith("/") or ".." in i.filename.split("/"):
            raise ValueError("الملف غير مقبول.")
