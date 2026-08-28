import ipaddress
import os
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

import yt_dlp

from . import auth, config
from .database import SessionLocal
from .models import Download

# Sized generously and fixed — the actual concurrency cap is admin-configurable
# (max_concurrent_downloads, stored in the DB) and enforced by _ConcurrencyGate
# below, not by this pool's size.
_executor = ThreadPoolExecutor(max_workers=16)

COMMON_LABELS = {
    4320: "8K",
    2160: "4K",
    1440: "2K",
    1080: "Full HD",
    720: "HD",
}

SKIP_EXT = {".srt", ".vtt", ".json", ".description", ".part", ".ytdl"}

VIDEO_FORMATS = {"mp4", "webm", "mkv"}
AUDIO_FORMATS = {"mp3", "m4a", "opus", "wav"}


class _ConcurrencyGate:
    """Caps how many downloads actually run at once, against a limit that
    can change at runtime (read fresh from the DB on every acquire)."""

    def __init__(self):
        self._cv = threading.Condition()
        self._active = 0

    def acquire(self, limit: int):
        with self._cv:
            while self._active >= limit:
                self._cv.wait()
            self._active += 1

    def release(self):
        with self._cv:
            self._active -= 1
            self._cv.notify()


_gate = _ConcurrencyGate()


def clear_ytdlp_cache():
    """Wipes yt-dlp's own on-disk cache (extractor artifacts, nsig functions, ...).
    A common fix when extraction starts failing for reasons unrelated to our code."""
    yt_dlp.YoutubeDL({"quiet": True}).cache.remove()


def _height_filter(quality: str) -> str:
    if not quality or quality == "best":
        return ""
    try:
        height = int(quality)
    except ValueError:
        return ""
    return f"[height<={height}]"


def probe_qualities(url: str, db):
    """Fetch the real (width x height) resolutions actually available for this URL."""
    if not is_url_allowed(url, db):
        raise RuntimeError("Це посилання вказує на заборонену адресу")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if _should_use_proxy(url, db):
        ydl_opts["proxy"] = auth.get_proxy_url(db)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # dedupe by height only: several formats (different codecs/bitrates) often
    # share the same height, and the download-side quality filter also caps by height
    by_height = {}
    best_audio_bytes = 0
    for f in (info or {}).get("formats", []) or []:
        h = f.get("height")
        w = f.get("width")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        fsize = f.get("filesize") or f.get("filesize_approx")

        if h and vcodec not in (None, "none"):
            h = int(h)
            entry = by_height.get(h)
            if entry is None or (w and w > (entry["width"] or 0)):
                by_height[h] = {"width": w, "bytes": fsize}
        elif (not h) and vcodec in (None, "none") and acodec not in (None, "none") and fsize:
            best_audio_bytes = max(best_audio_bytes, fsize)

    result = []
    for h in sorted(by_height, reverse=True):
        entry = by_height[h]
        w = entry["width"]
        label = f"{w}×{h}" if w else f"{h}p"
        common = COMMON_LABELS.get(h)
        if common:
            label += f" ({common})"
        result.append({
            "value": str(h),
            "label": label,
            "video_bytes": entry["bytes"],
            "audio_bytes": best_audio_bytes or None,
        })
    return result


def _source_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", netloc)
    except Exception:
        return "unknown"


def _should_use_proxy(url: str, db) -> bool:
    proxy_url = auth.get_proxy_url(db)
    if not proxy_url:
        return False
    domains = auth.get_proxy_domains(db)
    if not domains:
        return True
    source = _source_from_url(url)
    return any(d in source for d in domains)


