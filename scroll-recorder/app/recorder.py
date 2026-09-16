import base64
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

VIEWPORTS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}

FRAME_RATES = {30, 60}

DEVICE_MODES = {"desktop", "mobile"}

# Real per-CSS-pixel-width breakpoints mean a genuinely narrow viewport is
# what actually triggers a site's mobile layout - not just an is_mobile
# flag on a 1080-1920px-wide "desktop-shaped" viewport, which most sites'
# media queries would still treat as a large desktop/tablet. So "phone
# mode" uses Playwright's own verified "Pixel 7" device descriptor
# (viewport/UA/deviceScaleFactor/isMobile/hasTouch bundled together, exact
# values confirmed against playwright-core 1.47.0's own
# deviceDescriptorsSource.json) instead of trying to force a phone-like
# render into one of the aspect_ratio presets above - the two goals
# (accurate mobile rendering vs. an arbitrary chosen video frame shape)
# don't both fit at once, so mobile mode intentionally ignores
# aspect_ratio and just records at the phone's own natural shape.
MOBILE_DEVICE_NAME = "Pixel 7"
# Mirrors that same descriptor's viewport, only for reporting size to the
# preview frontend before a real browser/Playwright driver exists yet.
MOBILE_VIEWPORT = (412, 839)

# A storage/resource guard (this project's NVMe "apps" pool is small, see
# project-homelab-infra memory) - also the deadline _scroll_and_capture
# paces itself against, replacing the old fixed speed presets.
MIN_DURATION_SECONDS = 3
MAX_DURATION_SECONDS = 300

# Playwright's default headless Chromium UA can still read as automated to
# some checks - a plain modern desktop Chrome UA is a cheap, honest-effort
# improvement alongside playwright-stealth below. (Mobile mode gets its UA
# from the Pixel 7 device descriptor instead, see above.)
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# A small curated list, not a full EasyList-format filter engine (parsing
# AdBlock Plus filter syntax is a much bigger, separately-maintained
# problem) - good enough for a personal recording tool. Registered on the
# page before goto so it also covers the initial page load's own ad
# requests, not just ones fired after.
AD_BLOCK_DOMAINS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "google-analytics.com", "googletagmanager.com", "adnxs.com",
    "amazon-adsystem.com", "criteo.com", "criteo.net", "taboola.com",
    "outbrain.com", "pubmatic.com", "rubiconproject.com", "openx.net",
    "adform.net", "media.net", "revcontent.com", "mgid.com",
    "scorecardresearch.com", "quantserve.com", "moatads.com",
    "adsafeprotected.com", "bidswitch.net", "casalemedia.com",
    "smartadserver.com", "yieldmo.com", "sharethrough.com",
)

# Cosmetic fallback for ad markup the network block doesn't catch (already
# server-rendered, or served from a first-party path). Applied once after
# the initial settle wait, before any frame is ever captured.
AD_BLOCK_CSS = """
[id*="google_ads"], [id^="div-gpt-ad"], ins.adsbygoogle,
[class*="adsbygoogle"], iframe[src*="doubleclick"],
[class*="ad-container"], [class*="ad-slot"], [class*="advert"],
[id*="taboola"], [id*="outbrain"], [class*="sponsored-content"] {
  display: none !important;
}
"""

# How much wall-clock time each scroll step targets - recomputed every tick
# against the real remaining time/distance (see _scroll_and_capture), so a
# slow real tick (screenshot/evaluate overhead) just speeds up the pace of
# the next one rather than throwing off the requested total duration.
TARGET_TICK_SECONDS = 0.1

RETENTION_SECONDS = 2 * 3600
CLEANUP_INTERVAL_SECONDS = 600

# A preview session holds a live headless Chromium instance idle while the
# user picks elements to remove - cap how many can be open at once (each is
# a real browser process) and close ones nobody's touched in a while so an
# abandoned picker tab doesn't leak a browser forever.
MAX_PREVIEW_SESSIONS = 3
PREVIEW_IDLE_SECONDS = 600

_executor = ThreadPoolExecutor(max_workers=2)
_jobs = {}
_jobs_lock = threading.Lock()
_cancel_requested = set()

_previews = {}
_previews_lock = threading.Lock()


