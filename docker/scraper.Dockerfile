# NeuroNews News Scraper Service Docker Image
FROM python:3.11-slim as base

LABEL maintainer="NeuroNews Team"
LABEL description="NeuroNews News Scraper Service"
LABEL version="1.0.0"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# Install system dependencies for web scraping
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    wget \
    chromium \
    chromium-driver \
    xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install Playwright dependencies
RUN apt-get update && apt-get install -y \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgtk-3-0 \
    libxss1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1001 neuronews && \
    useradd --uid 1001 --gid neuronews --shell /bin/bash --create-home neuronews

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies as root
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir playwright selenium

# Install Playwright browsers
RUN playwright install chromium

# Development stage
FROM base as development

# Install additional development tools
RUN apt-get update && apt-get install -y \
    git \
    vim \
    htop \
    && rm -rf /var/lib/apt/lists/*

# Switch to non-root user
USER neuronews

# Copy application code
COPY --chown=neuronews:neuronews . .

# Create data directory
USER root
RUN mkdir -p /app/data /app/logs \
    && chown -R neuronews:neuronews /app/data /app/logs
USER neuronews

# Health check
HEALTHCHECK --interval=60s --timeout=30s --start-period=60s --retries=3 \
    CMD python -c "import requests; import sys; sys.exit(0)" || exit 1

# Default command for development
CMD ["python", "src/main.py", "--scrape"]

# Production stage
FROM base as production

# Switch to non-root user
USER neuronews

# Copy only necessary files for production
COPY --chown=neuronews:neuronews src/ ./src/
COPY --chown=neuronews:neuronews config/ ./config/
COPY --chown=neuronews:neuronews requirements.txt ./

# Create directories
USER root
RUN mkdir -p /app/data /app/logs \
    && chown -R neuronews:neuronews /app/data /app/logs
USER neuronews

# Health check
HEALTHCHECK --interval=60s --timeout=30s --start-period=60s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# Command for production (scraper service)
CMD ["python", "src/main.py", "--scrape", "--output", "/app/data/news_articles.json"]

# Scheduler stage for periodic scraping
FROM production as scheduler

# Install cron for scheduling
USER root
RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*

# Add cron job for periodic scraping (every hour)
RUN echo "0 * * * * neuronews cd /app && python src/main.py --scrape --output /app/data/news_articles.json >> /app/logs/scraper.log 2>&1" > /etc/cron.d/neuronews-scraper
RUN chmod 0644 /etc/cron.d/neuronews-scraper
RUN crontab /etc/cron.d/neuronews-scraper

# Switch back to non-root user
USER neuronews

# Command for scheduler
CMD ["cron", "-f"]
