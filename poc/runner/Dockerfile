FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

RUN groupadd --gid 10002 runner \
    && useradd --uid 10002 --gid 10002 --create-home --shell /usr/sbin/nologin runner

WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir -r /app/requirements.lock

COPY --chown=10002:10002 run_poc.py /app/run_poc.py

USER 10002:10002

ENTRYPOINT ["python", "/app/run_poc.py"]