def create_job(
    url: str, aspect_ratio: str, device: str, duration_seconds: int, framerate: int,
    block_ads: bool = False, proxy_url: str = None,
) -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "progress": 0.0, "error": None}
    _executor.submit(
        _run_job, job_id, url, aspect_ratio, device, duration_seconds, framerate, block_ads, proxy_url
    )
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


def _parse_proxy(proxy_url: str):
    """Converts a yt-dlp-style proxy URL (the same string the admin's
    "Проксі для заблокованих сайтів" setting already stores, allowing
    socks5h/socks4a schemes and embedded user:pass@) into Playwright's
    proxy launch-option shape - which wants credentials as separate fields,
    not embedded in the server URL, and doesn't recognize the h/a
    DNS-resolution suffixes."""
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme in ("socks5", "socks5h"):
        scheme = "socks5"
    elif scheme in ("socks4", "socks4a"):
        scheme = "socks4"
    elif scheme not in ("http", "https"):
        scheme = "http"

    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"

    proxy = {"server": server}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _apply_ad_block(page):
    def _route_handler(route):
        if any(domain in route.request.url for domain in AD_BLOCK_DOMAINS):
            route.abort()
        else:
            route.continue_()

    page.route("**/*", _route_handler)


def _hide_ad_containers(page):
    page.add_style_tag(content=AD_BLOCK_CSS)


def _screenshot_b64(page) -> str:
    return base64.b64encode(page.screenshot()).decode("ascii")


def _prepare_page(p, browser, url, aspect_ratio, device, block_ads):
    if device == "mobile":
        page = browser.new_page(**p.devices[MOBILE_DEVICE_NAME])
    else:
        width, height = VIEWPORTS[aspect_ratio]
        page = browser.new_page(viewport={"width": width, "height": height}, user_agent=DESKTOP_USER_AGENT)
    stealth_sync(page)
    if block_ads:
        _apply_ad_block(page)
    page.goto(url, wait_until="load", timeout=60000)
    page.wait_for_timeout(500)  # let late-loading content/lazy images settle
    if block_ads:
        _hide_ad_containers(page)
    return page


def _scroll_and_capture(job_id, page, frames_dir, duration_seconds):
    height = page.viewport_size["height"]
    deadline = time.monotonic() + duration_seconds

    frame_index = 0
    start = time.monotonic()
    state = page.evaluate(
        "() => ({y: window.scrollY, h: document.documentElement.scrollHeight})"
    )
    scroll_y, scroll_height = state["y"], state["h"]

    while True:
        if job_id in _cancel_requested:
            break

        now = time.monotonic()
        if now >= deadline:
            break

        # JPEG instead of PNG, and one combined evaluate (scroll + read)
        # per tick instead of three separate round-trips - both cut the
        # real per-tick latency, which is what actually caps how many
        # unique frames fit in duration_seconds (there's no artificial
        # delay in this loop; it just runs flat-out). More real frames
        # with a smaller shift between each is what makes the output look
        # like a smooth manual scroll instead of an interpolated blur.
        page.screenshot(path=os.path.join(frames_dir, f"frame_{frame_index:06d}.jpg"), type="jpeg", quality=85)
        frame_index += 1

        total_scrollable = max(1, scroll_height - height)
        progress = min(99.0, scroll_y / total_scrollable * 100) if scroll_height > height else 100.0
        _set_status(job_id, progress=round(progress, 1))

        remaining_px = max(0, scroll_height - height - scroll_y)
        if remaining_px <= 0:
            break

        # Recomputed every tick against the real clock, so a slower-than-
        # expected tick (or scroll_height growing from lazy-loaded content)
        # both get absorbed into the pace of what's left, instead of
        # finishing off-target from a pace fixed once at the start.
        remaining_time = max(TARGET_TICK_SECONDS, deadline - now)
        px_per_tick = max(1, round(remaining_px * TARGET_TICK_SECONDS / remaining_time))

        state = page.evaluate(
            """(dy) => {
                window.scrollBy(0, dy);
                return {y: window.scrollY, h: document.documentElement.scrollHeight};
            }""",
            px_per_tick,
        )
        scroll_y = state["y"]
        # re-measure in case lazy-loaded content grew the page
        scroll_height = max(scroll_height, state["h"])

    elapsed = max(0.001, time.monotonic() - start)
    input_fps = max(1, min(60, round(frame_index / elapsed)))
    return frame_index, input_fps


