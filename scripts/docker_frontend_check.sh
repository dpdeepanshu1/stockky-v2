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
# E127: calibration.test.mjs checks the plan against ../../services/calibration-runner-service/jobs.py; only frontend/ was mounted, so it died with ENOENT
# (/services/calibration-runner-service/jobs.py). The one file it reads is now mounted read-only at the path the test resolves.
docker run --rm -v "$root/frontend":/app -v "$root/services/calibration-runner-service/jobs.py":/services/calibration-runner-service/jobs.py:ro -w /app "$test_tag" sh -c \
  'for t in riskCap treemapLayout journalStats flowGeometry; do echo "-- $t"; node --experimental-strip-types src/components/board/$t.test.mjs; done; echo "-- npm test (all src tests)"; npm test'
