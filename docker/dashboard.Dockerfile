# NeuroNews Streamlit Dashboard Service Docker Image
FROM python:3.11-slim as base

LABEL maintainer="NeuroNews Team"
LABEL description="NeuroNews Streamlit Dashboard"
LABEL version="1.0.0"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
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

# Install additional dashboard packages
RUN pip install --no-cache-dir \
    streamlit \
    plotly \
    networkx \
    altair \
    bokeh \
    seaborn \
    matplotlib \
    folium \
    wordcloud

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

# Create streamlit config directory
RUN mkdir -p /home/neuronews/.streamlit

# Create streamlit config
RUN echo '[server]\n\
headless = true\n\
port = 8501\n\
address = "0.0.0.0"\n\
\n\
[browser]\n\
gatherUsageStats = false\n\
\n\
[theme]\n\
primaryColor = "#FF4B4B"\n\
backgroundColor = "#FFFFFF"\n\
secondaryBackgroundColor = "#F0F2F6"\n\
textColor = "#262730"\n' > /home/neuronews/.streamlit/config.toml

# Expose port
EXPOSE 8501

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# Default command for development
CMD ["streamlit", "run", "src/dashboards/streamlit_dashboard.py", "--server.port=8501", "--server.address=0.0.0.0"]

# Production stage
FROM base as production

# Switch to non-root user
USER neuronews

# Copy only necessary files for production. The Streamlit app entrypoint is
# src/dashboards/streamlit_dashboard.py (copied via src/); launch_dashboard.py
# lives under scripts/utilities/ and is not used by the production CMD, so it is
# intentionally not copied here (the stale root-level COPY broke the build).
COPY --chown=neuronews:neuronews src/ ./src/
COPY --chown=neuronews:neuronews config/ ./config/
COPY --chown=neuronews:neuronews requirements.txt ./

# Create streamlit config directory
RUN mkdir -p /home/neuronews/.streamlit

# Create streamlit config
RUN echo '[server]\n\
headless = true\n\
port = 8501\n\
address = "0.0.0.0"\n\
enableCORS = false\n\
enableXsrfProtection = false\n\
\n\
[browser]\n\
gatherUsageStats = false\n\
\n\
[theme]\n\
primaryColor = "#FF4B4B"\n\
backgroundColor = "#FFFFFF"\n\
secondaryBackgroundColor = "#F0F2F6"\n\
textColor = "#262730"\n' > /home/neuronews/.streamlit/config.toml

# Expose port
EXPOSE 8501

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# Command for production
CMD ["streamlit", "run", "src/dashboards/streamlit_dashboard.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
