import secrets
import threading
from datetime import datetime, timedelta

from passlib.context import CryptContext
from fastapi import Request
from sqlalchemy.orm import Session

from . import config
from .models import Setting, User

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
        set_setting(db, "admin_username", config.DEFAULT_ADMIN_USERNAME)
    if not get_setting(db, "admin_password_hash"):
        set_setting(db, "admin_password_hash", pwd_context.hash(config.DEFAULT_ADMIN_PASSWORD))


def get_admin_username(db: Session) -> str:
    return get_setting(db, "admin_username", config.DEFAULT_ADMIN_USERNAME)


def ensure_secret_key(db: Session):
    if not get_setting(db, "secret_key"):
        set_setting(db, "secret_key", secrets.token_hex(32))


def get_secret_key(db: Session) -> str:
    return get_setting(db, "secret_key")


def verify_admin_credentials(db: Session, username: str, password: str) -> bool:
    expected_username = get_admin_username(db)
    hash_ = get_setting(db, "admin_password_hash")
    if not hash_ or username != expected_username:
        return False
    return pwd_context.verify(password, hash_)


def get_retention_hours(db: Session) -> int:
    val = get_setting(db, "cleanup_hours")
    return int(val) if val else config.DEFAULT_CLEANUP_HOURS


def get_cleanup_interval_minutes(db: Session) -> int:
    val = get_setting(db, "cleanup_interval_minutes")
    return int(val) if val else config.DEFAULT_CLEANUP_INTERVAL_MINUTES


def get_max_concurrent_downloads(db: Session) -> int:
    val = get_setting(db, "max_concurrent_downloads")
    return int(val) if val else config.DEFAULT_MAX_CONCURRENT_DOWNLOADS


def get_session_max_age_days(db: Session) -> int:
    val = get_setting(db, "session_max_age_days")
    return int(val) if val else config.DEFAULT_SESSION_MAX_AGE_DAYS


def get_proxy_url(db: Session) -> str:
    return get_setting(db, "proxy_url", "") or ""


def get_proxy_domains(db: Session) -> list:
    raw = get_setting(db, "proxy_domains", config.DEFAULT_PROXY_DOMAINS) or ""
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def require_admin(request: Request):
    if not request.session.get("admin"):
        raise NotAuthenticated()


# ---------------- Site-wide login gate (multi-user) ----------------
# Disabled until an admin creates at least one user from the admin panel —
# there is no env var seed for it (nothing to configure on deploy).

def is_site_gate_enabled(db: Session) -> bool:
    return db.query(User).count() > 0


def list_users(db: Session):
    return db.query(User).order_by(User.created_at).all()


def username_exists(db: Session, username: str) -> bool:
    return db.query(User).filter(User.username == username).first() is not None


def create_user(db: Session, username: str, password: str) -> User:
    user = User(username=username, password_hash=pwd_context.hash(password))
    db.add(user)
    db.commit()
    return user


def delete_user(db: Session, user_id: str):
    user = db.get(User, user_id)
    if user:
        db.delete(user)
        db.commit()


def verify_site_credentials(db: Session, username: str, password: str) -> bool:
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return False
    return pwd_context.verify(password, user.password_hash)


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


# ---------------- Download rate limiting ----------------

DOWNLOAD_RATE_LIMIT = 20
DOWNLOAD_RATE_WINDOW_MINUTES = 10

_download_lock = threading.Lock()
_download_windows = {}  # key -> {"count": int, "window_started": datetime}


def check_download_rate_limit(key: str) -> bool:
    """Returns True if this key is allowed to submit another download now."""
    with _download_lock:
        entry = _download_windows.get(key)
        now = datetime.utcnow()
        if not entry or now - entry["window_started"] > timedelta(minutes=DOWNLOAD_RATE_WINDOW_MINUTES):
            _download_windows[key] = {"count": 1, "window_started": now}
            return True
        if entry["count"] >= DOWNLOAD_RATE_LIMIT:
            return False
        entry["count"] += 1
        return True
