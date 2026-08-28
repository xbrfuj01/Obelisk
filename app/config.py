import os

# The only two things that stay as env vars: they are Docker volume mount
# points, not application settings, so they belong to the deployment, not
# the admin panel.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Everything below is a first-run default only. All of it is stored in the
# database once the app starts and from then on is edited from the admin
# panel — docker-compose.yml doesn't need to set any of it.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"
DEFAULT_SITE_USERNAME = "user"
DEFAULT_CLEANUP_HOURS = 24
DEFAULT_CLEANUP_INTERVAL_MINUTES = 30
DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
DEFAULT_SESSION_MAX_AGE_DAYS = 30
DEFAULT_PROXY_DOMAINS = "vk.com,vk.ru,vkvideo.ru,ok.ru,rutube.ru,mail.ru"
