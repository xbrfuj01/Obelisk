import os
import shutil
import threading
import time
from datetime import datetime, timedelta

from . import auth, config
from .database import SessionLocal
from .models import Conversion, Download

# The metadata editor keeps no DB history - its temp dirs are meant to live
# only until the user downloads the cleaned file (deleted right after via a
# background task). This is a backstop for ones nobody ever came back for.
METADATA_TEMP_MAX_AGE_HOURS = 1


def run_cleanup_once():
    db = SessionLocal()
    try:
        retention_hours = auth.get_retention_hours(db)
        cutoff = datetime.utcnow() - timedelta(hours=retention_hours)
        # "error" is included alongside "finished": a failed download/conversion
        # still leaves its whole working directory behind (partial fragments,
        # a half-written output file, ...) since nothing else ever removes it -
        # only job.filepath (never set on failure) was swept before, so those
        # never actually got cleaned up.
        for model, subdir in ((Download, None), (Conversion, "converts")):
            expired = (
                db.query(model)
                .filter(model.status.in_(("finished", "error")))
                .filter(model.finished_at.isnot(None))
                .filter(model.finished_at < cutoff)
                .all()
            )
            for job in expired:
                job_dir = os.path.join(config.DOWNLOAD_DIR, subdir, job.id) if subdir else os.path.join(config.DOWNLOAD_DIR, job.id)
                shutil.rmtree(job_dir, ignore_errors=True)
                if job.status == "finished":
                    job.filepath = None
                    job.status = "expired"
        db.commit()
    finally:
        db.close()
    _cleanup_metadata_temp_dirs()


def _cleanup_metadata_temp_dirs():
    base = os.path.join(config.DOWNLOAD_DIR, "metadata")
    try:
        entries = os.listdir(base)
    except OSError:
        return
    cutoff = time.time() - METADATA_TEMP_MAX_AGE_HOURS * 3600
    for name in entries:
        path = os.path.join(base, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


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
