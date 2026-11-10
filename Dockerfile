FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Add Facebook Story support that upstream yt-dlp still lacks (issue #5763).
# Exits non-zero if the pinned yt-dlp's anchors have moved, so a future bump
# fails the build instead of silently shipping a half-applied patch.
COPY patches ./patches
RUN python patches/apply_ytdlp_facebook_stories.py
COPY bot ./bot
CMD ["python", "-m", "bot"]
