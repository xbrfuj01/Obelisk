import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

import yt_dlp

from . import config
from .database import SessionLocal
from .models import Download

_executor = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_DOWNLOADS)

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


def _height_filter(quality: str) -> str:
    if not quality or quality == "best":
        return ""
    try:
        height = int(quality)
    except ValueError:
        return ""
    return f"[height<={height}]"


def probe_qualities(url: str):
    """Fetch the real (width x height) resolutions actually available for this URL."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if _should_use_proxy(url):
        ydl_opts["proxy"] = config.PROXY_URL
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # dedupe by height only: several formats (different codecs/bitrates) often
    # share the same height, and the download-side quality filter also caps by height
    by_height = {}
    for f in (info or {}).get("formats", []) or []:
        h = f.get("height")
        w = f.get("width")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        h = int(h)
        if h not in by_height or (w and w > (by_height[h] or 0)):
            by_height[h] = w

    result = []
    for h in sorted(by_height, reverse=True):
        w = by_height[h]
        label = f"{w}×{h}" if w else f"{h}p"
        common = COMMON_LABELS.get(h)
        if common:
            label += f" ({common})"
        result.append({"value": str(h), "label": label})
    return result


def _source_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", netloc)
    except Exception:
        return "unknown"


def _should_use_proxy(url: str) -> bool:
    if not config.PROXY_URL:
        return False
    if not config.PROXY_DOMAINS:
        return True
    source = _source_from_url(url)
    return any(d in source for d in config.PROXY_DOMAINS)


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
    try:
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
        if _should_use_proxy(job.url):
            ydl_opts["proxy"] = config.PROXY_URL

        if job.mode == "audio":
            audio_codec = job.container if job.container in AUDIO_FORMATS else "mp3"
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_codec,
                "preferredquality": "192",
            }]
        elif job.mode == "video_only":
            ydl_opts["format"] = f"bestvideo{height_filter}/best{height_filter}"
            if job.container in VIDEO_FORMATS:
                ydl_opts.setdefault("postprocessors", [])
                ydl_opts["postprocessors"].append({
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": job.container,
                })
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
        db.close()
