# syntax=docker/dockerfile:1.7
#
# Multi-stage frontend image with two end targets:
#   - `dev`     : Vite dev server with HMR (used by docker-compose.yml).
#   - `runtime` : Nginx serving the static `dist/` bundle (production).

ARG NODE_VERSION=20.18.0
ARG PNPM_VERSION=9.12.3

# ---------------------------------------------------------------------------
# Stage 1 — deps (cacheable pnpm fetch)
# ---------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS deps

# Enable pnpm via Corepack — pinned to the version declared in package.json.
ARG PNPM_VERSION
RUN corepack enable && corepack prepare pnpm@${PNPM_VERSION} --activate

WORKDIR /app
COPY package.json pnpm-lock.yaml* ./

RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store \
 && pnpm install --frozen-lockfile || pnpm install

# ---------------------------------------------------------------------------
# Stage 2 — dev (Vite HMR; targeted by docker-compose.yml)
# ---------------------------------------------------------------------------
FROM deps AS dev
WORKDIR /app
COPY . .
EXPOSE 5173
CMD ["pnpm", "dev", "--host", "0.0.0.0"]

# ---------------------------------------------------------------------------
# Stage 3 — build (produces /app/dist)
# ---------------------------------------------------------------------------
FROM deps AS build
WORKDIR /app
COPY . .
RUN pnpm build

# ---------------------------------------------------------------------------
# Stage 4 — runtime (nginx serving static assets, production)
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html

# SPA fallback — every unknown route returns index.html so the React router
# can take over.
RUN printf 'server {\n\
  listen 80;\n\
  root /usr/share/nginx/html;\n\
  location / { try_files $uri /index.html; }\n\
}\n' > /etc/nginx/conf.d/default.conf

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget --quiet --spider http://localhost/ || exit 1
