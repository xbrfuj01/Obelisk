import ipaddress
import os
import re
import shutil
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

# How much each new speed sample moves the smoothed estimate used for ETA -
# lower means steadier (slower to react to a real speed change), higher
# means more responsive (but jitterier). 0.25 roughly averages the last few
# progress-hook callbacks, which fire every second or so.
ETA_SMOOTHING_ALPHA = 0.25

# YouTube increasingly withholds some videos/formats ("This video is not
# available") from clients that can't prove they're a real browser - a
# proof-of-origin token. A real browser gets one automatically via Google's
# own JS; yt-dlp needs this sidecar (bgutil-ytdlp-pot-provider, bundled as
# its own service in docker-compose.yml) to generate one instead. Reachable
# over the compose network by its service name - if it's ever not running,
# yt-dlp degrades to not sending a token rather than failing outright.
YOUTUBE_EXTRACTOR_ARGS = {
    # Values must be lists, matching how --extractor-args "key=value" gets
    # parsed on the CLI ({'base_url': ['http://...']}) - a bare string here
    # gets iterated character-by-character instead, which is what was
    # producing "Unsupported url scheme: \"\"" against the provider.
    "youtubepot-bgutilhttp": {"base_url": ["http://bgutil-provider:4416"]},
    # YouTube has been rolling out "SABR-only" streaming for the default
    # web client - it still responds, but every format is missing a usable
    # URL ("YouTube is forcing SABR streaming for this client"), which is
    # exactly what was making otherwise-normal videos come back as
    # unavailable even with the PO token working. tv/ios/web_safari aren't
    # (yet) restricted this way - keeping "web" in the list too so videos
    # that don't hit this restriction still get its higher-res formats.
    "youtube": {"player_client": ["web", "tv", "ios", "web_safari"]},
}


class _YdlLogCapture:
    """Replaces yt-dlp's normal console output (suppressed via quiet=True)
    so its debug/warning lines - including anything about the PO token
    provider - end up somewhere the person hitting a download error can
    actually read them, instead of only in the container's own logs."""

    def __init__(self):
        self.lines = []

    def debug(self, msg):
        self.lines.append(msg)

    def warning(self, msg):
        self.lines.append(f"WARNING: {msg}")

    def error(self, msg):
        self.lines.append(f"ERROR: {msg}")

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

# job_ids the user has asked to cancel. A queued job checks this right after
# acquiring its concurrency slot (and skips running yt-dlp at all); an
# in-progress one is caught by the progress hook, which fires often enough
# for this to feel roughly instant.
_cancel_requested = set()


def request_cancel(job_id: str):
    _cancel_requested.add(job_id)


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
        "extractor_args": YOUTUBE_EXTRACTOR_ARGS,
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
    if job_id in _cancel_requested:
        # yt_dlp.utils.DownloadCancelled is the library's own supported way
        # to abort mid-download from inside a progress hook - unlike a plain
        # exception, it's guaranteed not to get swallowed as "a hook errored,
        # ignoring it" and actually propagates out of extract_info().
        from yt_dlp.utils import DownloadCancelled
        raise DownloadCancelled("cancelled by user")
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
                state["smoothed_speed"] = None  # new leg (e.g. audio after video) - unrelated transfer, fresh start
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            # For timecode-clipped downloads yt-dlp's ffmpeg-based range
            # downloader never fires this hook mid-download at all (only
            # once, at "finished") — so total/downloaded stay at 0 here and
            # the UI shows an indeterminate bar instead of a fake percentage.
            leg_pct = (downloaded / total * 100) if total else 0
            job.progress = _combine_leg_progress(state["leg"], leg_pct)
            job.status = "downloading"

            # yt-dlp's own eta jumps around a lot (it's derived from a very
            # short recent window - a momentary speed dip reads as "3 годин"
            # one tick and "5 хв" the next). Smoothing the transfer rate
            # ourselves with an exponential moving average - each new sample
            # nudges the estimate instead of replacing it outright - reacts
            # to a real, sustained speed change without visibly jittering.
            raw_speed = d.get("speed")
            if raw_speed:
                prev = state.get("smoothed_speed")
                state["smoothed_speed"] = raw_speed if not prev else (
                    ETA_SMOOTHING_ALPHA * raw_speed + (1 - ETA_SMOOTHING_ALPHA) * prev
                )
            smoothed_speed = state.get("smoothed_speed")
            if total and smoothed_speed:
                job.eta_seconds = max(0, round((total - downloaded) / smoothed_speed))
            else:
                job.eta_seconds = None
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
    log_capture = _YdlLogCapture()
    try:
        if job_id in _cancel_requested:
            # Cancelled while it was still waiting for a concurrency slot -
            # never actually started, so there's nothing to abort mid-flight.
            _update(db, job, status="cancelled", eta_seconds=None, finished_at=datetime.utcnow())
            return

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
            "verbose": True,  # otherwise yt-dlp's own debug lines (incl. PO token status) never reach the logger at all
            "logger": log_capture,
            "progress_hooks": [lambda d: _progress_hook(job_id, d, progress_state)],
            "extractor_args": YOUTUBE_EXTRACTOR_ARGS,
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
            # premiere_compat no longer restricts the format selection here -
            # always grab the best available, then check the actual codec
            # after downloading and re-encode only if it turns out to
            # matter (see the premiere_compat block after the download).
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

        # Resolved *before* the job is marked "finished" (and committed
        # together with it below) so a poll landing right after "finished"
        # appears can never see it without auto_convert_id already set -
        # otherwise the frontend would briefly think the raw, incompatible
        # file is the final result and auto-download that instead.
        auto_convert_id = None
        if job.mode != "audio" and job.premiere_compat:
            # Deferred import: converter.py imports parse_timecode from this
            # module, so importing it back at module load time would be
            # circular - by the time this actually runs, both modules are
            # already fully loaded, so a call-time import resolves fine.
            from . import converter
            probed = converter.probe_input(filepath)
            if probed and not converter.is_premiere_compatible(probed["vcodec"], probed["acodec"]):
                job.filepath = filepath  # not committed yet - the conversion needs the real path to copy from
                try:
                    auto_convert_id = converter.submit_conversion_from_download(job)
                except Exception:
                    auto_convert_id = None

        _update(
            db, job,
            status="finished",
            progress=100.0,
            eta_seconds=None,
            title=title,
            filepath=filepath,
            filesize=filesize,
            auto_convert_id=auto_convert_id,
            finished_at=datetime.utcnow(),
        )
    except Exception as e:
        if job_id in _cancel_requested:
            _update(db, job, status="cancelled", eta_seconds=None, finished_at=datetime.utcnow())
        else:
            # The exception text alone is often just "This video is not
            # available" with no clue why - yt-dlp's own debug/warning
            # trail (captured above via the logger, since quiet=True would
            # otherwise send it nowhere) usually says a lot more, including
            # whether the PO token provider was even reached.
            detail = str(e)
            if log_capture.lines:
                detail += "\n---\n" + "\n".join(log_capture.lines[-15:])
            _update(db, job, status="error", eta_seconds=None, error_message=detail[:2000], finished_at=datetime.utcnow())
        # Partial output from an aborted download shouldn't linger forever -
        # let the cleanup path treat it the same as any other dead job.
        out_dir = os.path.join(config.DOWNLOAD_DIR, job_id)
        if job_id in _cancel_requested and os.path.isdir(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
    finally:
        _cancel_requested.discard(job_id)
        _gate.release()
        db.close()
