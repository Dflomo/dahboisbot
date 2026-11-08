FROM python:3.12-slim

# ffmpeg converts the audio for Discord
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime to read YouTube pages reliably
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
# Unpinned on purpose: every redeploy pulls the newest yt-dlp, which YouTube changes often break
RUN pip install --no-cache-dir --upgrade -r requirements.txt

COPY bot.py .
CMD ["python", "-u", "bot.py"]
