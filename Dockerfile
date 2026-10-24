# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.22 AS uv
FROM denoland/deno:bin-2.9.7 AS deno

FROM python:3.14.7-slim-trixie AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md .python-version ./
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.14.7-slim-trixie AS runtime

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends adduser ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --gecos "" --shell /usr/sbin/nologin app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    XDG_CACHE_HOME=/home/app/.cache \
    PATH="/app/.venv/bin:$PATH"

COPY --from=deno /deno /usr/local/bin/deno
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app bot.py /app/bot.py
COPY --chown=app:app src /app/src

WORKDIR /app
USER app

CMD ["python", "bot.py"]
