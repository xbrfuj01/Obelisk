import os
import threading
import time
from datetime import datetime, timedelta

from . import auth
from .database import SessionLocal
from .models import Download


def run_cleanup_once():
    db = SessionLocal()
    try:
        retention_hours = auth.get_retention_hours(db)
        cutoff = datetime.utcnow() - timedelta(hours=retention_hours)
        expired = (
            db.query(Download)
            .filter(Download.status == "finished")
            .filter(Download.finished_at.isnot(None))
            .filter(Download.finished_at < cutoff)
            .all()
        )
        for job in expired:
            if job.filepath and os.path.exists(job.filepath):
                try:
                    os.remove(job.filepath)
                    parent = os.path.dirname(job.filepath)
                    if os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                except OSError:
                    pass
            job.filepath = None
            job.status = "expired"
        db.commit()
    finally:
        db.close()


def _loop():
    while True:
        db = SessionLocal()
        try:
            interval_minutes = auth.get_cleanup_interval_minutes(db)
        finally:
            db.close()
        try:
            run_cleanup_once()
        except Exception:
            pass
        time.sleep(interval_minutes * 60)


def start_cleanup_thread():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
