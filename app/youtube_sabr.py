"""Experimental YouTube download engine using a separate, SABR-capable
yt-dlp fork (github.com/coletdjnz/yt-dlp-dev, feat/youtube/sabr branch —
see yt-dlp/yt-dlp#13515). That fork actually speaks YouTube's newer SABR
streaming protocol instead of working around it via player-client
selection, which is what the stable yt-dlp Obelisk otherwise uses still
struggles with (see downloader.py's YOUTUBE_EXTRACTOR_ARGS comment).

It registers as the same "yt_dlp" package as stable yt-dlp, so it can't be
pip-installed into the main environment without replacing it - it's built
into its own venv (see Dockerfile) and driven here as a CLI subprocess
instead of a Python import. Only handles plain, non-clip video+audio
downloads (see downloader.py's _should_use_sabr_engine) - the fork itself
doesn't yet support --download-sections, so clips/"лише відео"/"лише
аудіо" always stay on the stable engine regardless of this module.
"""

import re
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit

from .database import SessionLocal
from .models import Download

SABR_YTDLP_BIN = "/opt/venv-sabr/bin/yt-dlp"

# Matches yt-dlp's own --newline progress lines, e.g.:
#   [download]  45.2% of   10.00MiB at    1.23MiB/s ETA 00:07
_PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
_ETA_RE = re.compile(r"ETA\s+([\d:]+)")

# How often a progress line is allowed to actually hit the database - same
# reasoning as downloader.py's PROGRESS_DB_WRITE_INTERVAL_SECONDS.
_PROGRESS_WRITE_INTERVAL_SECONDS = 0.5

# How many lines of subprocess output to keep for an error message if the
# download fails - mirrors _YdlLogCapture.summary()'s default budget.
_ERROR_TAIL_LINES = 60


def _redact_command(cmd: list) -> str:
    """For the error-message tail only - the proxy URL can carry a
    username:password (see the microsocks setup in admin settings), which
    has no business ending up in a Download row's error_message."""
    parts = []
    for arg in cmd:
        split = urlsplit(arg)
        if split.scheme and split.password:
            port = f":{split.port}" if split.port else ""
            netloc = f"{split.username}:***@{split.hostname or ''}{port}"
            arg = urlunsplit(split._replace(netloc=netloc))
        parts.append(arg)
    return " ".join(parts)


def _parse_eta(text: str):
    try:
        parts = [int(p) for p in text.strip().split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def _build_command(url, outtmpl, height_filter, container, cookies_path, proxy_url, po_token=None):
    cmd = [
        SABR_YTDLP_BIN,
        "--no-warnings",
        "--verbose",
        "--newline",
        "--extractor-args", "youtube:formats=duplicate;player-client=web,web_safari,tv,ios",
        "--extractor-args", "youtubepot-bgutilhttp:base_url=http://bgutil-provider:4416",
        "-f", f"bv[protocol=sabr]{height_filter}+ba[protocol=sabr]/best{height_filter}/best",
        "-o", outtmpl,
    ]
    if container:
        cmd += ["--merge-output-format", container]
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    if proxy_url:
        cmd += ["--proxy", proxy_url]
    if po_token:
        # A real, browser-minted token from the Obelisk Bridge extension
        # (see downloader.py's _wait_for_extension_token) - the "web.gvs+"
        # prefix is yt-dlp's own convention for a GVS PO token scoped to
        # the "web" client; may need adjusting once tested against an
        # actual captured token.
        cmd += ["--extractor-args", f"youtube:po_token=web.gvs+{po_token}"]
    cmd.append(url)
    return cmd


def download_via_sabr(
    *, url, out_dir, outtmpl, height_filter, container, cookies_path, proxy_url,
    job_id, should_cancel, po_token=None,
):
    """Runs the SABR-fork yt-dlp as a subprocess. Returns (filepath, error) -
    exactly one of which is None. Progress is written straight to the
    Download row as it's parsed, same fields _progress_hook uses, so the
    existing /api/status polling needs no changes to see it."""
    cmd = _build_command(url, outtmpl, height_filter, container, cookies_path, proxy_url, po_token)
    state = {"last_write": 0.0}

    def handle_line(line: str):
        match = _PROGRESS_RE.search(line)
        if not match:
            return
        now = time.time()
        if now - state["last_write"] < _PROGRESS_WRITE_INTERVAL_SECONDS:
            return
        state["last_write"] = now
        eta_match = _ETA_RE.search(line)
        db = SessionLocal()
        try:
            job = db.get(Download, job_id)
            if not job:
                return
            job.progress = float(match.group(1))
            job.status = "downloading"
            job.eta_seconds = _parse_eta(eta_match.group(1)) if eta_match else None
            db.commit()
        finally:
            db.close()

    proc = subprocess.Popen(
        cmd, cwd=out_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    output_lines = []
    cancelled = False
    try:
        for line in proc.stdout:
            output_lines.append(line.rstrip("\n"))
            if len(output_lines) > _ERROR_TAIL_LINES:
                output_lines.pop(0)
            handle_line(line)
            if should_cancel():
                cancelled = True
                proc.terminate()
                break
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    except Exception:
        proc.kill()
        raise

    if cancelled:
        return None, "cancelled"

    if proc.returncode != 0:
        body = "\n".join(output_lines) or f"yt-dlp-sabr завершився з кодом {proc.returncode}"
        tail = _redact_command(cmd) + "\n---\n" + body
        return None, tail[:4000]

    # Deferred import: downloader.py imports this module, so importing it
    # back at module load time would be circular - by the time this
    # function actually runs, both modules are already fully loaded.
    from .downloader import _find_main_file
    filepath = _find_main_file(out_dir)
    if not filepath:
        return None, "Не вдалося знайти завантажений файл (SABR)"
    return filepath, None
