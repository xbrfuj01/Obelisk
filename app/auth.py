import threading
from datetime import datetime, timedelta

from passlib.context import CryptContext
from fastapi import Request
from sqlalchemy.orm import Session

from . import config
from .models import Setting

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class NotAuthenticated(Exception):
    pass


class SiteNotAuthenticated(Exception):
    pass


def get_setting(db: Session, key: str, default=None):
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str):
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        row = Setting(key=key, value=value)
        db.add(row)
    db.commit()


def ensure_admin_credentials(db: Session):
    if not get_setting(db, "admin_username"):
        set_setting(db, "admin_username", config.ADMIN_USERNAME)
    if not get_setting(db, "admin_password_hash"):
        set_setting(db, "admin_password_hash", pwd_context.hash(config.ADMIN_PASSWORD))


def get_admin_username(db: Session) -> str:
    return get_setting(db, "admin_username", config.ADMIN_USERNAME)


def verify_admin_credentials(db: Session, username: str, password: str) -> bool:
    expected_username = get_admin_username(db)
    hash_ = get_setting(db, "admin_password_hash")
    if not hash_ or username != expected_username:
        return False
    return pwd_context.verify(password, hash_)


def get_retention_hours(db: Session) -> int:
    val = get_setting(db, "cleanup_hours")
    return int(val) if val else config.DEFAULT_CLEANUP_HOURS


def require_admin(request: Request):
    if not request.session.get("admin"):
        raise NotAuthenticated()


# ---------------- Site-wide password gate ----------------

def ensure_site_password(db: Session):
    if config.SITE_PASSWORD and not get_setting(db, "site_password_hash"):
        set_setting(db, "site_password_hash", pwd_context.hash(config.SITE_PASSWORD))


def is_site_gate_enabled(db: Session) -> bool:
    return bool(get_setting(db, "site_password_hash"))


def verify_site_password(db: Session, password: str) -> bool:
    hash_ = get_setting(db, "site_password_hash")
    if not hash_:
        return False
    return pwd_context.verify(password, hash_)


def set_site_password(db: Session, password: str):
    set_setting(db, "site_password_hash", pwd_context.hash(password))


def clear_site_password(db: Session):
    set_setting(db, "site_password_hash", "")


def require_site_access(request: Request, db: Session):
    if is_site_gate_enabled(db) and not request.session.get("site_access"):
        raise SiteNotAuthenticated()


# ---------------- Brute-force lockout ----------------

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

_login_lock = threading.Lock()
_login_attempts = {}  # key -> {"count": int, "locked_until": datetime | None}


def check_lockout(key: str):
    """Returns (is_locked, seconds_remaining)."""
    with _login_lock:
        entry = _login_attempts.get(key)
        if not entry or not entry.get("locked_until"):
            return False, 0
        remaining = (entry["locked_until"] - datetime.utcnow()).total_seconds()
        if remaining <= 0:
            _login_attempts.pop(key, None)
            return False, 0
        return True, int(remaining)


def register_failed_attempt(key: str):
    with _login_lock:
        entry = _login_attempts.setdefault(key, {"count": 0, "locked_until": None})
        entry["count"] += 1
        if entry["count"] >= MAX_LOGIN_ATTEMPTS:
            entry["locked_until"] = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)


def register_successful_attempt(key: str):
    with _login_lock:
        _login_attempts.pop(key, None)
