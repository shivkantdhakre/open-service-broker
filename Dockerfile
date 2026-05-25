# =============================================================================
# Stage 1: Builder — install dependencies
# =============================================================================
FROM python:3.12-slim AS builder

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

# Add required packages
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --prefix=/install .

# =============================================================================
# Stage 2: Runtime — slim production image
# =============================================================================
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY src/ ./src/

# Non-root user for security
RUN useradd --create-home --shell /bin/bash broker
USER broker

# Configurable entrypoint: set ENTRYPOINT_MODE=api or ENTRYPOINT_MODE=worker
ENV ENTRYPOINT_MODE=api
ENV APP_HOST=0.0.0.0
ENV APP_PORT=8000
ENV PYTHONPATH=/app/src

EXPOSE 8000

CMD ["sh", "-c", "\
    if [ \"$ENTRYPOINT_MODE\" = 'worker' ]; then \
        python -m broker.worker; \
    else \
        uvicorn broker.main:app --host $APP_HOST --port $APP_PORT --loop uvloop; \
    fi \
"]
