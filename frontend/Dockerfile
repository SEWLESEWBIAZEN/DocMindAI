# syntax=docker/dockerfile:1
#
# Targets:
#   dev    - Vite dev server with HMR; source bind-mounted by Compose.
#   build  - produces the static bundle in /app/dist (served by Nginx in Phase 10).

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
