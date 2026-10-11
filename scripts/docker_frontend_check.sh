#!/usr/bin/env bash
# group313: frontend type-check + build + unit tests on the Node version frontend/Dockerfile builds with, using the lockfile (npm ci).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
tag="$(grep -m1 -E '^FROM node:' "$root/frontend/Dockerfile" | sed -E 's/^FROM (node:[^ ]+).*/\1/')"
echo ">> frontend on $tag (same as frontend/Dockerfile)"
docker run --rm -v "$root/frontend":/app -w /app "$tag" sh -c \
  'npm ci && npx tsc --noEmit && npm run build && for t in riskCap treemapLayout journalStats; do node --experimental-strip-types src/components/board/$t.test.mjs; done'
