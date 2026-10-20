# syntax=docker/dockerfile:1
# Build from monorepo root (needs packages/catalogs for @aop-catalogs):
#   docker build -f apps/web/Dockerfile .
FROM node:20-alpine AS deps
WORKDIR /workspace/apps/web
ARG NPM_REGISTRY=https://registry.npmmirror.com
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm config set registry "${NPM_REGISTRY}" \
  && npm ci

FROM node:20-alpine AS build
WORKDIR /workspace/apps/web
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG NEXT_PUBLIC_API_BASE=http://127.0.0.1:8080
ARG NEXT_PUBLIC_API_KEY=
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
ENV NEXT_PUBLIC_API_KEY=${NEXT_PUBLIC_API_KEY}
COPY --from=deps /workspace/apps/web/node_modules ./node_modules
# Catalogs must sit at ../../packages/catalogs relative to apps/web (next.config.js alias)
COPY packages/catalogs /workspace/packages/catalogs
COPY apps/web /workspace/apps/web
RUN npm config set registry "${NPM_REGISTRY}" \
  && npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
COPY --from=build /workspace/apps/web/package.json ./
COPY --from=build /workspace/apps/web/.next ./.next
COPY --from=build /workspace/apps/web/node_modules ./node_modules
COPY --from=build /workspace/apps/web/next.config.js ./
RUN mkdir -p ./public
COPY --from=build /workspace/apps/web/public ./public
EXPOSE 3000
CMD ["npm", "run", "start"]
