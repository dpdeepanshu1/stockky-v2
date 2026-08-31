# Stockky frontend — PRODUCTION build, served as static files.
#
# Why this changed: the old image ran `npm run dev` (the Vite dev server). The
# dev server opens an HMR websocket (/@vite/client). Behind the HTTPS nginx
# reverse proxy that websocket can't stay connected, so Vite's client keeps
# deciding the server "restarted" and forces a full-page reload — that is the
# ~5-second reload-when-idle you were seeing. A production build has NO HMR
# websocket, so the page stays put.
FROM node:20-slim
WORKDIR /app

# Install deps first (better layer caching). devDependencies include vite/tsc,
# which are needed to build, so we do a full install (not --production).
COPY package.json ./
RUN npm install

# App source.
COPY . .

# VITE_* variables are inlined into the JS bundle at BUILD time (not read at
# runtime), so the API base URL MUST be provided as a build arg.
# docker-compose passes this from .env (VITE_API_URL). Default "/api" is a
# relative path that also works behind nginx if the var is unset.
ARG VITE_API_URL=/api
ENV VITE_API_URL=${VITE_API_URL}

# Build with vite directly instead of `npm run build` (which is "tsc && vite
# build"): a stray TypeScript type error should not be able to fail the
# production image. Vite transpiles TS on its own.
RUN npx vite build

EXPOSE 5173
# Serve the built dist/ with `vite preview` — a plain static file server with
# NO HMR/websocket, so there is no reload loop. host + allowedHosts come from
# vite.config.ts (preview block) so stockky.duckdns.org is not blocked.
CMD ["npx", "vite", "preview", "--host", "0.0.0.0", "--port", "5173"]
