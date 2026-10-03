# Attribution

This repository's own code is licensed under the MIT License (see `LICENSE`). It uses or builds on the third-party projects below, each under its own license and copyright; nothing here relicenses them.

## Immich

- **Project:** [immich-app/immich](https://github.com/immich-app/immich)
- **License:** AGPL-3.0
- **How it's used:** the pipeline calls Immich's HTTP API. No Immich code is included or redistributed here.

## Claude Code CLI (Anthropic)

- **Project:** `@anthropic-ai/claude-code`, from Anthropic PBC
- **License:** proprietary; use is subject to Anthropic's terms, not to this repository's MIT license
- **How it's used:** installed with npm at image build time (see the `Dockerfile`) and run headless as a subprocess by the pipeline's recipe runner. It is not covered by this repo's license.

## Node.js

- **Project:** [nodejs/node](https://github.com/nodejs/node)
- **License:** MIT (the Node.js project license; bundled components carry their own licenses)
- **How it's used:** the runtime that Claude Code needs, downloaded from nodejs.org at image build time.

## Python dependencies

Installed from PyPI at image build time, unmodified; exact pins are in `requirements.txt`.

- **flask:** BSD-3-Clause
- **requests:** Apache-2.0

## Trademarks

"Immich" and "Claude" are trademarks of their respective owners. This is an unofficial project, not affiliated with or endorsed by them.
