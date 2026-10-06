FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home madar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/backups /app/instance && chown -R madar /app
USER madar
EXPOSE 8000
# بدون سجل الوصول (access log): الروابط فيها رموز لمرة واحدة ما نبيها تنحفظ في السجلات
CMD ["sh", "-c", "flask --app wsgi db upgrade && gunicorn -w 3 -b 0.0.0.0:8000 --timeout 60 --limit-request-line 4094 wsgi:app"]
