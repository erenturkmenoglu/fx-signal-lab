# syntax=docker/dockerfile:1
# FX Signal Lab - one image: Python backend (API + ingestion) serving the built dashboard.

# ---- stage 1: build the dashboard -------------------------------------------------------
FROM node:22-alpine AS dashboard
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- stage 2: runtime ---------------------------------------------------------------------
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/backend \
    INSTRUMENTS_FILE=/app/config/instruments.yaml \
    STATIC_DIR=/app/static

WORKDIR /app/backend
COPY backend/requirements.lock ./
RUN pip install -r requirements.lock

COPY backend/ ./
COPY config/ /app/config/
COPY --from=dashboard /build/dist /app/static

RUN useradd --system --uid 10001 --no-create-home fxlab
USER fxlab

EXPOSE 8000
# migrations first, then exactly one server process (the provider allows one connection)
CMD ["sh", "-c", "python -m fxlab.cli migrate && exec python -m fxlab.cli serve"]
