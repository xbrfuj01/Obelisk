import os
import uuid

from fastapi import FastAPI, Request, Response, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import init_db, SessionLocal
from .models import Download
from . import auth
from .downloader import submit_job, _source_from_url, probe_qualities, is_url_allowed, clear_ytdlp_cache
from .cleanup import start_cleanup_thread, run_cleanup_once

BASE_DIR = os.path.dirname(__file__)

# The session cookie needs a secret key and max_age before the app object
# even exists, so the DB is bootstrapped here rather than in a startup
# event — that also means the app never needs an env var for any of this.
init_db()
_bootstrap_db = SessionLocal()
try:
    auth.ensure_admin_credentials(_bootstrap_db)
    auth.ensure_secret_key(_bootstrap_db)
    _secret_key = auth.get_secret_key(_bootstrap_db)
    _session_max_age_days = auth.get_session_max_age_days(_bootstrap_db)
finally:
    _bootstrap_db.close()

app = FastAPI(title="Obelisk")
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key,
    session_cookie="vd_session",
    max_age=_session_max_age_days * 86400,
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.exception_handler(auth.NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: auth.NotAuthenticated):
    return RedirectResponse("/admin/login", status_code=303)


@app.exception_handler(auth.SiteNotAuthenticated)
async def site_not_authenticated_handler(request: Request, exc: auth.SiteNotAuthenticated):
    return RedirectResponse("/site-login", status_code=303)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


CLIENT_ID_COOKIE = "client_id"
CLIENT_ID_MAX_AGE = 60 * 60 * 24 * 400  # ~400 days


def get_client_id(request: Request, response: Response) -> str:
    client_id = request.cookies.get(CLIENT_ID_COOKIE)
    if not client_id:
        client_id = uuid.uuid4().hex
        response.set_cookie(
            CLIENT_ID_COOKIE, client_id, max_age=CLIENT_ID_MAX_AGE, httponly=True, samesite="lax"
        )
    return client_id


def require_site_access_page(request: Request, db: Session = Depends(get_db)):
    auth.require_site_access(request, db)


def require_site_access_api(request: Request, db: Session = Depends(get_db)):
    if auth.is_site_gate_enabled(db) and not request.session.get("site_access"):
        raise HTTPException(status_code=401, detail="Потрібен пароль сайту")


@app.on_event("startup")
def on_startup():
    start_cleanup_thread()


# ---------------- Public ----------------

@app.get("/", response_class=HTMLResponse)
def hub(request: Request, _=Depends(require_site_access_page)):
    return templates.TemplateResponse("hub.html", {"request": request})


@app.get("/downloader", response_class=HTMLResponse)
def downloader_page(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    client_id = request.cookies.get(CLIENT_ID_COOKIE)
    is_new_client = not client_id
    if is_new_client:
        client_id = uuid.uuid4().hex

    recent = (
        db.query(Download)
        .filter(Download.client_id == client_id)
        .order_by(Download.created_at.desc())
        .limit(20)
        .all()
    )
    resp = templates.TemplateResponse("downloader.html", {"request": request, "recent": recent})
    if is_new_client:
        resp.set_cookie(
            CLIENT_ID_COOKIE, client_id, max_age=CLIENT_ID_MAX_AGE, httponly=True, samesite="lax"
        )
    return resp


@app.post("/api/download")
def create_download(
    request: Request,
    response: Response,
    url: str = Form(...),
    mode: str = Form("video"),
    quality: str = Form("best"),
    container: str = Form("mp4"),
    subtitle_lang: str = Form(""),
    premiere_compat: bool = Form(False),
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    client_id = get_client_id(request, response)
    ip = request.client.host if request.client else "unknown"

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "Некоректне посилання"}, status_code=400)
    if not is_url_allowed(url, db):
        return JSONResponse({"error": "Це посилання вказує на заборонену адресу"}, status_code=400)
    if not auth.check_download_rate_limit(f"dl:{ip}"):
        return JSONResponse(
            {"error": "Забагато завантажень поспіль. Спробуйте пізніше."}, status_code=429
        )
    if mode not in ("video", "video_only", "audio"):
        mode = "video"

    job = Download(
        url=url,
        source=_source_from_url(url),
        mode=mode,
        quality=quality,
        container=container,
        subtitle_lang=subtitle_lang.strip() or None,
        premiere_compat=1 if premiere_compat else 0,
        status="queued",
        client_ip=request.client.host if request.client else None,
        client_id=client_id,
        username=request.session.get("site_username"),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    submit_job(job.id)
    return {"id": job.id}


@app.get("/api/status/{job_id}")
def job_status(job_id: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    job = db.get(Download, job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "title": job.title,
        "error": job.error_message,
        "filesize": job.filesize,
    }


@app.get("/api/formats")
def get_formats(url: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "Некоректне посилання"}, status_code=400)
    if not is_url_allowed(url, db):
        return JSONResponse({"error": "Це посилання вказує на заборонену адресу"}, status_code=400)
    try:
        return probe_qualities(url, db)
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=400)


