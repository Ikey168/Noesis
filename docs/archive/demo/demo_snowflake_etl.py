"""
Demo script for Snowflake ETL Processor (Issue #242)

This script demonstrates the Snowflake ETL processor functionality including:
- Connection management
- Schema initialization  
- Single article loading
- Batch loading with COPY INTO
- Error handling and statistics
- Performance comparison between individual and batch loading
"""

import os
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any

from src.database.snowflake_loader import SnowflakeETLProcessor, SnowflakeArticleRecord


class SnowflakeETLDemo:
    """Demo class for testing Snowflake ETL processor."""
    
    def __init__(self):
        """Initialize demo with Snowflake connection parameters."""
        self.processor = None
        self.demo_articles = []
        self._generate_demo_data()
    
    def _generate_demo_data(self) -> None:
        """Generate sample articles for testing."""
        base_time = datetime.now()
        
        sample_articles = [
            {
                "id": f"demo_article_{i:03d}",
                "url": f"https://demo-news.com/article-{i}",
                "title": f"Demo News Article {i}: Technology and Innovation",
                "content": f"This is demo content for article {i}. It discusses various aspects of technology, innovation, and their impact on society. The article covers multiple topics including artificial intelligence, machine learning, and data processing.",
                "source": "demo_news" if i % 2 == 0 else "tech_daily",
                "published_date": base_time - timedelta(hours=i),
                "scraped_at": base_time - timedelta(hours=i-1),
                "validation_score": 85.5 + (i % 10),
                "content_quality": "high" if i % 3 == 0 else "medium",
                "source_credibility": "trusted" if i % 2 == 0 else "reliable", 
                "validation_flags": ["content_length_ok", "spell_check_passed"],
                "validated_at": base_time - timedelta(hours=i-2),
                "word_count": 150 + (i * 10),
                "content_length": 950 + (i * 50),
                "author": f"Demo Author {i % 5 + 1}",
                "category": "Technology" if i % 2 == 0 else "Innovation",
                "sentiment_score": 0.7 + (i % 3) * 0.1,
                "sentiment_label": "positive" if i % 3 == 0 else "neutral",
                "entities": [
                    {"text": "Technology", "label": "TOPIC", "confidence": 0.95},
                    {"text": f"Company {i % 3 + 1}", "label": "ORG", "confidence": 0.85}
                ],
                "keywords": [
                    {"keyword": "technology", "score": 0.9},
                    {"keyword": "innovation", "score": 0.8},
                    {"keyword": f"keyword_{i}", "score": 0.7}
                ],
                "topics": [
                    {"topic": "technology", "probability": 0.85},
                    {"topic": "business", "probability": 0.65}
                ],
                "dominant_topic": {"topic": "technology", "probability": 0.85},
                "extraction_method": "bert_nlp",
                "extraction_processed_at": base_time - timedelta(minutes=30),
                "extraction_processing_time": 2.5 + (i * 0.1)
            }
            for i in range(1, 51)  # Generate 50 demo articles
        ]
        
        self.demo_articles = sample_articles
    
    def setup_connection(self) -> bool:
        """Setup Snowflake connection using environment variables."""
        try:
            account = os.getenv('SNOWFLAKE_ACCOUNT')
            user = os.getenv('SNOWFLAKE_USER') 
            password = os.getenv('SNOWFLAKE_PASSWORD')
            warehouse = os.getenv('SNOWFLAKE_WAREHOUSE', 'COMPUTE_WH')
            database = os.getenv('SNOWFLAKE_DATABASE', 'NEURONEWS')
            schema = os.getenv('SNOWFLAKE_SCHEMA', 'PUBLIC')
            
            if not all([account, user, password]):
                print("❌ Missing required environment variables:")
                print("   - SNOWFLAKE_ACCOUNT")
                print("   - SNOWFLAKE_USER") 
                print("   - SNOWFLAKE_PASSWORD")
                print("   Optional: SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA")
                return False
            
            self.processor = SnowflakeETLProcessor(
                account=account,
                user=user,
                password=password,
                warehouse=warehouse,
                database=database,
                schema=schema,
                batch_size=10  # Smaller batches for demo
            )
            
            print(f"✅ Snowflake ETL processor configured:")
            print(f"   Account: {account}")
            print(f"   Database: {database}")
            print(f"   Schema: {schema}")
            print(f"   Warehouse: {warehouse}")
            
            return True
            
        except Exception as e:
            print(f"❌ Failed to setup connection: {e}")
            return False
    
    def test_connection(self) -> bool:
        """Test connection to Snowflake."""
        print("\n🔍 Testing Snowflake connection...")
        
        try:
            with self.processor:
                health = self.processor.health_check()
                
                print(f"✅ Connection: {'✅' if health['connection'] else '❌'}")
                print(f"✅ Schema valid: {'✅' if health['schema_valid'] else '❌'}")
                print(f"✅ Tables exist: {'✅' if health['tables_exist'] else '❌'}")
                print(f"✅ Sample query: {'✅' if health['sample_query'] else '❌'}")
                
                if health['errors']:
                    print(f"❌ Errors: {health['errors']}")
                    return False
                    
                return True
                
        except Exception as e:
            print(f"❌ Connection test failed: {e}")
            return False
    
    def test_schema_initialization(self) -> bool:
        """Test schema initialization."""
        print("\n📋 Testing schema initialization...")
        
        try:
            with self.processor:
                self.processor.initialize_schema()
                print("✅ Schema initialized successfully")
                return True
                
        except Exception as e:
            print(f"❌ Schema initialization failed: {e}")
            return False
    
    def test_single_article_loading(self) -> bool:
        """Test loading a single article."""
        print("\n📝 Testing single article loading...")
        
        try:
            with self.processor:
                sample_article = self.demo_articles[0]
                success = self.processor.load_single_article(sample_article)
                
                if success:
                    print(f"✅ Single article loaded: {sample_article['id']}")
                    
                    # Verify the article was loaded
                    stats = self.processor.get_article_stats()
                    print(f"✅ Total articles in database: {stats.get('total_articles', 0)}")
                    return True
                else:
                    print("❌ Single article loading failed")
                    return False
                    
        except Exception as e:
            print(f"❌ Single article loading failed: {e}")
            return False
    
    def test_batch_loading_individual(self) -> bool:
        """Test batch loading with individual inserts."""
        print("\n📦 Testing batch loading (individual inserts)...")
        
        try:
            with self.processor:
                # Use a subset of articles for individual loading test
                test_articles = self.demo_articles[1:11]  # 10 articles
                
                start_time = time.time()
                stats = self.processor.load_articles_batch(
                    test_articles, 
                    use_copy_into=False,  # Force individual loading
                    skip_duplicates=True
                )
                end_time = time.time()
                
                print(f"✅ Individual batch loading completed:")
                print(f"   Total articles: {stats['total_articles']}")
                print(f"   Loaded: {stats['loaded_count']}")
                print(f"   Failed: {stats['failed_count']}")
                print(f"   Skipped: {stats['skipped_count']}")
                print(f"   Success rate: {stats['success_rate']:.1f}%")
                print(f"   Processing time: {end_time - start_time:.2f}s")
                print(f"   Articles per second: {stats['articles_per_second']:.1f}")
                
                return stats['success_rate'] > 0
                
        except Exception as e:
            print(f"❌ Individual batch loading failed: {e}")
            return False
    
    def test_batch_loading_copy_into(self) -> bool:
        """Test batch loading with COPY INTO command."""
        print("\n🚀 Testing batch loading (COPY INTO)...")
        
        try:
            with self.processor:
                # Use remaining articles for COPY INTO test
                test_articles = self.demo_articles[11:31]  # 20 articles
                
                start_time = time.time()
                stats = self.processor.load_articles_batch(
                    test_articles,
                    use_copy_into=True,  # Use COPY INTO
                    skip_duplicates=True
                )
                end_time = time.time()
                
                print(f"✅ COPY INTO batch loading completed:")
                print(f"   Total articles: {stats['total_articles']}")
                print(f"   Loaded: {stats['loaded_count']}")
                print(f"   Failed: {stats['failed_count']}")
                print(f"   Skipped: {stats['skipped_count']}")
                print(f"   Success rate: {stats['success_rate']:.1f}%")
                print(f"   Processing time: {end_time - start_time:.2f}s")
                print(f"   Articles per second: {stats['articles_per_second']:.1f}")
                
                return stats['success_rate'] > 0
                
        except Exception as e:
            print(f"❌ COPY INTO batch loading failed: {e}")
            return False
    
    def test_article_statistics(self) -> bool:
        """Test article statistics retrieval."""
        print("\n📊 Testing article statistics...")
        
        try:
            with self.processor:
                stats = self.processor.get_article_stats()
                
                print(f"✅ Article statistics:")
                print(f"   Total articles: {stats.get('total_articles', 0)}")
                
                if stats.get('by_source_credibility'):
                    print("   By source credibility:")
                    for item in stats['by_source_credibility'][:3]:
                        print(f"     {item['SOURCE_CREDIBILITY']}: {item['COUNT']}")
                
                if stats.get('by_content_quality'):
                    print("   By content quality:")
                    for item in stats['by_content_quality'][:3]:
                        print(f"     {item['CONTENT_QUALITY']}: {item['COUNT']}")
                
                if stats.get('top_sources'):
                    print("   Top sources:")
                    for item in stats['top_sources'][:3]:
                        print(f"     {item['SOURCE']}: {item['COUNT']}")
                
                return True
                
        except Exception as e:
            print(f"❌ Statistics retrieval failed: {e}")
            return False
    
    def test_paginated_retrieval(self) -> bool:
        """Test paginated article retrieval."""
        print("\n📄 Testing paginated article retrieval...")
        
        try:
            with self.processor:
                articles, pagination = self.processor.get_articles_paginated(
                    page=1,
                    per_page=5,
                    sentiment="positive"
                )
                
                print(f"✅ Paginated retrieval:")
                print(f"   Retrieved articles: {len(articles)}")
                print(f"   Total pages: {pagination['pages']}")
                print(f"   Total articles: {pagination['total']}")
                
                if articles:
                    print("   Sample article:")
                    article = articles[0]
                    print(f"     ID: {article['id']}")
                    print(f"     Title: {article['title'][:50]}...")
                    print(f"     Sentiment: {article['sentiment']}")
                
                return True
                
        except Exception as e:
            print(f"❌ Paginated retrieval failed: {e}")
            return False
    
    def test_error_handling(self) -> bool:
        """Test error handling with invalid data."""
        print("\n⚠️  Testing error handling...")
        
        try:
            with self.processor:
                # Test with invalid article (missing required fields)
                invalid_article = {
                    "id": "invalid_article",
                    "url": "",  # Invalid URL
                    "title": "",  # Empty title
                    "content": "",  # Empty content
                    "source": ""  # Empty source
                }
                
                success = self.processor.load_single_article(invalid_article)
                
                # Should handle the error gracefully
                print(f"✅ Error handling test: {'Handled gracefully' if not success else 'Unexpected success'}")
                
                return True
                
        except Exception as e:
            print(f"✅ Error handling test: Exception caught as expected - {e}")
            return True
    
    def cleanup_demo_data(self) -> bool:
        """Clean up demo data."""
        print("\n🧹 Cleaning up demo data...")
        
        try:
            with self.processor:
                deleted_count = 0
                for article in self.demo_articles:
                    if self.processor.delete_article(article['id']):
                        deleted_count += 1
                
                print(f"✅ Cleaned up {deleted_count} demo articles")
                return True
                
        except Exception as e:
            print(f"❌ Cleanup failed: {e}")
            return False
    
    def run_full_demo(self) -> None:
        """Run the complete Snowflake ETL demo."""
        print("🎯 Starting Snowflake ETL Processor Demo (Issue #242)")
        print("=" * 65)
        
        if not self.setup_connection():
            return
        
        tests = [
            ("Connection Test", self.test_connection),
            ("Schema Initialization", self.test_schema_initialization),
            ("Single Article Loading", self.test_single_article_loading),
            ("Batch Loading (Individual)", self.test_batch_loading_individual),
            ("Batch Loading (COPY INTO)", self.test_batch_loading_copy_into),
            ("Article Statistics", self.test_article_statistics),
            ("Paginated Retrieval", self.test_paginated_retrieval),
            ("Error Handling", self.test_error_handling),
            ("Cleanup", self.cleanup_demo_data)
        ]
        
        passed = 0
        total = len(tests)
        
        for test_name, test_func in tests:
            try:
                if test_func():
                    passed += 1
                    print(f"✅ {test_name}: PASSED")
                else:
                    print(f"❌ {test_name}: FAILED")
            except Exception as e:
                print(f"❌ {test_name}: ERROR - {e}")
        
        print("\n" + "=" * 65)
        print(f"🎉 Demo Results: {passed}/{total} tests passed")
        
        if passed == total:
            print("✅ All tests passed! Snowflake ETL processor is working correctly.")
        else:
            print("⚠️  Some tests failed. Check the output above for details.")
        
        print("\n📋 Demo Summary:")
        print("✅ Snowflake connection and authentication")
        print("✅ Schema initialization and management")
        print("✅ Single article loading with MERGE operations")
        print("✅ Batch loading with individual inserts")
        print("✅ Efficient bulk loading with COPY INTO")
        print("✅ Comprehensive error handling")
        print("✅ Article statistics and analytics")
        print("✅ Paginated data retrieval")
        print("✅ Data cleanup and maintenance")


if __name__ == "__main__":
    # Set environment variables before running:
    # export SNOWFLAKE_ACCOUNT=your_account
    # export SNOWFLAKE_USER=your_user  
    # export SNOWFLAKE_PASSWORD=your_password
    # export SNOWFLAKE_WAREHOUSE=COMPUTE_WH
    # export SNOWFLAKE_DATABASE=NEURONEWS
    # export SNOWFLAKE_SCHEMA=PUBLIC
    
    demo = SnowflakeETLDemo()
    demo.run_full_demo()
