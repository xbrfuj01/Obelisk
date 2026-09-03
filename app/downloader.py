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

LANG_NAMES = {
    # Native/autonym names for languages Ukrainian users are most likely to
    # see; everything else falls back to an English name below rather than
    # a raw 2-3 letter code — YouTube alone offers 100+ auto-translated
    # caption languages, most of which will never hit the first list.
    "uk": "Українська", "en": "English", "ru": "Русский", "es": "Español",
    "fr": "Français", "de": "Deutsch", "pl": "Polski", "pt": "Português",
    "it": "Italiano", "ja": "日本語", "ko": "한국어", "zh": "中文", "tr": "Türkçe",
    "ar": "العربية", "hi": "हिन्दी", "id": "Bahasa Indonesia", "vi": "Tiếng Việt",
    "th": "ไทย", "nl": "Nederlands", "sv": "Svenska", "no": "Norsk", "da": "Dansk",
    "fi": "Suomi", "cs": "Čeština", "sk": "Slovenčina", "hu": "Magyar",
    "ro": "Română", "bg": "Български", "el": "Ελληνικά", "he": "עברית", "iw": "עברית",
    "fa": "فارسی", "ur": "اردو", "bn": "বাংলা", "ta": "தமிழ்", "sr": "Српски",
    "hr": "Hrvatski", "sl": "Slovenščina", "lt": "Lietuvių", "lv": "Latviešu",
    "et": "Eesti", "be": "Беларуская", "ka": "ქართული", "hy": "Հայերեն",
    "az": "Azərbaycan", "kk": "Қазақша", "uz": "Oʻzbekcha", "ms": "Bahasa Melayu",
    "sw": "Kiswahili", "fil": "Filipino", "sq": "Shqip", "mk": "Македонски",

    "aa": "Afar", "ab": "Abkhaz", "ae": "Avestan", "af": "Afrikaans",
    "ak": "Akan", "am": "Amharic", "an": "Aragonese", "as": "Assamese",
    "av": "Avar", "ay": "Aymara", "ba": "Bashkir", "bh": "Bihari",
    "bi": "Bislama", "bm": "Bambara", "bo": "Tibetan", "br": "Breton",
    "bs": "Bosnian", "ca": "Catalan", "ce": "Chechen", "ch": "Chamorro",
    "co": "Corsican", "cr": "Cree", "cu": "Church Slavic", "cv": "Chuvash",
    "cy": "Welsh", "dv": "Divehi", "dz": "Dzongkha", "ee": "Ewe",
    "eo": "Esperanto", "eu": "Basque", "ff": "Fulah", "fj": "Fijian",
    "fo": "Faroese", "fy": "Western Frisian", "ga": "Irish",
    "gd": "Scottish Gaelic", "gl": "Galician", "gn": "Guarani",
    "gu": "Gujarati", "gv": "Manx", "ha": "Hausa", "haw": "Hawaiian",
    "hmn": "Hmong", "ho": "Hiri Motu", "hz": "Herero", "ia": "Interlingua",
    "ie": "Interlingue", "ig": "Igbo", "ii": "Sichuan Yi", "ik": "Inupiaq",
    "io": "Ido", "is": "Icelandic", "iu": "Inuktitut", "jv": "Javanese",
    "jw": "Javanese", "kg": "Kongo", "ki": "Kikuyu", "kj": "Kuanyama",
    "kl": "Kalaallisut", "km": "Khmer", "kn": "Kannada", "kr": "Kanuri",
    "ks": "Kashmiri", "ku": "Kurdish", "kv": "Komi", "kw": "Cornish",
    "ky": "Kyrgyz", "la": "Latin", "lb": "Luxembourgish", "lg": "Ganda",
    "li": "Limburgish", "ln": "Lingala", "lo": "Lao", "lu": "Luba-Katanga",
    "mg": "Malagasy", "mh": "Marshallese", "mi": "Maori", "ml": "Malayalam",
    "mn": "Mongolian", "mr": "Marathi", "mt": "Maltese", "my": "Burmese",
    "na": "Nauru", "nb": "Norwegian Bokmål", "nd": "North Ndebele",
    "ne": "Nepali", "ng": "Ndonga", "nn": "Norwegian Nynorsk",
    "nr": "South Ndebele", "nv": "Navajo", "ny": "Chichewa", "oc": "Occitan",
    "oj": "Ojibwe", "om": "Oromo", "or": "Odia", "os": "Ossetian",
    "pa": "Punjabi", "pi": "Pali", "ps": "Pashto", "qu": "Quechua",
    "rm": "Romansh", "rn": "Rundi", "rw": "Kinyarwanda", "sa": "Sanskrit",
    "sc": "Sardinian", "sd": "Sindhi", "se": "Northern Sami", "sg": "Sango",
    "si": "Sinhala", "sm": "Samoan", "sn": "Shona", "so": "Somali",
    "ss": "Swati", "st": "Southern Sotho", "su": "Sundanese", "te": "Telugu",
    "tg": "Tajik", "ti": "Tigrinya", "tk": "Turkmen", "tl": "Tagalog",
    "tn": "Tswana", "to": "Tongan", "ts": "Tsonga", "tt": "Tatar",
    "tw": "Twi", "ty": "Tahitian", "ug": "Uyghur", "ve": "Venda",
    "vo": "Volapük", "wa": "Walloon", "wo": "Wolof", "xh": "Xhosa",
    "yi": "Yiddish", "yo": "Yoruba", "za": "Zhuang", "zu": "Zulu",

    # YouTube-specific/legacy codes not in plain ISO 639-1 (its caption
    # auto-translate list includes languages with no 2-letter code at all).
    "bho": "Bhojpuri", "ceb": "Cebuano", "kri": "Krio", "luo": "Luo",
    "nso": "Northern Sotho", "yue": "Cantonese", "in": "Bahasa Indonesia",
    "ji": "Yiddish", "mni": "Meitei", "gom": "Konkani", "doi": "Dogri",
}

