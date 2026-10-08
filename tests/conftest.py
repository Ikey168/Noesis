# Simplified pytest configuration for containerized testing
import asyncio
import os

import pytest

# Try to import database dependencies, but make them optional for backward
# compatibility
try:
    pass

    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

# Set testing environment
os.environ["TESTING"] = "true"
# Never count test traffic against the real free-tier quota ledger (src/ingestion/quota.py).
os.environ.setdefault("NOESIS_QUOTA_DISABLED", "1")

# Only import database setup if dependencies are available
if PSYCOPG2_AVAILABLE:
    try:
        from src.database.setup import (
            cleanup_test_database,
            create_test_articles,
            get_sync_connection,
            setup_test_database,
        )

        DATABASE_SETUP_AVAILABLE = True
    except ImportError:
        DATABASE_SETUP_AVAILABLE = False
else:
    DATABASE_SETUP_AVAILABLE = False


# Removed custom event_loop fixture to avoid conflicts with pytest-asyncio
# Use @pytest.mark.asyncio(loop_scope="session") if session-scoped loop is needed


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    """Set up test environment before running tests."""
    if not DATABASE_SETUP_AVAILABLE:
        # Skip database setup in non-containerized environment
        yield
        return

    # Set up test database (the setup helpers are async coroutines).
    # A database is optional - tests fall back to mocks when unavailable.
    try:
        asyncio.run(setup_test_database())
    except Exception as e:
        print(f"Test database unavailable, continuing with mocks: {e}")
    yield
    # Clean up after all tests
    try:
        asyncio.run(cleanup_test_database())
    except Exception:
        pass


@pytest.fixture(scope="function")
def db_connection():
    """Provide a database connection for testing."""
    if not DATABASE_SETUP_AVAILABLE:
        # Return a mock connection for non-containerized testing
        from unittest.mock import MagicMock

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda x: mock_conn
        mock_conn.__exit__ = lambda *args: None
        yield mock_conn
        return

    conn = get_sync_connection(testing=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="function")
def clean_db(db_connection):
    """Ensure clean database state for each test."""
    # Clear test data before test
    with db_connection.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE neuronews.article_clusters,
                          neuronews.article_embeddings,
                          neuronews.article_keywords,
                          neuronews.article_topics,
                          neuronews.article_translations,
                          neuronews.event_clusters,
                          neuronews.articles
            RESTART IDENTITY CASCADE
        """
        )
        db_connection.commit()

    yield db_connection

    # Clean up after test (optional, but good practice)
    with db_connection.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE neuronews.article_clusters,
                          neuronews.article_embeddings,
                          neuronews.article_keywords,
                          neuronews.article_topics,
                          neuronews.article_translations,
                          neuronews.event_clusters,
                          neuronews.articles
            RESTART IDENTITY CASCADE
        """
        )
        db_connection.commit()


@pytest.fixture
def sample_articles(clean_db):
    """Create sample articles for testing."""
    return create_test_articles(count=5)


@pytest.fixture
def mock_sentence_transformer():
    """Provide a simple mock for SentenceTransformer in isolated tests."""

    class MockTransformer:

        def __init__(self, *args, **kwargs):
            self.embedding_dimension = 384

        def encode(self, texts, **kwargs):
            import numpy as np

            if isinstance(texts, str):
                texts = [texts]
            return np.random.rand(len(texts), 384)

        def get_sentence_embedding_dimension(self):
            return 384

    return MockTransformer


# Environment-specific configurations
@pytest.fixture
def app_config():
    """Provide application configuration for tests."""
    return {
        "database": {
            "host": os.getenv("DB_HOST", "test-postgres"),
            "port": int(os.getenv("DB_PORT", 5432)),
            "database": os.getenv("DB_NAME", "neuronews_test"),
            "user": os.getenv("DB_USER", "test_user"),
            "password": os.getenv("DB_PASSWORD", "test_password"),
        },
        "redis": {
            "host": os.getenv("REDIS_HOST", "test-redis"),
            "port": int(os.getenv("REDIS_PORT", 6379)),
        },
        "s3": {
            "endpoint": os.getenv("S3_ENDPOINT", "http://test-minio:9000"),
            "access_key": os.getenv("S3_ACCESS_KEY", "testuser"),
            "secret_key": os.getenv("S3_SECRET_KEY", "testpassword"),
            "bucket": os.getenv("S3_BUCKET", "test-bucket"),
        },
    }
