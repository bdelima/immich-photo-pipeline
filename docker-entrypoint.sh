#!/bin/sh
# Installs the Claude Code CLI on first start, then runs the app.
#
# The CLI is proprietary ("All rights reserved", use subject to Anthropic's
# Commercial Terms of Service), so it is deliberately NOT baked into this
# image. It is installed from npm into the /data volume the first time the
# container starts and reused on every later start.
#
# Environment:
#   CLAUDE_CLI_PREFIX   where to install it (default /data/claude-cli)
#   CLAUDE_CODE_VERSION npm version/tag to install (default "latest"). Setting
#                       a specific version re-installs when it differs from
#                       what's already installed.
set -eu

PREFIX="${CLAUDE_CLI_PREFIX:-/data/claude-cli}"
WANT="${CLAUDE_CODE_VERSION:-latest}"
MARKER="$PREFIX/.installed-version"
export PATH="$PREFIX/bin:$PATH"

need_install=0
if ! command -v claude >/dev/null 2>&1; then
  need_install=1
elif [ "$WANT" != "latest" ] && [ "$(cat "$MARKER" 2>/dev/null || true)" != "$WANT" ]; then
  need_install=1
fi

if [ "$need_install" -eq 1 ]; then
  echo "immich-photo-pipeline: installing Claude Code CLI (@anthropic-ai/claude-code@$WANT) into $PREFIX (first start, or version changed)..." >&2
  mkdir -p "$PREFIX"
  if npm install -g --prefix "$PREFIX" "@anthropic-ai/claude-code@$WANT" >&2; then
    printf '%s\n' "$WANT" > "$MARKER"
    echo "immich-photo-pipeline: Claude Code CLI installed." >&2
  else
    echo "immich-photo-pipeline: WARNING: could not install the Claude Code CLI (network/npm problem?). The pipeline will report no working Claude session until it is installed; restart the container to retry." >&2
  fi
fi

exec "$@"
