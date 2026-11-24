# syntax=docker/dockerfile:1
# LedgerFlow frontend image (Phase 6C): the production build served by NGINX as a non-root user.
# Same origin as the API (it proxies /api and /health to the gateway), so no CORS. It does
# NOT proxy /controller: the local experiment controller and its token never leave the
# developer's machine (use `npm run dev` for the failure lab and benchmarks).

FROM node:22.17-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# No secrets: every VITE_* value is public. Empty base URL = same origin.
ENV VITE_API_BASE_URL=""
RUN npm run build

# The official image (it receives NGINX security releases first; the -unprivileged variant
# lagged behind in a Trivy scan), made unprivileged here: PID and temp files in /tmp.
FROM nginx:1.30-alpine AS runtime
# OS security updates at build time (Trivy: fixable CVEs in base-image packages).
RUN apk upgrade --no-cache
COPY nginx.main.conf /etc/nginx/nginx.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
USER 101
COPY --from=build /app/dist /usr/share/nginx/html
# Runs as the image's non-root `nginx` user (uid 101) on 8080.
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
