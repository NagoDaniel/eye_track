# Build context is the repository root: `docker build -f proxy/Dockerfile .`
# Stage 1 builds the frontend; MediaPipe WASM and the model are bundled into dist/ (no CDN).
FROM node:20 AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY shared /app/shared
COPY frontend ./
RUN npm run build

FROM caddy:2
COPY --from=frontend /app/frontend/dist /srv/www
COPY Caddyfile /etc/caddy/Caddyfile
COPY proxy/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
