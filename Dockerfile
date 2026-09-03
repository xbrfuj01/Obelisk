FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl make cpanminus \
    && rm -rf /var/lib/apt/lists/*

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
    && pip install --no-cache-dir --upgrade yt-dlp bgutil-ytdlp-pot-provider \
    && cpanm --notest Image::ExifTool

COPY app ./app

RUN mkdir -p /data /downloads

EXPOSE 30301

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-30301} --no-access-log"]
