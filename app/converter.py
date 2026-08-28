import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import auth
from .database import SessionLocal
from .downloader import parse_timecode
from .models import Conversion

_executor = ThreadPoolExecutor(max_workers=16)

QUALITY_PRESETS = {
    "high": {"crf": 18, "preset": "slow"},
    "medium": {"crf": 23, "preset": "medium"},
    "low": {"crf": 28, "preset": "fast"},
}

PROGRESS_KEYS = {
    "frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms",
    "out_time", "dup_frames", "drop_frames", "speed", "progress",
}


class _ConcurrencyGate:
    """Caps how many conversions actually run at once, against a limit that
    can change at runtime (read fresh from the DB on every acquire). Separate
    from the downloader's gate — encoding is CPU-bound, downloading is
    mostly I/O-bound, so mixing them into one limit would let one starve
    the other."""

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


def _parse_fps(rate: str):
    try:
        if "/" in rate:
            num, den = rate.split("/")
            den = float(den)
            return float(num) / den if den else None
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return None


def _run_ffprobe(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def probe_input(path):
    """Reads container/codec/resolution info via ffprobe. Returns None if
    ffprobe can't read the file or it has no video stream at all."""
    data = _run_ffprobe(path)
    if not data:
        return None

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        return None

    duration = None
    for candidate in (fmt.get("duration"), video.get("duration")):
        try:
            if candidate:
                duration = float(candidate)
                break
        except (TypeError, ValueError):
            continue

    fps = _parse_fps(video.get("r_frame_rate") or "") or 30.0
    width = video.get("width")
    height = video.get("height")
    container = (fmt.get("format_name") or "").split(",")[0].upper()
    vcodec = (video.get("codec_name") or "").upper()
    acodec = (audio.get("codec_name") or "").upper() if audio else None

    parts = [container or "?"]
    if width and height:
        parts.append(f"{width}×{height}")
    if vcodec:
        parts.append(vcodec)
    if fps:
        fps_label = f"{fps:.2f}".rstrip("0").rstrip(".")
        parts.append(f"{fps_label} fps")
    parts.append(f"аудіо {acodec}" if acodec else "без звуку")
    if duration:
        m, s = divmod(int(duration), 60)
        h, m = divmod(m, 60)
        parts.append(f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}")

    return {
        "summary": " · ".join(parts),
        "duration": duration,
        "fps": fps,
        "has_audio": audio is not None,
    }


def _build_ffmpeg_cmd(input_path, output_path, quality, audio_option, fps, has_audio):
    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["medium"])
    # A keyframe roughly every second (rather than the long GOPs typical of
    # camera/phone/web footage) is what actually makes H.264 smooth to
    # scrub and multicam-sync in Premiere - as much a part of "Premiere
    # compatibility" as the codec choice itself.
    gop = max(1, round(fps)) if fps else 30

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-map", "0:v:0",
        "-c:v", "libx264",
        "-preset", preset["preset"],
        "-crf", str(preset["crf"]),
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", "4.2",
        "-g", str(gop),
        "-keyint_min", str(gop),
    ]

    if audio_option == "none" or not has_audio:
        cmd.append("-an")
    else:
        cmd += ["-map", "0:a:0"]
        if audio_option == "original":
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k"]

    cmd += ["-movflags", "+faststart", "-loglevel", "error", "-progress", "pipe:1", "-nostats", output_path]
    return cmd


def _run_ffmpeg(cmd, job_id, duration):
    db = SessionLocal()
    log_tail = []
    last_commit = 0.0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            key, sep, value = line.partition("=")
            if sep and key in PROGRESS_KEYS:
                # ffmpeg's -progress can report multiple times a second (up
                # to once per frame) - throttle DB writes to a couple times a
                # second instead of hammering SQLite on every line.
                now = time.time()
                if key == "out_time" and duration and now - last_commit >= 0.5:
                    seconds = parse_timecode(value)
                    if seconds is not None:
                        job = db.get(Conversion, job_id)
                        if job:
                            job.progress = round(min(99.0, seconds / duration * 100), 1)
                            job.status = "converting"
                            db.commit()
                    last_commit = now
            else:
                log_tail.append(line)
                del log_tail[:-20]
        proc.wait()
        return proc.returncode, "\n".join(log_tail)
    finally:
        db.close()


def _run_job(job_id: str, input_path: str, info: dict):
    db = SessionLocal()
    job = db.get(Conversion, job_id)
    if not job:
        db.close()
        return

    limit = auth.get_max_concurrent_conversions(db)
    _gate.acquire(limit)
    try:
        job.status = "converting"
        db.commit()

        job_dir = os.path.dirname(input_path)
        base = os.path.splitext(job.original_filename or "video")[0]
        base = re.sub(r"[^\w\-. ]", "_", base).strip(" .") or "video"
        output_path = os.path.join(job_dir, f"{base}_premiere.mp4")

        has_audio = info.get("has_audio", True)
        fps = info.get("fps")
        duration = info.get("duration")

        cmd = _build_ffmpeg_cmd(input_path, output_path, job.quality, job.audio_option, fps, has_audio)
        returncode, log_tail = _run_ffmpeg(cmd, job_id, duration)

        if returncode != 0 and job.audio_option == "original":
            # Stream-copying the source audio into an MP4 container can fail
            # outright for codecs MP4 can't hold (e.g. Vorbis/Opus from a
            # webm source) - fall back to a real AAC re-encode once instead
            # of just surfacing an error for something we can recover from.
            cmd = _build_ffmpeg_cmd(input_path, output_path, job.quality, "aac", fps, has_audio)
            returncode, log_tail = _run_ffmpeg(cmd, job_id, duration)

        if returncode != 0 or not os.path.exists(output_path):
            raise RuntimeError(log_tail[-500:] if log_tail else "Помилка конвертації ffmpeg")

        job.status = "finished"
        job.progress = 100.0
        job.filepath = output_path
        job.filesize = os.path.getsize(output_path)
        job.finished_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        job.status = "error"
        job.error_message = str(e)[:500]
        job.finished_at = datetime.utcnow()
        db.commit()
    finally:
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except OSError:
                pass
        _gate.release()
        db.close()


def submit_job(job_id: str, input_path: str, info: dict):
    _executor.submit(_run_job, job_id, input_path, info)
