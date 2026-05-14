# syntax=docker/dockerfile:1.6

FROM python:3.11-slim AS base

# Avoid .pyc clutter and force unbuffered stdout (better logs in containers).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=5000

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Then copy the app.
COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 5000

# gthread is the right worker class here because /api/proxy streams responses
# (I/O bound). 2 workers x 8 threads is a sensible default for small VMs.
CMD ["sh", "-c", "gunicorn -w 2 -k gthread --threads 8 --timeout 120 -b 0.0.0.0:${PORT:-5000} app:app"]
