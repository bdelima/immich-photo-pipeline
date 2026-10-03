FROM python:3.12-slim

ARG VERSION=0.0.0-dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="immich-photo-pipeline" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="https://github.com/bdelima/immich-photo-pipeline"

ENV APP_VERSION=${VERSION} \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Headless Claude Code CLI, for the recipe_runner.py subprocess contract.
# NOT VERIFIED in this PR — no sandbox here can authenticate a Claude Pro
# login to actually exercise this, so the package name/install path below
# needs confirming against the real CLI before this image is trusted.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://nodejs.org/dist/v22.11.0/node-v22.11.0-linux-x64.tar.xz \
       | tar -xJ -C /usr/local --strip-components=1 \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ app/
COPY photo-mat-recipe/ photo-mat-recipe/

RUN useradd --create-home --uid 1000 pipeline \
    && mkdir -p /data \
    && chown -R pipeline:pipeline /app /data
USER pipeline

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/albums', timeout=3)" || exit 1

CMD ["python", "-m", "app.main"]
