import os

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

# Optional login gate for the whole public site (separate from the admin
# login). Empty password = disabled (site stays open). Can also be
# set/changed later from the admin settings page.
SITE_USERNAME = os.environ.get("SITE_USERNAME", "user")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")

# How long a successful login (site or admin) is remembered on a device.
SESSION_MAX_AGE_DAYS = int(os.environ.get("SESSION_MAX_AGE_DAYS", "30"))

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")

MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
DEFAULT_CLEANUP_HOURS = int(os.environ.get("CLEANUP_HOURS", "24"))
CLEANUP_INTERVAL_MINUTES = int(os.environ.get("CLEANUP_INTERVAL_MINUTES", "30"))

# Optional proxy (e.g. socks5://user:pass@host:port) used only for the domains
# listed in PROXY_DOMAINS, so blocked sites can be reached without routing
# everything else (YouTube, Instagram, ...) through it.
PROXY_URL = os.environ.get("PROXY_URL", "")
PROXY_DOMAINS = [
    d.strip().lower()
    for d in os.environ.get("PROXY_DOMAINS", "vk.com,vk.ru,vkvideo.ru,ok.ru,rutube.ru,mail.ru").split(",")
    if d.strip()
]

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
