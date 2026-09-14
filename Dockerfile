FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip make cpanminus git \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime to solve YouTube's player challenge - without
# one it silently falls back to a more restricted client (e.g. "visionos")
# that reports some videos as unplayable even though the normal web client
# handles them fine. Deno is yt-dlp's own default runtime to look for, and
# DENO_INSTALL puts the binary straight on PATH without a manual mv.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp's site extractors (YouTube, TikTok, VK, ...) break and get fixed
# constantly, and ExifTool ships new format/metadata support (C2PA support
# is recent) faster than Debian's packaged libimage-exiftool-perl catches
# up - both get re-pulled to latest on every image build regardless of
# Docker layer cache. CACHEBUST is passed as a unique value per build.
ARG CACHEBUST=1
RUN echo "cachebust=${CACHEBUST}" \
    && pip install --no-cache-dir --upgrade yt-dlp yt-dlp-ejs bgutil-ytdlp-pot-provider \
    && cpanm --notest Image::ExifTool

# Experimental SABR-capable yt-dlp fork (youtube_sabr.py - automatically
# used for eligible YouTube jobs when the Obelisk Bridge extension supplies
# a PO token, see downloader.py's _is_extension_eligible) - it's a fork of
# the same "yt_dlp" package, so it can't be pip-installed into the main
# environment above without clobbering the stable one. Isolated in its own
# venv and invoked as a CLI subprocess instead. Re-pulled every build (same
# CACHEBUST) since it's an actively-changing, unreleased branch.
RUN python -m venv /opt/venv-sabr \
    && echo "cachebust=${CACHEBUST}" \
    && /opt/venv-sabr/bin/pip install --no-cache-dir \
       "yt-dlp[default] @ git+https://github.com/coletdjnz/yt-dlp-dev@feat/youtube/sabr" \
       bgutil-ytdlp-pot-provider

COPY app ./app
# The Obelisk Bridge Chrome extension's source - served as a zip by
# GET /extension/download (main.py), not run in this container itself.
COPY extension ./extension

RUN mkdir -p /data /downloads

EXPOSE 30301

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-30301} --no-access-log"]
