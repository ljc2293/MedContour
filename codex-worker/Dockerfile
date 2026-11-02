FROM python:3.12-slim-bookworm AS python-runtime

# The full Node image already contains Git and CA certificates. Copying the
# self-contained Python runtime into it avoids a network-dependent apt step
# while still giving Codex both its SDK runtime and repository tooling.
FROM node:22-bookworm

COPY --from=python-runtime /usr/local /usr/local

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CODEX_HOME=/home/codex/.codex \
    CODEX_WORKSPACE_ROOT=/workspace

RUN python --version && node --version && git --version

WORKDIR /service
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt \
    && CODEX_BIN="$(python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')" \
    && ln -sf "$CODEX_BIN" /usr/local/bin/codex \
    && codex --version
COPY app.py entrypoint.sh ./

RUN useradd --create-home --shell /bin/bash codex \
    && mkdir -p /workspace /task-files /home/codex/.codex \
    && chmod 755 /service/entrypoint.sh \
    && chown -R codex:codex /service /workspace /task-files /home/codex

USER codex
EXPOSE 8090
ENTRYPOINT ["/service/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
