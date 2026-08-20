FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_PATH=/usr/local/lib/node_modules \
    HOME=/tmp

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git jq nodejs npm unzip wget \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir \
        python-docx==1.2.0 \
        openpyxl==3.1.5 \
        python-pptx==1.0.2 \
        reportlab==4.4.3 \
        pypdf==5.9.0 \
        pdfplumber==0.11.7 \
    && npm install --global docx@9.5.1 \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /workspace /tmp \
    && chown 10001:10001 /workspace /tmp

WORKDIR /workspace
USER 10001:10001
CMD ["sleep", "infinity"]