def _encode(frames_dir, out_path, input_fps, output_fps):
    # Plain frame-rate conversion (duplicate/drop as needed to reach
    # output_fps), not a blending/interpolation filter - the latter reads
    # as a blurry "swimming" ghosting artifact instead of a clean scroll.
    # With _scroll_and_capture now aiming for as many genuinely distinct
    # frames as the real capture rate allows, this is meant to look like a
    # real many-small-steps scroll, not a smoothed-over one.
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(input_fps),
        "-i", os.path.join(frames_dir, "frame_%06d.jpg"),
        "-r", str(output_fps),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed: {result.stderr[-2000:]}")


def _finalize_recording(job_id, job_dir, frames_dir, frame_count, input_fps, output_fps):
    if job_id in _cancel_requested:
        _set_status(job_id, status="cancelled")
        return
    if frame_count == 0:
        _set_status(job_id, status="error", error="Не вдалося захопити жодного кадру")
        return

    _set_status(job_id, status="encoding")
    out_path = os.path.join(job_dir, "output.mp4")
    _encode(frames_dir, out_path, input_fps, output_fps)
    shutil.rmtree(frames_dir, ignore_errors=True)
    _set_status(job_id, status="finished", progress=100.0)


def _run_job(job_id, url, aspect_ratio, device, duration_seconds, framerate, block_ads, proxy_url):
    job_dir = os.path.join(DATA_DIR, job_id)
    frames_dir = os.path.join(job_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    try:
        _set_status(job_id, status="recording")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=_parse_proxy(proxy_url))
            try:
                page = _prepare_page(p, browser, url, aspect_ratio, device, block_ads)
                frame_count, input_fps = _scroll_and_capture(job_id, page, frames_dir, duration_seconds)
            finally:
                browser.close()
        _finalize_recording(job_id, job_dir, frames_dir, frame_count, input_fps, framerate)
    except Exception as exc:
        _set_status(job_id, status="error", error=str(exc))
    finally:
        _cancel_requested.discard(job_id)


def _finish_job_on_page(job_id, page, duration_seconds, framerate):
    """Same tail as _run_job, but reuses an already-live page (from a
    preview session) instead of launching a fresh browser - whatever
    ad-block routes/hidden elements are already on the page just carry
    over, nothing needs to be replayed."""
    job_dir = os.path.join(DATA_DIR, job_id)
    frames_dir = os.path.join(job_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    try:
        _set_status(job_id, status="recording")
        frame_count, input_fps = _scroll_and_capture(job_id, page, frames_dir, duration_seconds)
        _finalize_recording(job_id, job_dir, frames_dir, frame_count, input_fps, framerate)
    except Exception as exc:
        _set_status(job_id, status="error", error=str(exc))
    finally:
        _cancel_requested.discard(job_id)


class PreviewSession:
    """One dedicated worker thread per session, owning one live Playwright
    page. Required because Playwright's sync API is not thread-safe across
    threads - a page/browser must only ever be touched from the thread that
    created it. Every action (a click, an undo, or the final recording) is
    submitted as a callable through an internal queue and run on that
    thread; callers block on a Future to get the result back."""

    def __init__(
        self, session_id: str, url: str, aspect_ratio: str, device: str,
        block_ads: bool, proxy_url: str = None,
    ):
        self.id = session_id
        self.width, self.height = MOBILE_VIEWPORT if device == "mobile" else VIEWPORTS[aspect_ratio]
        self.last_active = time.monotonic()
        self._queue = queue.Queue()
        self._state_lock = threading.Lock()
        self._removed_stack = []
        self._removed_counter = 0

        ready = Future()
        self._thread = threading.Thread(
            target=self._run, args=(url, aspect_ratio, device, block_ads, proxy_url, ready), daemon=True
        )
        self._thread.start()
        self.screenshot_b64 = ready.result(timeout=65)

    def _run(self, url, aspect_ratio, device, block_ads, proxy_url, ready):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, proxy=_parse_proxy(proxy_url))
                try:
                    page = _prepare_page(p, browser, url, aspect_ratio, device, block_ads)
                    ready.set_result(_screenshot_b64(page))
                    while True:
                        item = self._queue.get()
                        if item is None:
                            break
                        func, fut, terminal = item
                        try:
                            fut.set_result(func(page))
                        except Exception as exc:
                            fut.set_exception(exc)
                        if terminal:
                            break
                finally:
                    browser.close()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)

    def call(self, func, timeout=30):
        self.last_active = time.monotonic()
        fut = Future()
        self._queue.put((func, fut, False))
        return fut.result(timeout=timeout)

    def call_terminal(self, func):
        """Submits a long-running action (the actual recording) without
        waiting for it - the thread closes its own browser and exits once
        func returns, same lifecycle as a normal direct-record job."""
        self.last_active = time.monotonic()
        fut = Future()
        self._queue.put((func, fut, True))

    def close(self):
        self._queue.put(None)

    def next_removed_index(self):
        with self._state_lock:
            self._removed_counter += 1
            return self._removed_counter

    def push_removed(self, idx):
        with self._state_lock:
            self._removed_stack.append(idx)

    def pop_removed(self):
        with self._state_lock:
            return self._removed_stack.pop() if self._removed_stack else None


