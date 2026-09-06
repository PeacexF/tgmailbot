FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable


FROM python:3.14-slim

RUN groupadd --gid 10001 mailbridge \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin mailbridge

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/mailbridge.db

RUN install -d -o mailbridge -g mailbridge /data
VOLUME ["/data"]

RUN install -d -o root -g root -m 0755 /app
WORKDIR /app

USER mailbridge

ENTRYPOINT ["mailbridge"]

# No HEALTHCHECK on purpose. The only self-check the bridge offers, --check,
# validates configuration and never touches the network, so it would report
# healthy on a wedged daemon.