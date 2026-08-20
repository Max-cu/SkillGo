FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 skillgo \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin skillgo \
    && mkdir -p /app /var/lib/skillgo/storage \
    && chown -R 10001:10001 /app /var/lib/skillgo

WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir -r /app/requirements.lock
COPY --chown=10001:10001 app /app/app

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