PRIORITY_LANG_ORDER = ["uk", "en", "ru"]


def _lang_label(code):
    return LANG_NAMES.get(code) or LANG_NAMES.get(code.split("-")[0]) or code


EMBEDDABLE_SUBTITLE_CONTAINERS = {"mp4", "mkv"}

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


def parse_timecode(value):
    """Accepts "SS", "MM:SS" or "HH:MM:SS" (fractional seconds allowed) and
    returns seconds as a float, or None if empty/unparseable/out of range.

    Only the leftmost (most significant) part is unbounded — minutes and
    seconds parts must each be below 60, so "00:61:67" is rejected rather
    than silently normalized.
    """
    value = (value or "").strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) > 3:
        return None
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return None
    if any(p < 0 for p in parts):
        return None
    if any(p >= 60 for p in parts[1:]):
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def _height_filter(quality: str) -> str:
    if not quality or quality == "best":
        return ""
    try:
        height = int(quality)
    except ValueError:
        return ""
    return f"[height<={height}]"


def _subtitle_options(info):
    manual = (info or {}).get("subtitles") or {}
    auto = (info or {}).get("automatic_captions") or {}
    result = []
    seen_bases = set()
    for code in manual:
        base = code.split("-")[0]
        if base in seen_bases:
            continue
        seen_bases.add(base)
        result.append({"code": code, "label": _lang_label(code), "auto": False})
    for code in auto:
        base = code.split("-")[0]
        if base in seen_bases:
            continue
        seen_bases.add(base)
        result.append({"code": code, "label": _lang_label(code), "auto": True})

    def sort_key(item):
        base = item["code"].split("-")[0]
        if base in PRIORITY_LANG_ORDER:
            return (0, PRIORITY_LANG_ORDER.index(base))
        return (1, 0)

    result.sort(key=sort_key)
    return result


def probe_qualities(url: str, db):
    """Fetch the real (width x height) resolutions and subtitle languages available for this URL."""
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
    best_audio_bytes = None
    for f in (info or {}).get("formats", []) or []:
        h = f.get("height")
        w = f.get("width")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        # Deliberately not estimated from tbr when missing: bitrate isn't a
        # reliable stand-in for actual size (it skewed badly high on real
        # videos in practice), and a blank size beats a wrong one.
        fsize = f.get("filesize") or f.get("filesize_approx")

        if h and vcodec not in (None, "none"):
            # yt-dlp's own "bestvideo"/"bestaudio" selectors pick the LAST
            # matching entry in this list — it comes back pre-sorted
            # worst-to-best by yt-dlp's own preference — so mirror that
            # instead of picking whichever codec variant happens to have the
            # biggest file at this height. Otherwise the shown estimate can
            # be a completely different (larger) codec than what a real
            # download actually selects.
            by_height[int(h)] = {"width": w, "bytes": fsize}
        elif (not h) and vcodec in (None, "none") and acodec not in (None, "none"):
            best_audio_bytes = fsize

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
    return {"qualities": result, "subtitles": _subtitle_options(info)}


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


