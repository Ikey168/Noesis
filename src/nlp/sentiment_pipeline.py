"""
Enhanced sentiment analysis pipeline for news articles.
Supports both AWS Comprehend and Hugging Face transformers.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import Json, execute_batch

from .sentiment_analysis import create_analyzer

logger = logging.getLogger(__name__)


class SentimentPipeline:
    """Enhanced sentiment analysis pipeline with multiple provider support."""

    def __init__(
        self,
        snowflake_account: str,
        snowflake_user: str,
        snowflake_password: str,
        snowflake_warehouse: str = "ANALYTICS_WH",
        snowflake_database: str = "NEURONEWS",
        snowflake_schema: str = "PUBLIC",
        sentiment_provider: str = "huggingface",
        batch_size: int = 25,
        **sentiment_config,
    ):
        """
        Initialize the sentiment analysis pipeline.

        Args:
            snowflake_account: Snowflake account identifier
            snowflake_user: Database user
            snowflake_password: Database password
            snowflake_warehouse: Snowflake warehouse name
            snowflake_database: Database name
            snowflake_schema: Schema name
            sentiment_provider: Provider ("huggingface", "aws_comprehend")
            batch_size: Batch size for processing
            **sentiment_config: Additional config for sentiment analyzer
        """
        self.batch_size = batch_size
        self.sentiment_provider = sentiment_provider

        self.conn_params = {
            "account": snowflake_account,
            "user": snowflake_user,
            "password": snowflake_password,
            "warehouse": snowflake_warehouse,
            "database": snowflake_database,
            "schema": snowflake_schema,
        }

        # Initialize sentiment analyzer
        try:
            self.analyzer = create_analyzer(
                provider=sentiment_provider, **sentiment_config
            )
            logger.info(
                "Initialized sentiment analyzer with provider: {0}".format(
                    sentiment_provider
                )
            )
        except Exception as e:
            logger.error("Failed to initialize sentiment analyzer: {0}".format(str(e)))
            raise

        # Initialize database schema
        self._initialize_database()

    def _initialize_database(self):
        """Create or update database tables for sentiment analysis."""

        # Enhanced schema to support the Issue #28 requirements
        create_tables_sql = """
        -- Main articles table with sentiment fields
        CREATE TABLE IF NOT EXISTS news_articles (
            id VARCHAR(255) PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT,
            content TEXT,
            source VARCHAR(255),
            category VARCHAR(100),
            publish_date DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            -- Sentiment analysis fields (Issue #28 requirement)
            sentiment_label VARCHAR(50),
            sentiment_score FLOAT,
            sentiment_confidence FLOAT,
            sentiment_provider VARCHAR(50),
            sentiment_processed_at TIMESTAMP,
            sentiment_metadata JSONB
        );

        -- Detailed sentiment scores table for advanced analytics
        CREATE TABLE IF NOT EXISTS article_sentiment_details (
            id SERIAL PRIMARY KEY,
            article_id VARCHAR(255) REFERENCES news_articles(id),
            provider VARCHAR(50) NOT NULL,
            positive_score FLOAT,
            negative_score FLOAT,
            neutral_score FLOAT,
            mixed_score FLOAT,
            language_code VARCHAR(10),
            text_snippet TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Index for efficient sentiment trend queries (Issue #28 requirement)
        CREATE INDEX IF NOT EXISTS idx_articles_sentiment_date
        ON news_articles(sentiment_label, publish_date);

        CREATE INDEX IF NOT EXISTS idx_articles_topic_sentiment
        ON news_articles(sentiment_label) WHERE title IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_articles_source_sentiment
        ON news_articles(source, sentiment_label, publish_date);
        """

        try:
            with psycopg2.connect(**self.conn_params) as conn:
                with conn.cursor() as cur:
                    cur.execute(create_tables_sql)
                    conn.commit()
                    logger.info(
                        "Successfully initialized sentiment analysis database schema"
                    )
        except Exception as e:
            logger.error("Failed to initialize database: {0}".format(str(e)))
            raise

    def _store_sentiment_results(self, results: List[Dict[str, Any]]):
        """Store sentiment analysis results in Redshift."""

        # Prepare data for news_articles table
        article_updates = []
        sentiment_details = []

        for result in results:
            # Main article sentiment update
            article_update = {
                "article_id": result["article_id"],
                "sentiment_label": result.get("sentiment_label"),
                "sentiment_score": result.get("sentiment_score"),
                "sentiment_confidence": result.get("sentiment_confidence"),
                "sentiment_provider": result.get("sentiment_provider"),
                "sentiment_processed_at": datetime.now(timezone.utc),
                "sentiment_metadata": Json(result.get("sentiment_metadata", {})),
            }
            article_updates.append(article_update)

            # Detailed sentiment scores
            if "all_scores" in result:
                scores = result["all_scores"]
                detail_record = {
                    "article_id": result["article_id"],
                    "provider": result.get("sentiment_provider"),
                    "positive_score": scores.get("positive", 0.0),
                    "negative_score": scores.get("negative", 0.0),
                    "neutral_score": scores.get("neutral", 0.0),
                    "mixed_score": scores.get("mixed", 0.0),
                    "language_code": result.get("language_code", "en"),
                    "text_snippet": result.get("text", "")[
                        :500
                    ],  # Store first 500 chars
                }
                sentiment_details.append(detail_record)

        # Update main articles table
        update_articles_sql = """
        UPDATE news_articles SET
            sentiment_label = %(sentiment_label)s,
            sentiment_score = %(sentiment_score)s,
            sentiment_confidence = %(sentiment_confidence)s,
            sentiment_provider = %(sentiment_provider)s,
            sentiment_processed_at = %(sentiment_processed_at)s,
            sentiment_metadata = %(sentiment_metadata)s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(article_id)s
        """

        # Insert detailed sentiment data
        insert_details_sql = """
        INSERT INTO article_sentiment_details (
            article_id, provider, positive_score, negative_score,
            neutral_score, mixed_score, language_code, text_snippet
        ) VALUES (
            %(article_id)s, %(provider)s, %(positive_score)s, %(negative_score)s,
            %(neutral_score)s, %(mixed_score)s, %(language_code)s, %(text_snippet)s
        )
        """

        try:
            with psycopg2.connect(**self.conn_params) as conn:
                with conn.cursor() as cur:
                    # Update articles
                    execute_batch(
                        cur,
                        update_articles_sql,
                        article_updates,
                        page_size=self.batch_size,
                    )

                    # Insert detailed scores
                    if sentiment_details:
                        execute_batch(
                            cur,
                            insert_details_sql,
                            sentiment_details,
                            page_size=self.batch_size,
                        )

                    conn.commit()
                    logger.info(
                        "Successfully stored sentiment results for {0} articles".format(
                            len(results)
                        )
                    )

        except Exception as e:
            logger.error("Failed to store sentiment results: {0}".format(str(e)))
            raise

    def process_articles(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Process articles through sentiment analysis pipeline.

        Args:
            articles: List of article dicts with keys:
                - article_id: Unique identifier
                - title: Article title
                - content: Article content
                - url: Article URL (optional)
                - source: News source (optional)
                - publish_date: Publication date (optional)

        Returns:
            List of processed results with sentiment analysis
        """
        if not articles:
            return []

        try:
            # Prepare texts for analysis (combine title and content)
            texts = []
            for article in articles:
                title = article.get("title", "")
                content = article.get("content", "")
                combined_text = "{0}. {1}".format(title, content).strip()
                texts.append(combined_text)

            logger.info(
                "Analyzing sentiment for {0} articles using {1}".format(
                    len(articles), self.sentiment_provider
                )
            )

            # Perform batch sentiment analysis
            if hasattr(self.analyzer, "analyze_batch"):
                sentiment_results = self.analyzer.analyze_batch(texts)
            else:
                # Fallback to individual analysis
                sentiment_results = [self.analyzer.analyze(text) for text in texts]

            # Combine article data with sentiment results
            processed_results = []
            for article, sentiment in zip(articles, sentiment_results):

                # Handle different response formats
                if sentiment.get("label") == "ERROR":
                    logger.warning(
                        f"Sentiment analysis failed for article {article.get('article_id')}: {sentiment.get('message')}"
                    )
                    sentiment_label = None
                    sentiment_score = None
                    sentiment_confidence = None
                else:
                    sentiment_label = sentiment.get("label", "").upper()
                    sentiment_score = sentiment.get("score", 0.0)

                    # Some providers use 'score' as confidence, others have
                    # separate fields
                    sentiment_confidence = sentiment.get(
                        "confidence", sentiment.get("score", 0.0)
                    )

                result = {
                    "article_id": article["article_id"],
                    "url": article.get("url"),
                    "title": article.get("title"),
                    "content": article.get("content"),
                    "source": article.get("source"),
                    "publish_date": article.get("publish_date"),
                    "sentiment_label": sentiment_label,
                    "sentiment_score": sentiment_score,
                    "sentiment_confidence": sentiment_confidence,
                    "sentiment_provider": self.sentiment_provider,
                    "sentiment_metadata": {
                        "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
                        "text_length": len(sentiment.get("text", "")),
                        "provider_specific": sentiment,
                    },
                    "all_scores": sentiment.get("all_scores", {}),
                    "language_code": sentiment.get("language_code", "en"),
                    "text": sentiment.get("text", ""),
                }
                processed_results.append(result)

            # Store results in database
            self._store_sentiment_results(processed_results)

            logger.info(
                "Successfully processed sentiment analysis for {0} articles".format(
                    len(processed_results)
                )
            )
            return processed_results

        except Exception as e:
            logger.error("Error in sentiment processing pipeline: {0}".format(str(e)))
            raise

    def reprocess_articles(
        self, article_ids: Optional[List[str]] = None, days_back: int = 7
    ) -> Dict[str, Any]:
        """
        Reprocess existing articles for sentiment analysis.

        Args:
            article_ids: Specific article IDs to reprocess (optional)
            days_back: How many days back to reprocess if no IDs specified

        Returns:
            Dict with processing statistics
        """
        try:
            # Build query to get articles to reprocess
            if article_ids:
                query = """
                SELECT id, title, content, url, source, publish_date
                FROM news_articles
                WHERE id IN ({placeholders})
                """
                params = article_ids
            else:
                query = """
                SELECT id, title, content, url, source, publish_date
                FROM news_articles
                WHERE publish_date >= CURRENT_DATE - INTERVAL '%s days'
                """
                params = [days_back]

            # Fetch articles from database
            with psycopg2.connect(**self.conn_params) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()

            if not rows:
                return {"processed": 0, "message": "No articles found to reprocess"}

            # Convert to article format
            articles = []
            for row in rows:
                articles.append(
                    {
                        "article_id": row[0],
                        "title": row[1],
                        "content": row[2],
                        "url": row[3],
                        "source": row[4],
                        "publish_date": row[5],
                    }
                )

            # Process articles
            results = self.process_articles(articles)

            # Calculate statistics
            sentiment_counts = {}
            for result in results:
                label = result.get("sentiment_label", "UNKNOWN")
                sentiment_counts[label] = sentiment_counts.get(label, 0) + 1

            return {
                "processed": len(results),
                "sentiment_distribution": sentiment_counts,
                "provider": self.sentiment_provider,
                "reprocessed_articles": len(articles),
            }

        except Exception as e:
            logger.error("Error reprocessing articles: {0}".format(str(e)))
            raise

    def get_sentiment_stats(self, days: int = 30) -> Dict[str, Any]:
        """Get sentiment analysis statistics for the last N days."""
        try:
            query = """
            SELECT
                sentiment_label,
                COUNT(*) as count,
                AVG(sentiment_score) as avg_score,
                MIN(sentiment_score) as min_score,
                MAX(sentiment_score) as max_score,
                sentiment_provider
            FROM news_articles
            WHERE sentiment_processed_at >= CURRENT_DATE - INTERVAL '%s days'
                AND sentiment_label IS NOT NULL
            GROUP BY sentiment_label, sentiment_provider
            ORDER BY count DESC
            """

            with psycopg2.connect(**self.conn_params) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, [days])
                    results = cur.fetchall()

            stats = {
                "period_days": days,
                "sentiment_breakdown": [],
                "total_processed": 0,
            }

            for row in results:
                sentiment_data = {
                    "sentiment": row[0],
                    "count": int(row[1]),
                    "avg_score": float(row[2]) if row[2] else 0.0,
                    "min_score": float(row[3]) if row[3] else 0.0,
                    "max_score": float(row[4]) if row[4] else 0.0,
                    "provider": row[5],
                }
                stats["sentiment_breakdown"].append(sentiment_data)
                stats["total_processed"] += sentiment_data["count"]

            return stats

        except Exception as e:
            logger.error("Error getting sentiment stats: {0}".format(str(e)))
            raise


def suggest_sentiment_with_jev(*args, **kwargs) -> dict:
    """Explicit source-bound suggestion, separate from pipeline writes/trends."""
    from src.argument_mining.jev_nlp_adapters import suggest_sentiment

    return suggest_sentiment(*args, **kwargs)
