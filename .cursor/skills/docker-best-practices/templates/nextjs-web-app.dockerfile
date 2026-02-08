# syntax=docker/dockerfile:1.7
#
# Next.js 16 web application
# Optimized for: Fast builds, minimal production image, Vercel deployment compatibility
#
# Build: docker build -f templates/nextjs-web-app.dockerfile -t nextjs-app:latest .
# Dev:   docker build --target development -t nextjs-app:dev .
# Prod:  docker build --target production -t nextjs-app:prod .

# ─── Base stage — shared Node.js runtime ────────────────────────────────────
FROM node:20.18.2-alpine AS base

WORKDIR /app

# Install system dependencies for native modules
RUN apk add --no-cache libc6-compat

# ─── Dependencies stage — installs node_modules ─────────────────────────────
FROM base AS deps

# Copy package files
COPY package.json package-lock.json* pnpm-lock.yaml* yarn.lock* ./

# Install dependencies based on lockfile (pnpm preferred)
RUN --mount=type=cache,target=/root/.pnpm-store \
    if [ -f pnpm-lock.yaml ]; then \
        npm install -g pnpm && pnpm install --frozen-lockfile --prefer-offline; \
    elif [ -f yarn.lock ]; then \
        yarn install --frozen-lockfile; \
    elif [ -f package-lock.json ]; then \
        npm ci --prefer-offline; \
    else \
        npm install; \
    fi

# ─── Builder stage — builds Next.js app ─────────────────────────────────────
FROM base AS builder

# Copy dependencies from deps stage
COPY --from=deps /app/node_modules ./node_modules

# Copy application source
COPY . .

# Set build-time environment variables
ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production

# Build Next.js application
RUN --mount=type=cache,target=/app/.next/cache \
    if [ -f pnpm-lock.yaml ]; then \
        npm install -g pnpm && pnpm build; \
    elif [ -f yarn.lock ]; then \
        yarn build; \
    else \
        npm run build; \
    fi

# ─── Development stage — includes dev tools and hot reload ──────────────────
FROM base AS development

COPY --from=deps /app/node_modules ./node_modules
COPY . .

ENV NODE_ENV=development

EXPOSE 3000

CMD ["npm", "run", "dev"]

# ─── Production stage — minimal runtime image ───────────────────────────────
FROM base AS production

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# Create non-root user
RUN addgroup -g 1001 nodejs && \
    adduser -S -u 1001 -G nodejs nextjs

# Copy built assets from builder
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs

EXPOSE 3000

ENV PORT=3000 \
    HOSTNAME="0.0.0.0"

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD node -e "require('http').get('http://localhost:3000/api/health', (r) => process.exit(r.statusCode === 200 ? 0 : 1))" || exit 1

# Next.js standalone server (built-in when `output: 'standalone'` in next.config.ts)
CMD ["node", "server.js"]
