FROM denoland/deno:bin-2.5.6 AS deno

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# ffmpeg grabs single frames straight from the video stream; yt-dlp needs a JS runtime (Deno)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /srv
COPY common /srv/common
RUN pip install /srv/common
COPY worker/requirements.txt /srv/worker/requirements.txt
RUN pip install -r /srv/worker/requirements.txt

# Speech-to-text fallback for videos without captions: bake the Whisper model into the image
ARG WHISPER_MODEL=base
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8', download_root='/opt/whisper')"

COPY worker /srv/worker
WORKDIR /srv/worker

CMD ["python", "-m", "worker.main"]
