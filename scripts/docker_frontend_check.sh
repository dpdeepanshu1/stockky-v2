#!/usr/bin/env bash
# group313: frontend type-check + build + unit tests, using the lockfile (npm ci).
# group435: the type-check and build run on the Node version frontend/Dockerfile builds with (node:20), but the unit tests are .mjs files that
# need `node --experimental-strip-types` (Node >= 22.6), so they run on FRONTEND_TEST_NODE_IMAGE (default node:22-slim). On node:20 every test
# died with "node: bad option: --experimental-strip-types" and the script looked like it had failed after a good build.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
tag="$(grep -m1 -E '^FROM node:' "$root/frontend/Dockerfile" | sed -E 's/^FROM (node:[^ ]+).*/\1/')"
test_tag="${FRONTEND_TEST_NODE_IMAGE:-node:22-slim}"
echo ">> frontend type-check + build on $tag (same as frontend/Dockerfile)"
docker run --rm -v "$root/frontend":/app -w /app "$tag" sh -c 'npm ci && npx tsc --noEmit && npm run build'
echo ">> frontend unit tests on $test_tag (needs --experimental-strip-types)"
# E285: several unit tests read files OUTSIDE frontend/ through new URL("../../", import.meta.url): services/*/main.py, swing/*.py, jobs.py, hourly_reconcile.py
# and frontend/src/*.ts. With only frontend/ mounted at /app that root is "/" and the reads died with ENOENT (first /services/..., then /frontend/...). The WHOLE repo is
# now mounted read-only at /repo and the tests run from /repo/frontend, so "../../" is the repo root exactly as on a checkout (node_modules from the build step above
# sits in frontend/ on the host, so it is there too).
docker run --rm -v "$root":/repo:ro -w /repo/frontend "$test_tag" sh -c \
  'for t in riskCap treemapLayout journalStats flowGeometry; do echo "-- $t"; node --experimental-strip-types src/components/board/$t.test.mjs; done; echo "-- npm test (all src tests)"; npm test'