def _combine_leg_progress(leg: int, leg_pct: float) -> float:
    """Maps progress within one download leg to a spot on the overall bar.

    "video" mode downloads video and audio as two separate legs (yt-dlp
    merges them afterwards via ffmpeg), which otherwise shows as the bar
    going 0->100% twice. The first leg (video, normally the bulk of the
    size) gets 0-90%, a second leg (audio) gets 90-99%, and the last 1% is
    left for the merge/postprocessing step, which isn't itself tracked here.
    """
    if leg <= 1:
        return round(leg_pct * 0.9, 1)
    if leg == 2:
        return round(90 + leg_pct * 0.09, 1)
    return 99.0


def _progress_hook(job_id, d, state):
    db = SessionLocal()
    try:
        job = db.get(Download, job_id)
        if not job:
            return
        status = d.get("status")
        if status == "downloading":
            if not state["leg_active"]:
                state["leg_active"] = True
                state["leg"] += 1
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            # For timecode-clipped downloads yt-dlp's ffmpeg-based range
            # downloader never fires this hook mid-download at all (only
            # once, at "finished") — so total/downloaded stay at 0 here and
            # the UI shows an indeterminate bar instead of a fake percentage.
            leg_pct = (downloaded / total * 100) if total else 0
            job.progress = _combine_leg_progress(state["leg"], leg_pct)
            job.status = "downloading"
            # yt-dlp already computes this from the current transfer rate -
            # it's per-leg (video, then audio for "video" mode), not for the
            # combined job, but that's a fine approximation for "приблизно".
            job.eta_seconds = d.get("eta")
            db.commit()
        elif status == "finished":
            state["leg_active"] = False
            job.progress = _combine_leg_progress(state["leg"], 100)
            job.eta_seconds = None
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

        progress_state = {"leg": 0, "leg_active": False}
        ydl_opts = {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [lambda d: _progress_hook(job_id, d, progress_state)],
        }
        if _should_use_proxy(job.url, db):
            ydl_opts["proxy"] = auth.get_proxy_url(db)

        if job.clip_start is not None or job.clip_end is not None:
            from yt_dlp.utils import download_range_func
            start = job.clip_start or 0
            end = job.clip_end if job.clip_end is not None else float("inf")
            ydl_opts["download_ranges"] = download_range_func([], [(start, end)])
            # Without this, ffmpeg trims via stream copy, which requires an
            # actual keyframe inside the requested range to cut on. Short
            # clips (YouTube Shorts, or a tight timecode range on a longer
            # video) often have only one keyframe for the whole clip, so a
            # stream-copy cut either fails outright ("ffmpeg exited with
            # code ...") or silently produces a near-empty file. Re-encoding
            # is slower but always produces a correct, complete clip.
            ydl_opts["force_keyframes_at_cuts"] = True

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

        if job.subtitle_lang and job.mode != "audio" and job.container in EMBEDDABLE_SUBTITLE_CONTAINERS:
            # Written as .srt and embedded (soft subs) directly into the video so
            # there's still exactly one output file — no orphaned subtitle file
            # left behind that nothing ever downloads or cleans up.
            ydl_opts["writesubtitles"] = True
            ydl_opts["writeautomaticsub"] = True
            ydl_opts["subtitleslangs"] = [job.subtitle_lang]
            ydl_opts.setdefault("postprocessors", [])
            ydl_opts["postprocessors"].append({
                "key": "FFmpegSubtitlesConvertor",
                "format": "srt",
            })
            ydl_opts["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})

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
            eta_seconds=None,
            title=title,
            filepath=filepath,
            filesize=filesize,
            finished_at=datetime.utcnow(),
        )
    except Exception as e:
        _update(db, job, status="error", eta_seconds=None, error_message=str(e)[:500], finished_at=datetime.utcnow())
    finally:
        _gate.release()
        db.close()
