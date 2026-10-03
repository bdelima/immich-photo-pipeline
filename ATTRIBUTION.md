# Attribution

This repository's own code is licensed under the MIT License (see `LICENSE`). It uses or builds on the third-party projects below, each under its own license and copyright; nothing here relicenses them.

## Immich

- **Project:** [immich-app/immich](https://github.com/immich-app/immich)
- **License:** AGPL-3.0
- **How it's used:** the pipeline calls Immich's HTTP API. No Immich code is included or redistributed here.

## Claude Code CLI (Anthropic)

- **Project:** `@anthropic-ai/claude-code`, from Anthropic PBC
- **License:** proprietary ("All rights reserved"; use is subject to Anthropic's Commercial Terms of Service), not this repository's MIT license
- **How it's used:** **not** included in this repository or in the published Docker image. The container's entrypoint installs it from npm into the `/data` volume on first start, and the pipeline runs it as a headless subprocess, the container and CI usage Anthropic's documentation describes. Authentication is the operator's own (a long-lived token from `claude setup-token`).

## Node.js and npm

- **Project:** [nodejs/node](https://github.com/nodejs/node) and npm
- **License:** MIT (the Node.js project license; bundled components carry their own licenses); npm is Artistic-2.0
- **How it's used:** installed from Debian's `nodejs` and `npm` packages at image build time; the runtime the Claude Code CLI needs.

## Python dependencies

Installed from PyPI at image build time, unmodified; exact pins are in `requirements.txt`.

- **flask:** BSD-3-Clause
- **requests:** Apache-2.0

## Trademarks

"Immich" and "Claude" are trademarks of their respective owners. This is an unofficial project, not affiliated with or endorsed by them.
