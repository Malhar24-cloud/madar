"""مُشغّل المهام الدورية بدون cron: كل ساعة تذكير وتصعيد، ومرة يوميًا الفحص اليومي والنسخ الاحتياطي.
التشغيل: python -m madar.scheduler (في Docker يشتغل كخدمة مستقلة)."""
import logging
import time
from datetime import datetime

from . import create_app
from .models import db
from .services.automation import daily_sweep, hourly_sweep
from .services.exporter import backup

log = logging.getLogger("madar.scheduler")


def run_once(app, do_daily):
    with app.app_context():
        try:
            done = hourly_sweep()
            if do_daily:
                done += daily_sweep()
            db.session.commit()
            if do_daily:
                path = backup()
                if path:
                    log.info("backup: %s", path)
            log.info("%s actions", len(done))
        except Exception:
            db.session.rollback()
            log.exception("scheduler run failed")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app()
    last_daily = None
    while True:
        now = datetime.now()
        do_daily = now.hour >= app.config["DAILY_RUN_HOUR"] and last_daily != now.date()
        run_once(app, do_daily)
        if do_daily:
            last_daily = now.date()
        time.sleep(3600 - now.minute * 60 - now.second + 30)


if __name__ == "__main__":
    main()
