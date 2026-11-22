FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY worker/ /app/
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m py_compile engine.py render_clip.py highlight_engine.py transcribe_audio.py daemon.py
ENTRYPOINT ["python", "daemon.py"]