@app.get("/api/recent")
def recent_jobs(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    client_id = get_client_id(request, response)
    rows = (
        db.query(Download)
        .filter(Download.client_id == client_id)
        .order_by(Download.created_at.desc())
        .limit(20)
        .all()
    )
    return [
        {
            "id": r.id,
            "title": r.title or r.url,
            "url": r.url,
            "status": r.status,
            "progress": r.progress,
            "source": r.source,
            "mode": r.mode,
            "filesize": r.filesize,
        }
        for r in rows
    ]


@app.get("/api/file/{job_id}")
def download_file(job_id: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    job = db.get(Download, job_id)
    if not job or job.status != "finished" or not job.filepath or not os.path.exists(job.filepath):
        return JSONResponse({"error": "Файл недоступний"}, status_code=404)
    filename = os.path.basename(job.filepath)
    return FileResponse(job.filepath, filename=filename)


# ---------------- Site gate ----------------

@app.get("/site-login", response_class=HTMLResponse)
def site_login_form(request: Request, db: Session = Depends(get_db)):
    if not auth.is_site_gate_enabled(db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("site_login.html", {"request": request, "error": None})


@app.post("/site-login")
def site_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    key = f"site:{ip}"
    locked, remaining = auth.check_lockout(key)
    if locked:
        minutes = max(1, remaining // 60)
        return templates.TemplateResponse(
            "site_login.html",
            {"request": request, "error": f"Забагато спроб. Спробуйте ще раз через {minutes} хв."},
            status_code=429,
        )
    if auth.verify_site_credentials(db, username, password):
        auth.register_successful_attempt(key)
        request.session["site_access"] = True
        request.session["site_username"] = username
        return RedirectResponse("/", status_code=303)
    auth.register_failed_attempt(key)
    return templates.TemplateResponse(
        "site_login.html", {"request": request, "error": "Невірний логін або пароль"}, status_code=401
    )


@app.get("/site-logout")
def site_logout(request: Request):
    request.session.pop("site_access", None)
    request.session.pop("site_username", None)
    return RedirectResponse("/site-login", status_code=303)


# ---------------- Admin ----------------

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": None})


@app.post("/admin/login")
def admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    key = f"admin:{ip}"
    locked, remaining = auth.check_lockout(key)
    if locked:
        minutes = max(1, remaining // 60)
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": f"Забагато спроб. Спробуйте ще раз через {minutes} хв."},
            status_code=429,
        )
    if auth.verify_admin_credentials(db, username, password):
        auth.register_successful_attempt(key)
        request.session["admin"] = True
        return RedirectResponse("/admin", status_code=303)
    auth.register_failed_attempt(key)
    return templates.TemplateResponse(
        "admin_login.html", {"request": request, "error": "Невірний логін або пароль"}, status_code=401
    )


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    total = db.query(func.count(Download.id)).scalar()
    finished = db.query(func.count(Download.id)).filter(Download.status == "finished").scalar()
    errors = db.query(func.count(Download.id)).filter(Download.status == "error").scalar()
    total_size = (
        db.query(func.coalesce(func.sum(Download.filesize), 0))
        .filter(Download.status == "finished")
        .scalar()
    )

    by_source = (
        db.query(Download.source, func.count(Download.id))
        .group_by(Download.source)
        .order_by(func.count(Download.id).desc())
        .limit(10)
        .all()
    )

    history = db.query(Download).order_by(Download.created_at.desc()).limit(100).all()
    retention_hours = auth.get_retention_hours(db)
    admin_username = auth.get_admin_username(db)
    site_gate_enabled = auth.is_site_gate_enabled(db)
    users = auth.list_users(db)
    cleanup_interval_minutes = auth.get_cleanup_interval_minutes(db)
    max_concurrent_downloads = auth.get_max_concurrent_downloads(db)
    session_max_age_days = auth.get_session_max_age_days(db)
    proxy_url = auth.get_proxy_url(db)
    proxy_domains = ",".join(auth.get_proxy_domains(db))

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "site_gate_enabled": site_gate_enabled,
            "users": users,
            "total": total,
            "finished": finished,
            "errors": errors,
            "total_size": total_size,
            "by_source": by_source,
            "history": history,
            "retention_hours": retention_hours,
            "admin_username": admin_username,
            "cleanup_interval_minutes": cleanup_interval_minutes,
            "max_concurrent_downloads": max_concurrent_downloads,
            "session_max_age_days": session_max_age_days,
            "proxy_url": proxy_url,
            "proxy_domains": proxy_domains,
        },
    )


@app.post("/admin/delete/{job_id}")
def admin_delete(job_id: str, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    job = db.get(Download, job_id)
    if job:
        if job.filepath and os.path.exists(job.filepath):
            try:
                os.remove(job.filepath)
                parent = os.path.dirname(job.filepath)
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass
        db.delete(job)
        db.commit()
    return RedirectResponse("/admin?tab=stats", status_code=303)


@app.post("/admin/users/add")
def admin_add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
    _=Depends(auth.require_admin),
):
    username = username.strip()
    if not username or not password:
        return RedirectResponse("/admin?tab=users&user_error=empty", status_code=303)
    if password != password_confirm:
        return RedirectResponse("/admin?tab=users&user_error=mismatch", status_code=303)
    if auth.username_exists(db, username):
        return RedirectResponse("/admin?tab=users&user_error=exists", status_code=303)
    auth.create_user(db, username, password)
    return RedirectResponse("/admin?tab=users&user_added=1", status_code=303)


@app.post("/admin/users/delete/{user_id}")
def admin_delete_user(user_id: str, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    auth.delete_user(db, user_id)
    return RedirectResponse("/admin?tab=users", status_code=303)


@app.post("/admin/clear-ytdlp-cache")
def admin_clear_ytdlp_cache(_=Depends(auth.require_admin)):
    try:
        clear_ytdlp_cache()
    except Exception:
        pass
    return RedirectResponse("/admin?tab=settings&cache_cleared=1", status_code=303)


@app.post("/admin/run-cleanup-now")
def admin_run_cleanup_now(_=Depends(auth.require_admin)):
    try:
        run_cleanup_once()
    except Exception:
        pass
    return RedirectResponse("/admin?tab=settings&cleanup_ran=1", status_code=303)


@app.post("/admin/change-password")
def admin_change_password(
    request: Request,
    new_username: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(auth.require_admin),
):
    if new_password or new_password_confirm:
        if new_password != new_password_confirm:
            return RedirectResponse("/admin?tab=password&pw_error=1", status_code=303)
        auth.set_setting(db, "admin_password_hash", auth.pwd_context.hash(new_password))

    new_username = new_username.strip()
    if new_username:
        auth.set_setting(db, "admin_username", new_username)

    return RedirectResponse("/admin?tab=password&pw_saved=1", status_code=303)


@app.post("/admin/settings")
def admin_settings(
    request: Request,
    retention_hours: int = Form(...),
    cleanup_interval_minutes: int = Form(...),
    max_concurrent_downloads: int = Form(...),
    session_max_age_days: int = Form(...),
    proxy_url: str = Form(""),
    proxy_domains: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(auth.require_admin),
):
    auth.set_setting(db, "cleanup_hours", str(retention_hours))
    auth.set_setting(db, "cleanup_interval_minutes", str(cleanup_interval_minutes))
    auth.set_setting(db, "max_concurrent_downloads", str(max_concurrent_downloads))
    auth.set_setting(db, "session_max_age_days", str(session_max_age_days))
    auth.set_setting(db, "proxy_url", proxy_url.strip())
    auth.set_setting(db, "proxy_domains", proxy_domains.strip())

    return RedirectResponse("/admin?tab=settings&saved=1", status_code=303)
