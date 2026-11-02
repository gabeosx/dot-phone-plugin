FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.13.18-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TAC_ANALYTICS_DISABLED=true
WORKDIR /app
COPY pyproject.toml uv.lock LICENSE NOTICE ./
COPY dots_phone ./dots_phone
COPY vendor ./vendor
COPY tests ./tests
COPY scripts ./scripts
COPY evals ./evals
COPY plugins/dots-phone ./plugins/dots-phone
COPY experiments/pipecat/*.py ./experiments/pipecat/
COPY experiments/pipecat/text_eval_worker.mjs experiments/pipecat/codex_call_reasoner.mjs experiments/pipecat/subscription_app_server.mjs ./experiments/pipecat/
RUN uv sync --frozen --all-groups && groupadd --gid 10001 phone && useradd --uid 10001 --gid phone --no-create-home phone && mkdir -p /data /artifacts && chown phone:phone /data /artifacts
ENV PATH="/app/.venv/bin:$PATH" DOTS_PHONE_DATABASE=/data/phone.sqlite3
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1
CMD ["python", "-m", "dots_phone"]
