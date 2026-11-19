FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY common /srv/common
RUN pip install /srv/common
COPY gateway/requirements.txt /srv/gateway/requirements.txt
RUN pip install -r /srv/gateway/requirements.txt

COPY gateway /srv/gateway
WORKDIR /srv/gateway

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
