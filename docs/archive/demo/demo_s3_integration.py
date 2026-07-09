#!/usr/bin/env python3
"""
Integration example: NeuroNews Scraper with S3 Article Storage

This example demonstrates how to integrate the S3 article storage
with the existing NeuroNews scraper for automated article ingestion.
"""

from src.scraper.async_scraper_engine import AsyncNewsScraperEngine
from src.database.s3_storage import (ArticleType, S3ArticleStorage,
                                     S3StorageConfig,
                                     ingest_scraped_articles_to_s3)
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

# Add project path
sys.path.append("/workspaces/NeuroNews")

# Import S3 storage components
# Import scraper components


class NeuroNewsS3Integration:
    """Integration class for NeuroNews scraper with S3 storage."""

    def __init__(self, s3_config: S3StorageConfig):
        """Initialize the integration."""
        self.s3_config = s3_config
        self.s3_storage = None
        self.scraper = None

    async def initialize(self):
        """Initialize scraper and S3 storage."""
        print("🔧 Initializing NeuroNews S3 Integration...")

        # Initialize S3 storage
        try:
            self.s3_storage = S3ArticleStorage(self.s3_config)
            if self.s3_storage.s3_client:
                print(" S3 storage initialized")
            else:
                print("⚠️  S3 storage initialized without credentials")
        except Exception as e:
            print("❌ Failed to initialize S3 storage: {0}".format(e))

        # Initialize scraper with monitoring (if available)
        try:
            self.scraper = AsyncNewsScraperEngine(
                max_concurrent=5, enable_monitoring=True  # Use monitoring if available
            )
            await self.scraper.start()
            print(" Scraper initialized")
        except Exception as e:
            print("❌ Failed to initialize scraper: {0}".format(e))
            # Fallback to basic scraper
            self.scraper = AsyncNewsScraperEngine(max_concurrent=5)
            await self.scraper.start()
            print(" Basic scraper initialized")

    async def scrape_and_store_articles(self, urls: List[str]) -> Dict[str, Any]:
        """
        Scrape articles from URLs and store them in S3.

        Args:
            urls: List of URLs to scrape

        Returns:
            Results dictionary with scraping and storage statistics
        """
        print("
 Scraping {0} URLs...".format(len(urls)))"

        # Scrape articles
        scraped_articles = []
        scraping_errors = []

        for i, url in enumerate(urls):
            try:
                print("   Scraping {0}/{1}: {2}".format(i+1, len(urls), url))

                # Use the scraper to get article content
                # Note: This is a simplified example - in real usage you'd handle'
                # the full scraping pipeline with proper error handling
                result = await self._scrape_single_url(url)

                if result:
                    scraped_articles.append(result)
                    print(
                        f"    Successfully scraped: {result.get('title', 'Unknown')[:50]}..."
                    )
                else:
                    scraping_errors.append("Failed to scrape {0}".format(url))
                    print("   ❌ Failed to scrape: {0}".format(url))

            except Exception as e:
                scraping_errors.append("Error scraping {0}: {1}".format(url, str(e)))
                print("   ❌ Error scraping {0}: {1}".format(url, e))

        print(
            ""
 Scraping completed: {0} successful, {1} failed".format(len(scraped_articles), len(scraping_errors))"
        )

        # Store articles in S3
        if scraped_articles and self.s3_storage:
            print(""
💾 Storing {0} articles in S3...".format(len(scraped_articles)))"

            try:
                storage_result = await ingest_scraped_articles_to_s3(
                    scraped_articles, self.s3_config
                )

                print(" S3 Storage completed:")
                print(f"   Status: {storage_result['status']}")
                print(f"   Stored: {storage_result['stored_articles']}")
                print(f"   Failed: {storage_result[f'ailed_articles']}")

                return {
                    "scraping": {
                        "total_urls": len(urls),
                        "successful_scrapes": len(scraped_articles),
                        f"ailed_scrapes": len(scraping_errors),
                        "scraping_errors": scraping_errors,
                    },
                    "storage": storage_result,
                }

            except Exception as e:
                print("❌ S3 storage failed: {0}".format(e))
                return {
                    "scraping": {
                        "total_urls": len(urls),
                        "successful_scrapes": len(scraped_articles),
                        f"ailed_scrapes": len(scraping_errors),
                        "scraping_errors": scraping_errors,
                    },
                    "storage": {"status": "error", "error": str(e)},
                }
        else:
            print("⚠️  No articles to store or S3 storage unavailable")
            return {
                "scraping": {
                    "total_urls": len(urls),
                    "successful_scrapes": len(scraped_articles),
                    f"ailed_scrapes": len(scraping_errors),
                    "scraping_errors": scraping_errors,
                },
                "storage": {
                    "status": "skipped",
                    "reason": "No articles or S3 unavailable",
                },
            }


    async def _scrape_single_url(self, url: str) -> Dict[str, Any]:
        """
        Scrape a single URL and return structured article data.

        This is a simplified example. In production, you would use
        the full scraping pipeline with proper content extraction.
        """
        try:
            if not self.scraper:
                raise Exception("Scraper not initialized")

            # For demo purposes, we'll create mock article data'
            # In production, this would use the actual scraper
            article_data = {
                "title": "Article from {0}".format(url),
                "content": "This is sample content scraped from {0}. ".format(url) * 20,
                "url": url,
                "source": self._extract_domain(url),
                "published_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "scraped_date": datetime.now(timezone.utc).isoformat(),
                "author": "Unknown",
                "tags": ["news", "scraped"],
                "metadata": {"scraper_version": "1.0", "extraction_method": "demo"},
            }

            return article_data

        except Exception as e:
            print("Error scraping {0}: {1}".format(url, e))
            return None


    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL."""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            return parsed.netloc
        except:
            return "unknown"


    async def process_and_store_articles(
        self, raw_article_keys: List[str]
    ) -> Dict[str, Any]:
        """
        Process raw articles and store processed versions in S3.

        Args:
            raw_article_keys: List of S3 keys for raw articles

        Returns:
            Processing results
        """
        print("
⚙️  Processing {0} raw articles...".format(len(raw_article_keys)))"

        processed_count = 0
        processing_errors = []

        for key in raw_article_keys:
            try:
                # Retrieve raw article
                raw_article = await self.s3_storage.retrieve_article(key)

                # Process article (simplified example)
                processed_article = await self._process_article(raw_article)

                # Store processed article
                processing_metadata = {
                    "processing_date": datetime.now(timezone.utc).isoformat(),
                    "original_s3_key": key,
                    "processing_pipeline": "demo_nlp_v1.0",
                }

                await self.s3_storage.store_processed_article(
                    processed_article, processing_metadata
                )

                processed_count += 1
                print(
                    f"    Processed: {processed_article.get('title', 'Unknown')[:50]}..."
                )

            except Exception as e:
                processing_errors.append("Error processing {0}: {1}".format(key, str(e)))
                print("   ❌ Error processing {0}: {1}".format(key, e))

        return {
            "total_articles": len(raw_article_keys),
            "processed_successfully": processed_count,
            "processing_errors": len(processing_errors),
            "errors": processing_errors,
        }


    async def _process_article(self, raw_article: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a raw article (simplified NLP pipeline).

        In production, this would include:
        - Sentiment analysis
        - Entity extraction
        - Summarization
        - Topic classification
        - etc.
        """
        content = raw_article.get("content", "")"

        # Simplified processing
        processed_article = {
            **raw_article,
            "processed": True,
            "word_count": len(content.split()),
            "char_count": len(content),
            "sentiment_score": 0.5,  # Neutral (demo value)
            "summary": content[:200] + "..." if len(content) > 200 else content,
            "key_entities": ["demo", "entity1", "entity2"],
            "topics": ["general", "news"],
            "processing_metadata": {"nlp_model": "demo_model_v1.0", "confidence": 0.85},
        }

        return processed_article

    async def get_storage_overview(self) -> Dict[str, Any]:
        """Get comprehensive storage overview."""
        if not self.s3_storage or not self.s3_storage.s3_client:
            return {"error": "S3 storage not available"}

        try:
            stats = await self.s3_storage.get_storage_statistics()

            # Get recent articles
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            recent_raw = await self.s3_storage.list_articles_by_date(
                today, ArticleType.RAW, limit=10
            )
            recent_processed = await self.s3_storage.list_articles_by_date(
                today, ArticleType.PROCESSED, limit=10
            )

            return {
                "statistics": stats,
                "recent_articles": {"raw": recent_raw, "processed": recent_processed},
                "date": today,
            }

        except Exception as e:
            return {"error": str(e)}


    async def cleanup(self):
        """Cleanup resources."""
        if self.scraper:
            await self.scraper.close()
            print(" Scraper cleaned up")


async def demo_integration():
    """Demonstrate the S3 integration."""
    print(" NeuroNews S3 Integration Demo")
    print("=" * 50)

    # Configuration
    s3_config = S3StorageConfig(
        bucket_name="neuronews-articles-demo",
        region="us-east-1",
        raw_prefix="raw_articles",
        processed_prefix="processed_articles",
    )

    # Initialize integration
    integration = NeuroNewsS3Integration(s3_config)
    await integration.initialize()

    try:
        # Demo URLs (these would be real news URLs in production)
        demo_urls = [
            "https://example.com/tech-news-1",
            "https://example.com/health-news-2",
            "https://example.com/science-news-3",
        ]

        # Scrape and store articles
        result = await integration.scrape_and_store_articles(demo_urls)

        print(""
 Integration Results:")"
        print(json.dumps(result, indent=2))

        # Get storage overview
        overview = await integration.get_storage_overview()
        print(""
 Storage Overview:")"
        print(json.dumps(overview, indent=2))

        # Demo processing (if we have stored articles)
        if result.get("storage", {}).get("stored_keys"):
            stored_keys = result["storage"]["stored_keys"]
            if stored_keys:
                print(""
⚙️  Processing stored articles...")"
                processing_result = await integration.process_and_store_articles(
                    stored_keys[:2]
                )
                print("Processing Results: {0}".format(processing_result))

    finally:
        await integration.cleanup()

    print(""
✨ Integration demo completed!")"


if __name__ == "__main__":
    print("Starting NeuroNews S3 Integration Demo...")
    try:
        asyncio.run(demo_integration())
    except KeyboardInterrupt:
        print(""

⏹️  Demo interrupted by user")"
    except Exception as e:
        print(""

❌ Demo failed: {0}".format(e))"
        import traceback

        traceback.print_exc()

    print(""
Demo completed! ")"