def _get_preview(session_id: str) -> PreviewSession:
    with _previews_lock:
        session = _previews.get(session_id)
    if not session:
        raise KeyError(session_id)
    return session


def create_preview(url: str, aspect_ratio: str, device: str, block_ads: bool, proxy_url: str = None):
    with _previews_lock:
        if len(_previews) >= MAX_PREVIEW_SESSIONS:
            raise RuntimeError("Забагато активних попередніх переглядів, спробуйте пізніше")

    session_id = uuid.uuid4().hex
    session = PreviewSession(session_id, url, aspect_ratio, device, block_ads, proxy_url)
    with _previews_lock:
        _previews[session_id] = session
    return session_id, session.screenshot_b64, session.width, session.height


def remove_at_point(session_id: str, x: float, y: float) -> str:
    session = _get_preview(session_id)
    idx = session.next_removed_index()

    def action(page):
        removed = page.evaluate(
            """([x, y, idx]) => {
                const el = document.elementFromPoint(x, y);
                if (!el || el === document.body || el === document.documentElement) return false;
                el.setAttribute('data-obelisk-removed', String(idx));
                el.style.setProperty('display', 'none', 'important');
                return true;
            }""",
            [x, y, idx],
        )
        if removed:
            session.push_removed(idx)
        return _screenshot_b64(page)

    return session.call(action)


def undo_last(session_id: str) -> str:
    session = _get_preview(session_id)
    idx = session.pop_removed()

    def action(page):
        if idx is not None:
            page.evaluate(
                """(idx) => {
                    const el = document.querySelector(`[data-obelisk-removed="${idx}"]`);
                    if (el) {
                        el.style.removeProperty('display');
                        el.removeAttribute('data-obelisk-removed');
                    }
                }""",
                idx,
            )
        return _screenshot_b64(page)

    return session.call(action)


def start_recording_from_preview(session_id: str, duration_seconds: int, framerate: int) -> str:
    with _previews_lock:
        session = _previews.pop(session_id, None)
    if not session:
        raise KeyError(session_id)

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "progress": 0.0, "error": None}

    session.call_terminal(lambda page: _finish_job_on_page(job_id, page, duration_seconds, framerate))
    return job_id


def close_preview(session_id: str):
    with _previews_lock:
        session = _previews.pop(session_id, None)
    if session:
        session.close()


def _cleanup_loop():
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)

        now = time.time()
        try:
            names = os.listdir(DATA_DIR)
        except OSError:
            names = []
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

        now_monotonic = time.monotonic()
        with _previews_lock:
            stale_ids = [
                sid for sid, session in _previews.items()
                if now_monotonic - session.last_active > PREVIEW_IDLE_SECONDS
            ]
            stale_sessions = [_previews.pop(sid) for sid in stale_ids]
        for session in stale_sessions:
            session.close()


threading.Thread(target=_cleanup_loop, daemon=True).start()