def _is_safe_direct_url(url: str) -> bool:
    """Rejects hosts that resolve to a private/internal IP, so the download
    form can't be used to make the server probe its own local network."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        for info in socket.getaddrinfo(hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


def is_url_allowed(url: str, db) -> bool:
    # Proxied domains are a small admin-curated allowlist (not attacker
    # controlled) and are resolved remotely by the proxy anyway, so the
    # local SSRF check doesn't apply to them.
    if _should_use_proxy(url, db):
        return True
    return _is_safe_direct_url(url)


def submit_job(job_id: str):
    _executor.submit(_run_job, job_id)


def _update(db, job, **fields):
    for k, v in fields.items():
        setattr(job, k, v)
    db.commit()


def _find_main_file(out_dir: str):
    candidates = []
    for name in os.listdir(out_dir):
        path = os.path.join(out_dir, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() not in SKIP_EXT:
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=os.path.getsize)


def _progress_hook(job_id, d):
    db = SessionLocal()
    try:
        job = db.get(Download, job_id)
        if not job:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                job.progress = round(downloaded / total * 100, 1)
            job.status = "downloading"
            db.commit()
        elif d.get("status") == "finished":
            job.progress = 99.0
            db.commit()
    except Exception:
        pass
    finally:
        db.close()


def _run_job(job_id: str):
    db = SessionLocal()
    job = db.get(Download, job_id)
    if not job:
        db.close()
        return

    limit = auth.get_max_concurrent_downloads(db)
    _gate.acquire(limit)
    try:
        if not is_url_allowed(job.url, db):
            raise RuntimeError("Це посилання вказує на заборонену адресу")

        _update(db, job, status="downloading")

        out_dir = os.path.join(config.DOWNLOAD_DIR, job_id)
        os.makedirs(out_dir, exist_ok=True)
        outtmpl = os.path.join(out_dir, "%(title).150B.%(ext)s")

        height_filter = _height_filter(job.quality)

        ydl_opts = {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [lambda d: _progress_hook(job_id, d)],
        }
        if _should_use_proxy(job.url, db):
            ydl_opts["proxy"] = auth.get_proxy_url(db)

        if job.mode == "audio":
            audio_codec = job.container if job.container in AUDIO_FORMATS else "mp3"
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_codec,
                "preferredquality": "192",
            }]
        elif job.mode == "video_only":
            if job.premiere_compat:
                ydl_opts["format"] = (
                    f"bestvideo[vcodec^=avc1]{height_filter}/bestvideo{height_filter}/best{height_filter}"
                )
            else:
                ydl_opts["format"] = f"bestvideo{height_filter}/best{height_filter}"
            if job.container in VIDEO_FORMATS:
                ydl_opts.setdefault("postprocessors", [])
                ydl_opts["postprocessors"].append({
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": job.container,
                })
        else:
            if job.premiere_compat:
                # Prefer H.264 video + AAC audio (what Adobe Premiere Pro can actually
                # decode) over YouTube's usual best pick, which is often VP9/AV1 + Opus.
                ydl_opts["format"] = (
                    f"bestvideo[vcodec^=avc1]{height_filter}+bestaudio[acodec^=mp4a]"
                    f"/best[vcodec^=avc1]{height_filter}"
                    f"/bestvideo{height_filter}+bestaudio/best{height_filter}"
                )
            else:
                ydl_opts["format"] = f"bestvideo{height_filter}+bestaudio/best{height_filter}"
            if job.container in VIDEO_FORMATS:
                ydl_opts["merge_output_format"] = job.container

        if job.subtitles:
            ydl_opts["writesubtitles"] = True
            ydl_opts["writeautomaticsub"] = True
            ydl_opts["subtitleslangs"] = ["uk", "en"]
            ydl_opts.setdefault("postprocessors", [])
            ydl_opts["postprocessors"].append({
                "key": "FFmpegSubtitlesConvertor",
                "format": "srt",
            })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(job.url, download=True)

        title = (info or {}).get("title") or "video"
        filepath = _find_main_file(out_dir)
        filesize = os.path.getsize(filepath) if filepath and os.path.exists(filepath) else None

        if not filepath:
            raise RuntimeError("Не вдалося знайти завантажений файл")

        _update(
            db, job,
            status="finished",
            progress=100.0,
            title=title,
            filepath=filepath,
            filesize=filesize,
            finished_at=datetime.utcnow(),
        )
    except Exception as e:
        _update(db, job, status="error", error_message=str(e)[:500], finished_at=datetime.utcnow())
    finally:
        _gate.release()
        db.close()
