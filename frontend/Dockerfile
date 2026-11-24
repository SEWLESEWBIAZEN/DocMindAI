# syntax=docker/dockerfile:1
#
# Targets:
#   dev    - Vite dev server with HMR; source bind-mounted by Compose.
#   build    - produces the static bundle in /app/dist.
#   runtime  - production web server: Nginx serving the bundle and proxying /api
#              (deploy/production/nginx), TLS via Let's Encrypt.

ARG NODE_VERSION=22

FROM node:${NODE_VERSION}-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM deps AS dev
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

FROM deps AS build
COPY . .
RUN npm run build

FROM nginx:1.27-alpine AS runtime
RUN apk add --no-cache openssl
COPY --from=build /app/dist /usr/share/nginx/html
COPY --from=deploy-nginx documind.conf.template /etc/nginx/templates/documind.conf.template
COPY --from=deploy-nginx entrypoint.sh /usr/local/bin/documind-nginx
RUN rm -f /etc/nginx/conf.d/default.conf && mkdir -p /var/www/acme
EXPOSE 80 443
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget -q --spider --no-check-certificate https://127.0.0.1/ || exit 1
ENTRYPOINT ["/usr/local/bin/documind-nginx"]
