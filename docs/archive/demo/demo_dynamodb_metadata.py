"""
Comprehensive Demo for DynamoDB Article Metadata Manager - Issue #23

This demo showcases all functionality implemented for Issue #23:
1. Store article metadata (title, source, published_date, tags) in DynamoDB
2. Implement query API for quick lookups
3. Enable full-text search capabilities

Features demonstrated:
- Article metadata indexing (single and batch)
- Quick lookups by source, date, category, tags
- Full-text search with various modes
- Integration with S3 and Redshift systems
- Performance monitoring and statistics
- Health checks and error handling
"""

from src.database.dynamodb_metadata_manager import (
    ArticleMetadataIndex, DynamoDBMetadataConfig, DynamoDBMetadataManager,
    SearchMode, SearchQuery, integrate_with_redshift_etl,
    integrate_with_s3_storage, sync_metadata_from_scraper)
import asyncio
import json
import logging
import os
# Import DynamoDB metadata manager
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))


# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class DynamoDBMetadataDemo:
    """Comprehensive demo for DynamoDB article metadata indexing."""

    def __init__(self):
        """Initialize demo with configuration."""
        self.config = DynamoDBMetadataConfig(
            table_name="neuronews-metadata-demo",
            region="us-east-1",
            read_capacity_units=5,
            write_capacity_units=5,
            batch_size=25,
            enable_full_text_search=True,
            create_indexes=True,
        )

        # Demo will use mock data (no actual AWS resources needed)
        self.use_mock = True
        self.manager = None

        # Sample articles for demonstration
        self.sample_articles = self._create_sample_articles()

    def _create_sample_articles(self) -> List[Dict[str, Any]]:
        """Create diverse sample articles for demonstration."""
        base_date = datetime.now(timezone.utc)

        return [
            {
                "id": "ai-healthcare-breakthrough-001",
                "title": "AI Revolution Transforms Healthcare Diagnostics",
                "source": "HealthTech Weekly",
                "published_date": (base_date - timedelta(days=1)).strftime("%Y-%m-%d"),
                "tags": ["AI", "Healthcare", "Diagnostics", "Innovation"],
                "url": "https://healthtech.com/ai-diagnostics-revolution",
                "author": "Dr. Sarah Chen",
                "category": "Technology",
                "language": "en",
                "content": """
                Artificial intelligence is revolutionizing healthcare diagnostics with unprecedented accuracy.
                New AI models can detect diseases earlier than traditional methods, potentially saving millions of lives.
                The breakthrough involves deep learning algorithms trained on vast medical datasets.
                """,
                "word_count": 1250,
                "scraped_date": base_date.isoformat(),
                "sentiment_score": 0.85,
                "validation_score": 92,
                "content_quality": "high", "
            },
            {
                "id": "quantum-computing-milestone-002",
                "title": "Quantum Computing Achieves New Milestone in Error Correction",
                "source": "Science Frontier",
                "published_date": (base_date - timedelta(days=2)).strftime("%Y-%m-%d"),
                "tags": ["Quantum Computing", "Science", "Research", "Technology"],
                "url": "https://sciencefrontier.com/quantum-error-correction",
                "author": "Prof. Michael Rodriguez",
                "category": "Science",
                "language": "en",
                "content": """
                Scientists achieve breakthrough in quantum error correction, bringing practical quantum computers closer.
                The new method reduces error rates by 99%, making quantum supremacy more achievable.
                This advancement could revolutionize cryptography, drug discovery, and optimization problems.
                """,
                "word_count": 980,
                "scraped_date": base_date.isoformat(),
                "sentiment_score": 0.78,
                "validation_score": 88,
                "content_quality": "high","
            },
            {
                "id": "climate-tech-innovation-003",
                "title": "Revolutionary Solar Panel Technology Doubles Efficiency",
                "source": "Green Energy Today",
                "published_date": (base_date - timedelta(days=3)).strftime("%Y-%m-%d"),
                "tags": ["Solar Energy", "Climate", "Renewable Energy", "Innovation"],
                "url": "https://greenenergy.com/solar-efficiency-breakthrough",
                "author": "Emma Watson",
                "category": "Environment",
                "language": "en",
                "content": """
                New perovskite-silicon tandem solar cells achieve record 47% efficiency in laboratory tests.
                This breakthrough could make solar energy more affordable and accelerate clean energy adoption.
                Commercial applications expected within 3-5 years, potentially transforming energy sector.
                """,
                "word_count": 1100,
                "scraped_date": base_date.isoformat(),
                "sentiment_score": 0.82,
                "validation_score": 85,
                "content_quality": "high","
            },
            {
                "id": "space-exploration-mars-004",
                "title": "NASA Mars Mission Discovers Evidence of Ancient Water Systems",
                "source": "Space News Network",
                "published_date": (base_date - timedelta(days=4)).strftime("%Y-%m-%d"),
                "tags": ["Space", "Mars", "NASA", "Exploration", "Discovery"],
                "url": "https://spacenews.com/mars-water-discovery",
                "author": "Dr. James Thompson",
                "category": "Science",
                "language": "en",
                "content": """
                Latest Mars rover data reveals extensive ancient water systems across the red planet.
                Geological evidence suggests Mars once had a complex hydrological cycle similar to Earth.
                Discovery increases likelihood of finding evidence of past microbial life on Mars.
                """,
                "word_count": 890,
                "scraped_date": base_date.isoformat(),
                "sentiment_score": 0.75,
                "validation_score": 90,
                "content_quality": "high","
            },
            {
                "id": "cybersecurity-ai-defense-005",
                "title": "AI-Powered Cybersecurity Defense Blocks 99.9% of Attacks",
                "source": "CyberSec Today",
                "published_date": (base_date - timedelta(days=5)).strftime("%Y-%m-%d"),
                "tags": ["Cybersecurity", "AI", "Defense", "Technology"],
                "url": "https://cybersec.com/ai-defense-system",
                "author": "Alex Rivera",
                "category": "Technology",
                "language": "en",
                "content": """
                New AI-powered cybersecurity system demonstrates unprecedented threat detection capabilities.
                Machine learning algorithms identify and neutralize zero-day attacks in real-time.
                Enterprise deployment shows 99.9% success rate against advanced persistent threats.
                """,
                "word_count": 1050,
                "scraped_date": base_date.isoformat(),
                "sentiment_score": 0.80,
                "validation_score": 87,
                "content_quality": "high","
            },
            {
                "id": "biotech-gene-therapy-006",
                "title": "Gene Therapy Breakthrough Offers Hope for Rare Diseases",
                "source": "BioTech Review",
                "published_date": (base_date - timedelta(days=6)).strftime("%Y-%m-%d"),
                "tags": ["Biotechnology", "Gene Therapy", "Medicine", "Research"],
                "url": "https://biotechreview.com/gene-therapy-breakthrough",
                "author": "Dr. Lisa Park",
                "category": "Medicine",
                "language": "en",
                "content": """
                Clinical trials show promising results for CRISPR-based gene therapy treating rare genetic disorders.
                New delivery method increases efficiency while reducing side effects significantly.
                FDA approval expected within 18 months for first commercial gene therapy treatment.
                """,
                "word_count": 920,
                "scraped_date": base_date.isoformat(),
                "sentiment_score": 0.88,
                "validation_score": 93,
                "content_quality": "high","
            },
        ]

    async def run_comprehensive_demo(self):
        """Run complete demonstration of all features."""
        print(" Starting DynamoDB Article Metadata Manager Demo (Issue #23)")
        print("=" * 70)

        try:
            # Initialize manager
            await self._demo_initialization()

            # Demo 1: Article metadata indexing
            await self._demo_article_indexing()

            # Demo 2: Query API for quick lookups
            await self._demo_query_api()

            # Demo 3: Full-text search capabilities
            await self._demo_full_text_search()

            # Demo 4: Integration with existing systems
            await self._demo_system_integration()

            # Demo 5: Performance and statistics
            await self._demo_performance_monitoring()

            # Demo 6: Health checks and error handling
            await self._demo_health_monitoring()

            print(""
 Demo completed successfully!")
            print("All Issue #23 requirements demonstrated:")"
            print(
                " Store article metadata (title, source, published_date, tags) in DynamoDB"
            )
            print(" Implement query API for quick lookups")
            print(" Enable full-text search capabilities")

        except Exception as e:
            logger.error("Demo failed: {0}".format(e))
            raise


    async def _demo_initialization(self):
        """Demo manager initialization and table setup."""
        print(""
1️⃣ DynamoDB Manager Initialization")
        print("-" * 40)"

        start_time = time.time()

        if self.use_mock:
            print("📝 Using mock DynamoDB for demo (no AWS resources required)")
            self.manager = MockDynamoDBManager(self.config)
        else:
            print("🔗 Connecting to DynamoDB...")
            self.manager = DynamoDBMetadataManager(self.config)

        setup_time = (time.time() - start_time) * 1000

        print(" Manager initialized successfully ({0}ms)".format(setup_time:.2f))
        print(" Table: {0}".format(self.config.table_name))
        print("🌍 Region: {0}".format(self.config.region))
        print(
            f" Full-text search: {'Enabled' if self.config.enable_full_text_search else 'Disabled'}"
        )
        print(f" Indexes: {'Enabled' if self.config.create_indexes else 'Disabled'}")


    async def _demo_article_indexing(self):
        """Demo article metadata indexing functionality."""
        print(""
2️⃣ Article Metadata Indexing")
        print("-" * 40)"

        # Demo single article indexing
        print(""
📄 Indexing single article...")"
        start_time = time.time()

        single_article = self.sample_articles[0]
        metadata = await self.manager.index_article_metadata(single_article)

        indexing_time = (time.time() - start_time) * 1000

        print(" Article indexed: {0}".format(metadata.article_id))
        print("📝 Title: {0}".format(metadata.title))
        print("📰 Source: {0}".format(metadata.source))
        print("📅 Published: {0}".format(metadata.published_date))
        print(f"🏷️ Tags: {', '.join(metadata.tags)}")
        print("⚡ Indexing time: {0}ms".format(indexing_time:.2f))

        # Demo batch indexing
        print(""
📚 Batch indexing multiple articles...")"
        start_time = time.time()

        batch_articles = self.sample_articles[1:]  # Remaining articles
        batch_result = await self.manager.batch_index_articles(batch_articles)

        batch_time = (time.time() - start_time) * 1000

        print(" Batch indexing completed")
        print(f" Total articles: {batch_result['total_articles']}")
        print(f" Successfully indexed: {batch_result['indexed_count']}")
        print(f"❌ Failed: {batch_result[f'ailed_count']}")
        print("⚡ Batch time: {0}ms".format(batch_time:.2f))
        print(f" Indexing rate: {batch_result['indexing_rate']:.1f} articles/second")

        # Show tokenization example
        print("
🔤 Title tokenization example:")
        print(f"Original: '{metadata.title}'")
        print("Tokens: {0}".format(metadata.title_tokens))


    async def _demo_query_api(self):
        """Demo query API for quick lookups."""
        print(""
3️⃣ Query API for Quick Lookups")
        print("-" * 40)"

        # Query by ID
        print(""
 Get article by ID...")
        article_id = self.sample_articles[0]["id"]"
        start_time = time.time()

        article = await self.manager.get_article_by_id(article_id)
        query_time = (time.time() - start_time) * 1000

        if article:
            print(" Found article: {0}".format(article.title))
            print("⚡ Query time: {0}ms".format(query_time:.2f))
        else:
            print("❌ Article not found")

        # Query by source
        print(""
📰 Get articles by source...")
        source = "HealthTech Weekly""
        start_time = time.time()

        source_result = await self.manager.get_articles_by_source(source, limit=10)
        query_time = (time.time() - start_time) * 1000

        print(f" Found {source_result.count} articles from '{source}'")
        print("⚡ Query time: {0}ms".format(query_time:.2f))

        for article in source_result.items[:3]:  # Show first 3
            print("  📄 {0}".format(article.title))

        # Query by date range
        print(""
📅 Get articles by date range...")"
        start_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start_time = time.time()

        date_result = await self.manager.get_articles_by_date_range(
            start_date, end_date
        )
        query_time = (time.time() - start_time) * 1000

        print(" Found {0} articles from {1} to {2}".format(date_result.count, start_date, end_date))
        print("⚡ Query time: {0}ms".format(query_time:.2f))

        # Query by tags
        print(""
🏷️ Get articles by tags...")
        tags = ["AI", "Technology"]"
        start_time = time.time()

        tag_result = await self.manager.get_articles_by_tags(tags, match_all=False)
        query_time = (time.time() - start_time) * 1000

        print(f" Found {tag_result.count} articles with tags: {', '.join(tags)}")
        print("⚡ Query time: {0}ms".format(query_time:.2f))

        # Query by category
        print(""
📂 Get articles by category...")
        category = "Technology""
        start_time = time.time()

        category_result = await self.manager.get_articles_by_category(category)
        query_time = (time.time() - start_time) * 1000

        print(f" Found {category_result.count} articles in '{category}' category")
        print("⚡ Query time: {0}ms".format(query_time:.2f))


    async def _demo_full_text_search(self):
        """Demo full-text search capabilities."""
        print(""
4️⃣ Full-Text Search Capabilities")
        print("-" * 40)"

        # Basic search
        print(""
 Basic full-text search...")"
        search_query = SearchQuery(
            query_text="AI healthcare revolution",
            fields=["title", "content_summary"],
            search_mode=SearchMode.CONTAINS,
            limit=10,
        )

        start_time = time.time()
        search_result = await self.manager.search_articles(search_query)
        search_time = (time.time() - start_time) * 1000

        print(f" Search completed: '{search_query.query_text}'")
        print(" Found {0} matching articles".format(search_result.count))
        print("⚡ Search time: {0}ms".format(search_time:.2f))
        print(f"🔤 Search tokens: {search_result.query_info.get('tokens', [])}")

        for i, article in enumerate(search_result.items[:3], 1):
            print("  {0}. {1} (Source: {2})".format(i, article.title, article.source))

        # Search with filters
        print(""
 Search with filters...")"
        filtered_search = SearchQuery(
            query_text="breakthrough technology",
            fields=["title", "content_summary"],
            search_mode=SearchMode.CONTAINS,
            filters={"category": "Technology"},
            date_range={
                "start": (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
                    "%Y-%m-%d"
                ),
                "end": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            },
            limit=5,
        )

        start_time = time.time()
        filtered_result = await self.manager.search_articles(filtered_search)
        search_time = (time.time() - start_time) * 1000

        print(" Filtered search completed")
        print(" Found {0} matching articles".format(filtered_result.count))
        print("⚡ Search time: {0}ms".format(search_time:.2f))
        print("🔧 Filters applied: category=Technology, date range")

        # Exact search mode
        print(""
 Exact search mode...")"
        exact_search = SearchQuery(
            query_text="Quantum Computing",
            fields=["title"],
            search_mode=SearchMode.EXACT,
            limit=5,
        )

        start_time = time.time()
        exact_result = await self.manager.search_articles(exact_search)
        search_time = (time.time() - start_time) * 1000

        print(" Exact search completed")
        print(" Found {0} exact matches".format(exact_result.count))
        print("⚡ Search time: {0}ms".format(search_time:.2f))

        # Starts-with search mode
        print(""
 Starts-with search mode...")"
        prefix_search = SearchQuery(
            query_text="AI",
            fields=["title"],
            search_mode=SearchMode.STARTS_WITH,
            limit=5,
        )

        start_time = time.time()
        prefix_result = await self.manager.search_articles(prefix_search)
        search_time = (time.time() - start_time) * 1000

        print(" Prefix search completed")
        print(f" Found {prefix_result.count} articles starting with 'AI'")
        print("⚡ Search time: {0}ms".format(search_time:.2f))


    async def _demo_system_integration(self):
        """Demo integration with existing systems."""
        print(""
5️⃣ System Integration")
        print("-" * 40)"

        # S3 integration
        print(""
☁️ S3 Storage Integration...")"
        s3_metadata = {
            "article_id": "integration-test-001",
            "title": "Integration Test Article",
            "source": "Integration Source",
            "published_date": "2025-08-13",
            "url": "https://example.com/integration-test",
            "s3_key": "raw_articles/2025/08/13/integration-test.json",
            "content_hash": "sha256-integration-hash",
            "scraped_date": datetime.now(timezone.utc).isoformat(),
            "processing_status": "stored",
        }

        start_time = time.time()
        s3_result = await integrate_with_s3_storage(s3_metadata, self.manager)
        integration_time = (time.time() - start_time) * 1000

        print(" S3 integration successful")
        print("📄 Article: {0}".format(s3_result.title))
        print("🗂️ S3 Key: {0}".format(s3_result.s3_key))
        print("⚡ Integration time: {0}ms".format(integration_time:.2f))

        # Redshift integration
        print(""
 Redshift ETL Integration...")"
        redshift_record = {
            "article_id": s3_result.article_id,
            "title": s3_result.title,
            "processed_date": datetime.now(timezone.utc).isoformat(),
        }

        start_time = time.time()
        redshift_success = await integrate_with_redshift_etl(
            redshift_record, self.manager
        )
        integration_time = (time.time() - start_time) * 1000

        print(f" Redshift integration: {'Success' if redshift_success else 'Failed'}")
        print("⚡ Integration time: {0}ms".format(integration_time:.2f))

        # Scraper sync
        print(""
🕷️ Scraper Sync...")"
        scraper_articles = [
            {
                "title": "Scraper Article 1",
                "source": "Scraped Source",
                "published_date": "2025-08-13",
                "tags": ["Scraper", "Test"],
            },
            {
                "title": "Scraper Article 2",
                "source": "Scraped Source",
                "published_date": "2025-08-13",
                "tags": ["Scraper", "Demo"],
            },
        ]

        start_time = time.time()
        sync_result = await sync_metadata_from_scraper(scraper_articles, self.manager)
        sync_time = (time.time() - start_time) * 1000

        print(" Scraper sync completed")
        print(
            f" Articles synced: {sync_result['indexed_count']}/{sync_result['total_articles']}"
        )
        print("⚡ Sync time: {0}ms".format(sync_time:.2f))


    async def _demo_performance_monitoring(self):
        """Demo performance monitoring and statistics."""
        print(""
6️⃣ Performance Monitoring & Statistics")
        print("-" * 40)"

        # Get metadata statistics
        print(""
 Generating metadata statistics...")"
        start_time = time.time()

        stats = await self.manager.get_metadata_statistics()
        stats_time = (time.time() - start_time) * 1000

        print(" Statistics generated ({0}ms)".format(stats_time:.2f))
        print(f"📄 Total articles: {stats['total_articles']}")
        print(f"🔬 Sample size: {stats['sample_size']}")

        print(""
📰 Top sources:")
        for source, count in list(stats["source_distribution"].items())[:5]:
            print("  • {0}: {1} articles".format(source, count))"

        print(""
📂 Categories:")
        for category, count in stats["category_distribution"].items():
            print("  • {0}: {1} articles".format(category, count))"

        print(""
📅 Monthly distribution:")
        for month, count in list(stats["monthly_distribution"].items())[:6]:
            print("  • {0}: {1} articles".format(month, count))"

        print("
🏗️ Table info:")
        print(f"  • Table: {stats['table_info']['table_name']}")
        print(f"  • Region: {stats['table_info']['region']}")
        print(
            f"  • Indexes: {'Enabled' if stats['table_info']['indexes_enabled'] else 'Disabled'}"
        )


    async def _demo_health_monitoring(self):
        """Demo health checks and error handling."""
        print(""
7️⃣ Health Monitoring & Error Handling")
        print("-" * 40)"

        # Health check
        print(""
🏥 Performing health check...")"
        start_time = time.time()

        health = await self.manager.health_check()
        health_time = (time.time() - start_time) * 1000

        print(" Health check completed ({0}ms)".format(health_time:.2f))
        print(f"🔋 Status: {health['status']}")
        print(f" Table status: {health.get('table_status', 'Unknown')}")
        print(f"📖 Read capacity: {health.get('read_capacity', 'N/A')}")
        print(f"✏️ Write capacity: {health.get('write_capacity', 'N/A')}")
        print(f"📄 Item count: {health.get('item_count', 'N/A')}")
        print(f"💾 Table size: {health.get('table_size_bytes', 'N/A')} bytes")
        print(f" Indexes: {health.get('indexes', 'N/A')}")

        # Test update functionality
        print(""
🔄 Testing metadata updates...")
        test_article_id = self.sample_articles[0]["id"]"
        updates = {
            "sentiment_score": 0.95,
            "content_quality": "excellent",
            "validation_score": 98,
        }

        start_time = time.time()
        update_success = await self.manager.update_article_metadata(
            test_article_id, updates
        )
        update_time = (time.time() - start_time) * 1000

        print(f" Update test: {'Success' if update_success else 'Failed'}")
        print("⚡ Update time: {0}ms".format(update_time:.2f))

        # Verify update
        updated_article = await self.manager.get_article_by_id(test_article_id)
        if updated_article:
            print(
                " Verified updated sentiment score: {0}".format(updated_article.sentiment_score)
            )
            print(" Verified updated quality: {0}".format(updated_article.content_quality))


class MockDynamoDBManager:
    """Mock DynamoDB manager for demo without AWS resources."""


    def __init__(self, config: DynamoDBMetadataConfig):
        self.config = config
        self.table_name = config.table_name
        self.mock_data = {}
        self.logger = logging.getLogger(__name__)


    async def index_article_metadata(
        self, article_data: Dict[str, Any]
    ) -> ArticleMetadataIndex:
        """Mock article indexing."""
        # Create metadata from article data
        metadata = self._create_metadata_from_article(article_data)

        # Store in mock data
        self.mock_data[metadata.article_id] = metadata

        return metadata

    async def batch_index_articles(
        self, articles: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Mock batch indexing."""
        indexed_count = 0

        for article in articles:
            try:
                await self.index_article_metadata(article)
                indexed_count += 1
            except:
                pass

        return {
            "status": "completed",
            "total_articles": len(articles),
            "indexed_count": indexed_count,
            f"ailed_count": len(articles) - indexed_count,
            f"ailed_articles": [],
            "execution_time_ms": 50.0,
            "indexing_rate": len(articles) / 0.05,
        }


    async def get_article_by_id(self, article_id: str):
        """Mock get article by ID."""
        return self.mock_data.get(article_id)


    async def get_articles_by_source(self, source: str, **kwargs):
        """Mock get articles by source."""
        items = [item for item in self.mock_data.values() if item.source == source]
        from src.database.dynamodb_metadata_manager import QueryResult

        return QueryResult(items=items, count=len(items), execution_time_ms=25.0)

    async def get_articles_by_date_range(
        self, start_date: str, end_date: str, **kwargs
    ):
        """Mock get articles by date range."""
        items = [
            item
            for item in self.mock_data.values()
            if start_date <= item.published_date <= end_date
        ]
        from src.database.dynamodb_metadata_manager import QueryResult

        return QueryResult(items=items, count=len(items), execution_time_ms=30.0)

    async def get_articles_by_tags(self, tags: List[str], **kwargs):
        """Mock get articles by tags."""
        items = [
            item
            for item in self.mock_data.values()
            if any(tag in item.tags for tag in tags)
        ]
        from src.database.dynamodb_metadata_manager import QueryResult

        return QueryResult(items=items, count=len(items), execution_time_ms=20.0)

    async def get_articles_by_category(self, category: str, **kwargs):
        """Mock get articles by category."""
        items = [item for item in self.mock_data.values() if item.category == category]
        from src.database.dynamodb_metadata_manager import QueryResult

        return QueryResult(items=items, count=len(items), execution_time_ms=22.0)

    async def search_articles(self, search_query):
        """Mock search articles."""
        # Simple mock search
        query_tokens = search_query.query_text.lower().split()
        items = []

        for item in self.mock_data.values():
            title_lower = item.title.lower()
            if any(token in title_lower for token in query_tokens):
                items.append(item)

        from src.database.dynamodb_metadata_manager import QueryResult

        return QueryResult(
            items=items,
            count=len(items),
            execution_time_ms=35.0,
            query_info={
                "query": search_query.query_text,
                "tokens": query_tokens,
                "search_mode": search_query.search_mode.value,
                f"ields": search_query.fields,
            },
        )


    async def get_metadata_statistics(self):
        """Mock metadata statistics."""
        items = list(self.mock_data.values())

        sources = {}
        categories = {}
        monthly = {}

        for item in items:
            sources[item.source] = sources.get(item.source, 0) + 1
            categories[item.category] = categories.get(item.category, 0) + 1
            month = item.published_date[:7] if item.published_date else "unknown"
            monthly[month] = monthly.get(month, 0) + 1

        return {
            "total_articles": len(items),
            "sample_size": len(items),
            "source_distribution": sources,
            "category_distribution": categories,
            "monthly_distribution": monthly,
            "execution_time_ms": 45.0,
            "table_info": {
                "table_name": self.table_name,
                "region": self.config.region,
                "indexes_enabled": self.config.create_indexes,
            },
        }


    async def health_check(self):
        """Mock health check."""
        return {
            "status": "healthy",
            "table_status": "ACTIVE",
            "table_name": self.table_name,
            "region": self.config.region,
            "read_capacity": self.config.read_capacity_units,
            "write_capacity": self.config.write_capacity_units,
            "item_count": len(self.mock_data),
            "table_size_bytes": len(self.mock_data) * 1024,
            "indexes": 3,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


    async def update_article_metadata(
        self, article_id: str, updates: Dict[str, Any]
    ) -> bool:
        """Mock update article metadata."""
        if article_id in self.mock_data:
            item = self.mock_data[article_id]
            for key, value in updates.items():
                setattr(item, key, value)
            return True
        return False


    def _create_metadata_from_article(self, article_data: Dict[str, Any]):
        """Create metadata from article data."""
        import hashlib

        from src.database.dynamodb_metadata_manager import ArticleMetadataIndex

        article_id = article_data.get("id") or article_data.get("article_id")
        if not article_id:
            content_for_id = (
                f"{article_data.get('url', '')}{article_data.get('title', '')}"
            )
            article_id = hashlib.md5(content_for_id.encode()).hexdigest()

        content_hash = article_data.get("content_hash")
        if not content_hash:
            content_for_hash = article_data.get("content", "") or article_data.get(
                "title", ""
            )
            content_hash = hashlib.sha256(content_for_hash.encode()).hexdigest()

        tags = article_data.get("tags", [])
        if not isinstance(tags, list):
            tags = [tags] if tags else []

        return ArticleMetadataIndex(
            article_id=article_id,
            content_hash=content_hash,
            title=article_data.get("title", ""),
            source=article_data.get("source", ""),
            published_date=article_data.get("published_date", ""),
            tags=tags,
            url=article_data.get("url", ""),
            author=article_data.get("author", ""),
            category=article_data.get("category", ""),
            content_summary=(
                article_data.get("content", "")[:200]
                if article_data.get("content")
                else ""
            ),
            word_count=article_data.get("word_count", 0),
            sentiment_score=article_data.get("sentiment_score"),
            scraped_date=article_data.get("scraped_date", ""),
            validation_score=article_data.get("validation_score", 0),
            content_quality=article_data.get("content_quality", "unknown"),
        )


async def main():
    """Run the comprehensive demo."""
    demo = DynamoDBMetadataDemo()
    await demo.run_comprehensive_demo()


if __name__ == "__main__":
    asyncio.run(main())
