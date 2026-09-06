# Stockky frontend — PRODUCTION build, served by nginx instead of `vite
# preview` (a Node.js process).
#
# WHY THIS CHANGED (speed, not correctness): `vite preview` is a full Node
# runtime just to hand out static files — on a 2 vCPU / 12 GB VM that's
# wasted RAM and a slower TTFB than a purpose-built static file server.
# nginx:alpine serves the exact same `dist/` bundle with sendfile(),
# gzip, and long-lived cache headers on the hashed asset filenames, using
# a fraction of the memory and no JS runtime startup at all. The build
# step (stage 1) is unchanged from before — only how the result is served
# changes. No application code or data logic is touched.
#
# Stage 1 — build the static bundle (same as before).
FROM node:20-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
ARG VITE_API_URL=/api
ENV VITE_API_URL=${VITE_API_URL}
RUN npx vite build

# Stage 2 — serve dist/ with nginx. Final image has no Node.js in it at all.
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
