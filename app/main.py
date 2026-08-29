import os
import re
import shutil
import uuid

from fastapi import (
    BackgroundTasks, FastAPI, File, Request, Response, Form, Depends, HTTPException, UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import config
from .database import init_db, SessionLocal
from .models import Conversion, Download
from . import auth
from . import converter
from . import metadata_tool
from .downloader import (
    submit_job,
    _source_from_url,
    probe_qualities,
    is_url_allowed,
    clear_ytdlp_cache,
    parse_timecode,
)
from .cleanup import start_cleanup_thread, run_cleanup_once
from . import timeutil
from . import stats as stats_module
from . import sysinfo

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
templates.env.globals["format_dt"] = timeutil.format_local
templates.env.globals["format_bytes"] = sysinfo.format_bytes


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


@app.get("/converter", response_class=HTMLResponse)
def converter_page(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    client_id = request.cookies.get(CLIENT_ID_COOKIE)
    is_new_client = not client_id
    if is_new_client:
        client_id = uuid.uuid4().hex

    recent = (
        db.query(Conversion)
        .filter(Conversion.client_id == client_id)
        .order_by(Conversion.created_at.desc())
        .limit(20)
        .all()
    )
    resp = templates.TemplateResponse(
        "converter.html",
        {"request": request, "recent": recent, "max_upload_mb": auth.get_max_upload_mb(db)},
    )
    if is_new_client:
        resp.set_cookie(
            CLIENT_ID_COOKIE, client_id, max_age=CLIENT_ID_MAX_AGE, httponly=True, samesite="lax"
        )
    return resp


@app.get("/metadata", response_class=HTMLResponse)
def metadata_page(request: Request, db: Session = Depends(get_db), _=Depends(require_site_access_page)):
    return templates.TemplateResponse(
        "metadata.html", {"request": request, "max_upload_mb": auth.get_max_upload_mb(db)}
    )


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
    clip_start: str = Form(""),
    clip_end: str = Form(""),
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

    clip_start_sec = parse_timecode(clip_start)
    clip_end_sec = parse_timecode(clip_end)
    if clip_start and clip_start_sec is None:
        return JSONResponse({"error": "Некоректний початковий таймкод"}, status_code=400)
    if clip_end and clip_end_sec is None:
        return JSONResponse({"error": "Некоректний кінцевий таймкод"}, status_code=400)
    if clip_start_sec is not None and clip_end_sec is not None and clip_end_sec <= clip_start_sec:
        return JSONResponse({"error": "Кінцевий таймкод має бути більшим за початковий"}, status_code=400)

    job = Download(
        url=url,
        source=_source_from_url(url),
        mode=mode,
        quality=quality,
        container=container,
        subtitle_lang=subtitle_lang.strip() or None,
        premiere_compat=1 if premiere_compat else 0,
        clip_start=clip_start_sec,
        clip_end=clip_end_sec,
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
def get_formats(request: Request, url: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"fmt:{ip}"):
        return JSONResponse(
            {"error": "Забагато запитів поспіль. Спробуйте пізніше."}, status_code=429
        )
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


# ---------------- Video converter ----------------

CONVERT_QUALITIES = {"high", "medium", "low"}
CONVERT_AUDIO_OPTIONS = {"aac", "original", "none"}


def _finalize_conversion(db, request, response, job_id, input_path, original_name, quality, audio_option):
    """Shared by /api/convert (browser upload) and /api/convert/from-download
    (reuses an already-downloaded file): probes the file already sitting at
    input_path, creates the Conversion row, and kicks off the background
    job. Returns (json_result, None) on success or (None, error_response)."""
    job_dir = os.path.dirname(input_path)
    if quality not in CONVERT_QUALITIES:
        quality = "high"
    if audio_option not in CONVERT_AUDIO_OPTIONS:
        audio_option = "original"

    info = converter.probe_input(input_path)
    if not info:
        shutil.rmtree(job_dir, ignore_errors=True)
        return None, JSONResponse({"error": "Не вдалося розпізнати відеофайл"}, status_code=400)

    client_id = get_client_id(request, response)
    job = Conversion(
        id=job_id,
        original_filename=original_name,
        input_summary=info["summary"],
        duration_seconds=info["duration"],
        quality=quality,
        audio_option=audio_option,
        status="queued",
        client_ip=request.client.host if request.client else None,
        client_id=client_id,
        username=request.session.get("site_username"),
    )
    db.add(job)
    db.commit()

    converter.submit_job(job_id, input_path, info)
    return {"id": job.id, "input_summary": job.input_summary, "duration_seconds": job.duration_seconds}, None


@app.post("/api/convert")
async def create_conversion(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    quality: str = Form("high"),
    audio_option: str = Form("original"),
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"cv:{ip}"):
        return JSONResponse(
            {"error": "Забагато конвертацій поспіль. Спробуйте пізніше."}, status_code=429
        )

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(config.DOWNLOAD_DIR, "converts", job_id)
    os.makedirs(job_dir, exist_ok=True)

    original_name = file.filename or "video"
    ext = os.path.splitext(original_name)[1][:10] or ".bin"
    input_path = os.path.join(job_dir, f"input{ext}")

    max_bytes = auth.get_max_upload_mb(db) * 1024 * 1024
    total = 0
    too_large = False
    with open(input_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                too_large = True
                break
            out.write(chunk)
    if too_large:
        shutil.rmtree(job_dir, ignore_errors=True)
        return JSONResponse(
            {"error": f"Файл перевищує ліміт {auth.get_max_upload_mb(db)} МБ"}, status_code=413
        )

    result, error_resp = _finalize_conversion(
        db, request, response, job_id, input_path, original_name, quality, audio_option
    )
    return error_resp if error_resp else result


@app.post("/api/convert/from-download/{download_id}")
def create_conversion_from_download(
    download_id: str,
    request: Request,
    response: Response,
    quality: str = Form("high"),
    audio_option: str = Form("original"),
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    src = db.get(Download, download_id)
    if not src or src.status != "finished" or not src.filepath or not os.path.exists(src.filepath):
        return JSONResponse({"error": "Вихідний файл недоступний"}, status_code=404)
    if src.client_id != get_client_id(request, response):
        return JSONResponse({"error": "Вихідний файл недоступний"}, status_code=404)

    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"cv:{ip}"):
        return JSONResponse(
            {"error": "Забагато конвертацій поспіль. Спробуйте пізніше."}, status_code=429
        )

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(config.DOWNLOAD_DIR, "converts", job_id)
    os.makedirs(job_dir, exist_ok=True)

    original_name = os.path.basename(src.filepath)
    ext = os.path.splitext(original_name)[1][:10] or ".bin"
    input_path = os.path.join(job_dir, f"input{ext}")
    shutil.copyfile(src.filepath, input_path)

    result, error_resp = _finalize_conversion(
        db, request, response, job_id, input_path, original_name, quality, audio_option
    )
    return error_resp if error_resp else result


@app.get("/api/convert/status/{job_id}")
def conversion_status(job_id: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    job = db.get(Conversion, job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "input_summary": job.input_summary,
        "duration_seconds": job.duration_seconds,
        "error": job.error_message,
        "filesize": job.filesize,
    }


@app.get("/api/convert/recent")
def recent_conversions(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    client_id = get_client_id(request, response)
    rows = (
        db.query(Conversion)
        .filter(Conversion.client_id == client_id)
        .order_by(Conversion.created_at.desc())
        .limit(20)
        .all()
    )
    return [
        {
            "id": r.id,
            "title": r.original_filename or "video",
            "status": r.status,
            "progress": r.progress,
            "filesize": r.filesize,
        }
        for r in rows
    ]


@app.get("/api/convert/file/{job_id}")
def conversion_file(job_id: str, db: Session = Depends(get_db), _=Depends(require_site_access_api)):
    job = db.get(Conversion, job_id)
    if not job or job.status != "finished" or not job.filepath or not os.path.exists(job.filepath):
        return JSONResponse({"error": "Файл недоступний"}, status_code=404)
    filename = os.path.basename(job.filepath)
    return FileResponse(job.filepath, filename=filename)


# ---------------- Metadata editor ----------------
# No DB history here on purpose (unlike downloads/conversions) - this is a
# quick read-then-strip operation, not a background job. The cleaned file
# sits in a token-named temp dir just long enough to be downloaded once,
# cleaned up right after via a background task, with cleanup.py sweeping
# any abandoned ones (user never came back for the download) as a backstop.

METADATA_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


@app.post("/api/metadata/process")
async def process_metadata(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _=Depends(require_site_access_api),
):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_download_rate_limit(f"md:{ip}"):
        return JSONResponse(
            {"error": "Забагато запитів поспіль. Спробуйте пізніше."}, status_code=429
        )

    token = uuid.uuid4().hex
    job_dir = os.path.join(config.DOWNLOAD_DIR, "metadata", token)
    os.makedirs(job_dir, exist_ok=True)

    original_name = file.filename or "file"
    ext = os.path.splitext(original_name)[1][:15] or ".bin"
    input_path = os.path.join(job_dir, f"input{ext}")

    max_bytes = auth.get_max_upload_mb(db) * 1024 * 1024
    total = 0
    too_large = False
    with open(input_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                too_large = True
                break
            out.write(chunk)
    if too_large:
        shutil.rmtree(job_dir, ignore_errors=True)
        return JSONResponse(
            {"error": f"Файл перевищує ліміт {auth.get_max_upload_mb(db)} МБ"}, status_code=413
        )

    metadata = metadata_tool.read_metadata(input_path)
    if metadata is None:
        shutil.rmtree(job_dir, ignore_errors=True)
        return JSONResponse({"error": "Не вдалося прочитати цей файл"}, status_code=400)

    clean_name = re.sub(r"[^\w\-. ]", "_", os.path.basename(original_name)).strip(" .") or "file"
    output_path = os.path.join(job_dir, f"clean_{clean_name}")
    ok, _err = metadata_tool.strip_metadata(input_path, output_path)
    if not ok:
        shutil.rmtree(job_dir, ignore_errors=True)
        return JSONResponse(
            {"error": "Не вдалося видалити метадані з цього формату файлу"}, status_code=400
        )

    after = metadata_tool.read_metadata(output_path)
    verified = after is not None
    classified = metadata_tool.classify_metadata(metadata, after, verified=verified)
    found_count = sum(1 for c in classified.values() if c["status"] != "absent")
    removable_count = sum(1 for c in classified.values() if c["status"] == "removable")
    return {
        "token": token,
        "filename": clean_name,
        "metadata": classified,
        "found_count": found_count,
        "removable_count": removable_count,
        "verified": verified,
    }


@app.get("/api/metadata/download/{token}")
def download_clean_file(
    token: str, background_tasks: BackgroundTasks, _=Depends(require_site_access_api)
):
    if not METADATA_TOKEN_RE.match(token):
        return JSONResponse({"error": "Файл недоступний"}, status_code=404)
    job_dir = os.path.join(config.DOWNLOAD_DIR, "metadata", token)
    if not os.path.isdir(job_dir):
        return JSONResponse({"error": "Файл недоступний"}, status_code=404)
    candidates = [f for f in os.listdir(job_dir) if f.startswith("clean_")]
    if not candidates:
        return JSONResponse({"error": "Файл недоступний"}, status_code=404)
    filepath = os.path.join(job_dir, candidates[0])
    filename = candidates[0][len("clean_"):]
    background_tasks.add_task(shutil.rmtree, job_dir, ignore_errors=True)
    return FileResponse(filepath, filename=filename)


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
        auth.record_login(db, username)
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

    conversion_total = db.query(func.count(Conversion.id)).scalar()
    conversion_finished = db.query(func.count(Conversion.id)).filter(Conversion.status == "finished").scalar()
    conversion_errors = db.query(func.count(Conversion.id)).filter(Conversion.status == "error").scalar()
    conversion_total_size = (
        db.query(func.coalesce(func.sum(Conversion.filesize), 0))
        .filter(Conversion.status == "finished")
        .scalar()
    )
    conversion_history = db.query(Conversion).order_by(Conversion.created_at.desc()).limit(100).all()

    user_activity = stats_module.user_activity(db)
    activity_periods = [(key, label) for key, label, _delta in stats_module.PERIODS]

    sys_info = {
        "memory": sysinfo.get_memory_stats(),
        "cpu_temp": sysinfo.get_cpu_temperature(),
        "network": sysinfo.get_network_stats(),
    }

    retention_hours = auth.get_retention_hours(db)
    admin_username = auth.get_admin_username(db)
    site_gate_enabled = auth.is_site_gate_enabled(db)
    users = auth.list_users(db)
    cleanup_interval_minutes = auth.get_cleanup_interval_minutes(db)
    max_concurrent_downloads = auth.get_max_concurrent_downloads(db)
    max_concurrent_conversions = auth.get_max_concurrent_conversions(db)
    max_upload_mb = auth.get_max_upload_mb(db)
    session_max_age_days = auth.get_session_max_age_days(db)
    proxy_url = auth.get_proxy_url(db)
    proxy_domains = ",".join(auth.get_proxy_domains(db))
    timezone = auth.get_timezone(db)

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
            "conversion_total": conversion_total,
            "conversion_finished": conversion_finished,
            "conversion_errors": conversion_errors,
            "conversion_total_size": conversion_total_size,
            "conversion_history": conversion_history,
            "user_activity": user_activity,
            "activity_periods": activity_periods,
            "sys_info": sys_info,
            "retention_hours": retention_hours,
            "admin_username": admin_username,
            "cleanup_interval_minutes": cleanup_interval_minutes,
            "max_concurrent_downloads": max_concurrent_downloads,
            "max_concurrent_conversions": max_concurrent_conversions,
            "max_upload_mb": max_upload_mb,
            "session_max_age_days": session_max_age_days,
            "proxy_url": proxy_url,
            "proxy_domains": proxy_domains,
            "timezone": timezone,
            "timezones": timeutil.COMMON_TIMEZONES,
        },
    )


@app.get("/admin/api/sysinfo")
def admin_sysinfo(_=Depends(auth.require_admin)):
    return {
        "memory": sysinfo.get_memory_stats(),
        "cpu_temp": sysinfo.get_cpu_temperature(),
        "network": sysinfo.get_network_stats(),
    }


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


@app.post("/admin/delete-conversion/{job_id}")
def admin_delete_conversion(job_id: str, db: Session = Depends(get_db), _=Depends(auth.require_admin)):
    job = db.get(Conversion, job_id)
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


@app.post("/admin/users/reset-password/{user_id}")
def admin_reset_user_password(
    user_id: str,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    _=Depends(auth.require_admin),
):
    if not new_password:
        return RedirectResponse("/admin?tab=users&user_error=empty", status_code=303)
    auth.reset_user_password(db, user_id, new_password)
    return RedirectResponse("/admin?tab=users&pw_reset=1", status_code=303)


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
    max_concurrent_conversions: int = Form(...),
    max_upload_mb: int = Form(...),
    session_max_age_days: int = Form(...),
    proxy_url: str = Form(""),
    proxy_domains: str = Form(""),
    timezone: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(auth.require_admin),
):
    auth.set_setting(db, "cleanup_hours", str(retention_hours))
    auth.set_setting(db, "cleanup_interval_minutes", str(cleanup_interval_minutes))
    auth.set_setting(db, "max_concurrent_downloads", str(max_concurrent_downloads))
    auth.set_setting(db, "max_concurrent_conversions", str(max_concurrent_conversions))
    auth.set_setting(db, "max_upload_mb", str(max_upload_mb))
    auth.set_setting(db, "session_max_age_days", str(session_max_age_days))
    auth.set_setting(db, "proxy_url", proxy_url.strip())
    auth.set_setting(db, "proxy_domains", proxy_domains.strip())
    if timeutil.is_valid_timezone(timezone):
        auth.set_setting(db, "timezone", timezone)

    return RedirectResponse("/admin?tab=settings&saved=1", status_code=303)
