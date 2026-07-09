# NeuroNews NLP Pipeline Service Docker Image
FROM python:3.11-slim as base

LABEL maintainer="NeuroNews Team"
LABEL description="NeuroNews NLP Pipeline Service"
LABEL version="1.0.0"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# Install system dependencies for NLP
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    gfortran \
    libpq-dev \
    libopenblas-dev \
    liblapack-dev \
    pkg-config \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN groupadd --gid 1001 neuronews && \
    useradd --uid 1001 --gid neuronews --shell /bin/bash --create-home neuronews

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies as root
RUN pip install --no-cache-dir -r requirements.txt

# Install additional NLP packages
RUN pip install --no-cache-dir \
    torch==2.0.1 --index-url https://download.pytorch.org/whl/cpu \
    transformers \
    spacy \
    nltk \
    scikit-learn \
    numpy \
    pandas

# Download spaCy models
RUN python -m spacy download en_core_web_sm

# Download NLTK data
RUN python -c "import nltk; nltk.download('punkt'); nltk.download('stopwords'); nltk.download('averaged_perceptron_tagger'); nltk.download('wordnet')"

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

# Create data and logs directories
USER root
RUN mkdir -p /app/data /app/logs /app/models \
    && chown -R neuronews:neuronews /app/data /app/logs /app/models
USER neuronews

# Health check
HEALTHCHECK --interval=60s --timeout=30s --start-period=120s --retries=3 \
    CMD python -c "import spacy; import transformers; import sys; sys.exit(0)" || exit 1

# Default command for development
CMD ["python", "-c", "from src.nlp.pipeline import Pipeline; pipeline = Pipeline(); print('NLP Pipeline ready')"]

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
RUN mkdir -p /app/data /app/logs /app/models \
    && chown -R neuronews:neuronews /app/data /app/logs /app/models
USER neuronews

# Health check
HEALTHCHECK --interval=60s --timeout=30s --start-period=120s --retries=3 \
    CMD python -c "import spacy; import transformers; import sys; sys.exit(0)" || exit 1

# Command for production (NLP processing service)
CMD ["python", "-m", "src.nlp.pipeline", "--process-all"]

# Worker stage for processing queue
FROM production as worker

# Install Redis for queue management
RUN pip install --no-cache-dir redis celery

# Create celery user
USER root
RUN usermod -a -G neuronews neuronews

USER neuronews

# Command for worker
CMD ["celery", "-A", "src.nlp.tasks", "worker", "--loglevel=info"]
