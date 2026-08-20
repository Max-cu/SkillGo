FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

RUN groupadd --gid 10001 skill \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin skill \
    && mkdir -p /skill/src /skill/tests /input /output /workspace /run/skill \
    && chown -R 10001:10001 /skill /input /output /workspace /run/skill

COPY --chown=10001:10001 SKILL.md manifest.yaml /skill/
COPY --chown=10001:10001 src/ /skill/src/
COPY --chown=10001:10001 test-contract.json /skill/tests/input.json

USER 10001:10001
WORKDIR /skill

CMD ["tail", "-f", "/dev/null"]
