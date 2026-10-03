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

# Node.js + npm, for the Claude Code CLI that recipe_runner.py drives as a
# headless subprocess. The CLI itself is deliberately NOT baked into this
# (public) image: it is proprietary ("All rights reserved", use subject to
# Anthropic's Commercial Terms), so docker-entrypoint.sh installs it from npm
# into the /data volume on first start instead, the same thing a user would do
# by hand, and it persists across restarts. Distro nodejs/npm (not a hard-coded
# x64 tarball) so the multi-arch build also works on arm64.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV CLAUDE_CLI_PREFIX=/data/claude-cli \
    PATH=/data/claude-cli/bin:${PATH}

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ app/
COPY photo-mat-recipe/ photo-mat-recipe/
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh

RUN useradd --create-home --uid 1000 pipeline \
    && mkdir -p /data \
    && chown -R pipeline:pipeline /app /data
USER pipeline

VOLUME ["/data"]
EXPOSE 8080

# start-period covers the first-start Claude CLI install from npm plus the
# first auth probe (up to a 60s subprocess timeout) so the container isn't marked unhealthy before that's had a
# chance to complete; /healthz itself returns 503 while no Claude session
# has been established yet, not just on a web-server-down error.
HEALTHCHECK --interval=30s --timeout=5s --start-period=240s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
