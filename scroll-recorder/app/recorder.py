import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import sync_playwright

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

VIEWPORTS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}

SPEEDS = {
    "slow": 60,
    "medium": 150,
    "fast": 300,
}

# How much wall-clock time each scroll step targets. The real interval ends
# up longer than this once screenshot/evaluate overhead is added - that's
# fine, since the eventual ffmpeg -framerate is derived from how many frames
# actually got captured over how much real time elapsed (see _record), not
# from this constant, so the video's playback speed stays correct regardless
# of how fast a given run could capture frames.
TARGET_TICK_SECONDS = 0.1

# Hard ceiling on a single recording, checked cooperatively inside the scroll
# loop - guards against a page whose height keeps growing (infinite scroll)
# or that never reaches its own bottom. Playwright's own per-call timeouts
# (goto/screenshot) separately guard against a page that just hangs outright.
JOB_TIMEOUT_SECONDS = 600

RETENTION_SECONDS = 2 * 3600
CLEANUP_INTERVAL_SECONDS = 600

_executor = ThreadPoolExecutor(max_workers=2)
_jobs = {}
_jobs_lock = threading.Lock()
_cancel_requested = set()


def create_job(url: str, aspect_ratio: str, speed: str) -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "progress": 0.0, "error": None}
    _executor.submit(_run_job, job_id, url, aspect_ratio, speed)
    return job_id


def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def request_cancel(job_id: str):
    _cancel_requested.add(job_id)


def job_file_path(job_id: str):
    job = get_job(job_id)
    if not job or job.get("status") != "finished":
        return None
    return os.path.join(DATA_DIR, job_id, "output.mp4")


def _set_status(job_id, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _run_job(job_id, url, aspect_ratio, speed):
    job_dir = os.path.join(DATA_DIR, job_id)
    frames_dir = os.path.join(job_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    try:
        _set_status(job_id, status="recording")
        frame_count, fps = _record(job_id, frames_dir, url, aspect_ratio, speed)

        if job_id in _cancel_requested:
            _set_status(job_id, status="cancelled")
            return
        if frame_count == 0:
            _set_status(job_id, status="error", error="Не вдалося захопити жодного кадру")
            return

        _set_status(job_id, status="encoding")
        out_path = os.path.join(job_dir, "output.mp4")
        _encode(frames_dir, out_path, fps)
        shutil.rmtree(frames_dir, ignore_errors=True)
        _set_status(job_id, status="finished", progress=100.0)
    except Exception as exc:
        _set_status(job_id, status="error", error=str(exc))
    finally:
        _cancel_requested.discard(job_id)


def _record(job_id, frames_dir, url, aspect_ratio, speed):
    width, height = VIEWPORTS[aspect_ratio]
    px_per_tick = max(1, round(SPEEDS[speed] * TARGET_TICK_SECONDS))
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS

    frame_index = 0
    start = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(url, wait_until="load", timeout=60000)
            page.wait_for_timeout(500)  # let late-loading content/lazy images settle

            scroll_height = page.evaluate("document.documentElement.scrollHeight")

            while time.monotonic() < deadline:
                if job_id in _cancel_requested:
                    break

                page.screenshot(path=os.path.join(frames_dir, f"frame_{frame_index:06d}.png"))
                frame_index += 1

                scroll_y = page.evaluate("window.scrollY")
                remaining = max(1, scroll_height - height)
                progress = min(99.0, scroll_y / remaining * 100) if scroll_height > height else 100.0
                _set_status(job_id, progress=round(progress, 1))

                if scroll_y + height >= scroll_height:
                    break

                page.evaluate(f"window.scrollBy(0, {px_per_tick})")
                # re-measure in case lazy-loaded content grew the page
                scroll_height = max(scroll_height, page.evaluate("document.documentElement.scrollHeight"))
        finally:
            browser.close()

    elapsed = max(0.001, time.monotonic() - start)
    achieved_fps = max(1, min(60, round(frame_index / elapsed)))
    return frame_index, achieved_fps


def _encode(frames_dir, out_path, fps):
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", os.path.join(frames_dir, "frame_%06d.png"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed: {result.stderr[-2000:]}")


def _cleanup_loop():
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        now = time.time()
        try:
            names = os.listdir(DATA_DIR)
        except OSError:
            continue
        for name in names:
            path = os.path.join(DATA_DIR, name)
            if not os.path.isdir(path):
                continue
            try:
                if now - os.path.getmtime(path) > RETENTION_SECONDS:
                    shutil.rmtree(path, ignore_errors=True)
                    with _jobs_lock:
                        _jobs.pop(name, None)
            except OSError:
                continue


threading.Thread(target=_cleanup_loop, daemon=True).start()
