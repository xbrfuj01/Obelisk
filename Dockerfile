FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp's site extractors (YouTube, TikTok, VK, ...) break and get fixed
# constantly, so every image build re-pulls the latest release regardless
# of Docker layer cache. CACHEBUST is passed as a unique value per build.
ARG CACHEBUST=1
RUN echo "cachebust=${CACHEBUST}" && pip install --no-cache-dir --upgrade yt-dlp

COPY app ./app

RUN mkdir -p /data /downloads

EXPOSE 30301

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-30301} --no-access-log"]
