FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates curl unzip && rm -rf /var/lib/apt/lists/*
# yt-dlp officially requires a supported JS runtime + yt-dlp-ejs for YouTube.
# Pin the upstream Deno binary, and install EJS through yt-dlp[default].
ARG DENO_VERSION=2.5.6
RUN curl --fail --location --silent --show-error --retry 3 \
      "https://dl.deno.land/release/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
      --output /tmp/deno.zip \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version
WORKDIR /app
COPY worker/ /app/
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m py_compile engine.py render_clip.py highlight_engine.py transcribe_audio.py daemon.py
ENTRYPOINT ["python", "daemon.py"]