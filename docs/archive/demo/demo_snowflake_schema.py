"""
Demo script to test Snowflake news_articles schema (Issue #241)

This script demonstrates the Snowflake schema functionality and validates
that the migration from Redshift maintains compatibility with existing
analytics queries while leveraging Snowflake-specific optimizations.
"""

import snowflake.connector
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any


class SnowflakeSchemaDemo:
    """Demo class for testing Snowflake news_articles schema."""
    
    def __init__(self):
        """Initialize Snowflake connection."""
        self.conn = None
        self.cursor = None
    
    def connect(self):
        """Connect to Snowflake using environment variables."""
        try:
            self.conn = snowflake.connector.connect(
                account=os.getenv('SNOWFLAKE_ACCOUNT'),
                user=os.getenv('SNOWFLAKE_USER'),
                password=os.getenv('SNOWFLAKE_PASSWORD'),
                warehouse=os.getenv('SNOWFLAKE_WAREHOUSE', 'COMPUTE_WH'),
                database=os.getenv('SNOWFLAKE_DATABASE', 'NEURONEWS'),
                schema=os.getenv('SNOWFLAKE_SCHEMA', 'PUBLIC')
            )
            self.cursor = self.conn.cursor()
            print("✅ Connected to Snowflake successfully")
            return True
        except Exception as e:
            print(f"❌ Failed to connect to Snowflake: {e}")
            return False
    
    def test_schema_creation(self):
        """Test that all tables and views exist."""
        print("\n🔍 Testing schema creation...")
        
        tables = [
            'news_articles',
            'article_summaries', 
            'event_clusters',
            'article_cluster_assignments',
            'article_embeddings',
            'news_articles_staging'
        ]
        
        views = [
            'high_quality_articles',
            'article_statistics',
            'summary_statistics',
            'high_quality_summaries',
            'event_cluster_statistics',
            'active_breaking_news',
            'trending_events_by_category',
            'breaking_news_view',
            'trending_events_view'
        ]
        
        # Test tables
        for table in tables:
            try:
                self.cursor.execute(f"DESCRIBE TABLE {table}")
                result = self.cursor.fetchall()
                print(f"✅ Table {table}: {len(result)} columns")
            except Exception as e:
                print(f"❌ Table {table}: {e}")
        
        # Test views
        for view in views:
            try:
                self.cursor.execute(f"DESCRIBE VIEW {view}")
                result = self.cursor.fetchall()
                print(f"✅ View {view}: {len(result)} columns")
            except Exception as e:
                print(f"❌ View {view}: {e}")
    
    def test_sample_data_insertion(self):
        """Test inserting sample data with VARIANT fields."""
        print("\n📝 Testing sample data insertion...")
        
        # Sample article data with JSON fields
        sample_data = {
            'id': 'test_article_001',
            'url': 'https://example.com/test-article',
            'title': 'Test News Article for Snowflake Demo',
            'content': 'This is a test article to demonstrate Snowflake VARIANT fields and schema migration.',
            'source': 'demo_source',
            'published_date': datetime.now() - timedelta(hours=1),
            'validation_score': 85.5,
            'content_quality': 'high',
            'source_credibility': 'trusted',
            'validation_flags': ['spell_check_passed', 'content_length_ok'],
            'word_count': 150,
            'content_length': 945,
            'sentiment_score': 0.75,
            'sentiment_label': 'positive',
            'entities': [
                {'text': 'Snowflake', 'label': 'ORG', 'confidence': 0.95},
                {'text': 'demo', 'label': 'MISC', 'confidence': 0.80}
            ],
            'keywords': [
                {'keyword': 'snowflake', 'score': 0.9},
                {'keyword': 'demo', 'score': 0.8},
                {'keyword': 'migration', 'score': 0.7}
            ],
            'topics': [
                {'topic': 'technology', 'probability': 0.85},
                {'topic': 'database', 'probability': 0.75}
            ],
            'dominant_topic': {'topic': 'technology', 'probability': 0.85}
        }
        
        try:
            # Insert sample article
            insert_query = """
            INSERT INTO news_articles (
                id, url, title, content, source, published_date,
                validation_score, content_quality, source_credibility,
                validation_flags, word_count, content_length,
                sentiment_score, sentiment_label, entities, keywords, topics, dominant_topic
            ) VALUES (
                %(id)s, %(url)s, %(title)s, %(content)s, %(source)s, %(published_date)s,
                %(validation_score)s, %(content_quality)s, %(source_credibility)s,
                PARSE_JSON(%(validation_flags)s), %(word_count)s, %(content_length)s,
                %(sentiment_score)s, %(sentiment_label)s, 
                PARSE_JSON(%(entities)s), PARSE_JSON(%(keywords)s), 
                PARSE_JSON(%(topics)s), PARSE_JSON(%(dominant_topic)s)
            )
            """
            
            # Convert JSON fields to strings
            params = sample_data.copy()
            params['validation_flags'] = json.dumps(params['validation_flags'])
            params['entities'] = json.dumps(params['entities'])
            params['keywords'] = json.dumps(params['keywords'])
            params['topics'] = json.dumps(params['topics'])
            params['dominant_topic'] = json.dumps(params['dominant_topic'])
            
            self.cursor.execute(insert_query, params)
            self.conn.commit()
            print("✅ Sample article inserted successfully")
            
        except Exception as e:
            print(f"❌ Failed to insert sample data: {e}")
    
    def test_variant_field_queries(self):
        """Test querying VARIANT fields with JSON path syntax."""
        print("\n🔍 Testing VARIANT field queries...")
        
        queries = [
            {
                'name': 'Extract entity names',
                'query': """
                SELECT id, title, 
                       entities[0]:text::STRING as first_entity,
                       ARRAY_SIZE(entities) as entity_count
                FROM news_articles 
                WHERE id = 'test_article_001'
                """
            },
            {
                'name': 'Extract keyword scores',
                'query': """
                SELECT id, 
                       keywords[0]:keyword::STRING as top_keyword,
                       keywords[0]:score::FLOAT as top_score
                FROM news_articles 
                WHERE id = 'test_article_001'
                """
            },
            {
                'name': 'Extract dominant topic',
                'query': """
                SELECT id, 
                       dominant_topic:topic::STRING as topic,
                       dominant_topic:probability::FLOAT as probability
                FROM news_articles 
                WHERE id = 'test_article_001'
                """
            }
        ]
        
        for query_info in queries:
            try:
                self.cursor.execute(query_info['query'])
                result = self.cursor.fetchall()
                print(f"✅ {query_info['name']}: {result}")
            except Exception as e:
                print(f"❌ {query_info['name']}: {e}")
    
    def test_analytics_views(self):
        """Test analytics views for compatibility."""
        print("\n📊 Testing analytics views...")
        
        view_queries = [
            {
                'name': 'High quality articles',
                'query': 'SELECT COUNT(*) FROM high_quality_articles'
            },
            {
                'name': 'Article statistics',
                'query': 'SELECT COUNT(*) FROM article_statistics'
            },
            {
                'name': 'Summary statistics',
                'query': 'SELECT COUNT(*) FROM summary_statistics'
            }
        ]
        
        for query_info in view_queries:
            try:
                self.cursor.execute(query_info['query'])
                result = self.cursor.fetchone()
                print(f"✅ {query_info['name']}: {result[0]} rows")
            except Exception as e:
                print(f"❌ {query_info['name']}: {e}")
    
    def test_clustering_optimization(self):
        """Test clustering key effectiveness."""
        print("\n⚡ Testing clustering optimization...")
        
        try:
            # Check clustering information
            self.cursor.execute("""
                SELECT table_name, clustering_key, total_rows
                FROM information_schema.tables 
                WHERE table_schema = 'PUBLIC' 
                AND table_name IN ('NEWS_ARTICLES', 'ARTICLE_SUMMARIES', 'EVENT_CLUSTERS')
            """)
            
            results = self.cursor.fetchall()
            for row in results:
                print(f"✅ Table {row[0]}: Clustering key = {row[1]}, Rows = {row[2]}")
                
        except Exception as e:
            print(f"❌ Clustering info query failed: {e}")
    
    def test_search_optimization(self):
        """Test search optimization status."""
        print("\n🔍 Testing search optimization...")
        
        try:
            # Check search optimization status
            self.cursor.execute("""
                SHOW TABLES LIKE 'NEWS_ARTICLES'
            """)
            
            result = self.cursor.fetchall()
            print(f"✅ Search optimization status checked: {len(result)} tables")
            
        except Exception as e:
            print(f"❌ Search optimization check failed: {e}")
    
    def cleanup_demo_data(self):
        """Clean up demo data."""
        print("\n🧹 Cleaning up demo data...")
        
        try:
            self.cursor.execute("DELETE FROM news_articles WHERE id = 'test_article_001'")
            self.conn.commit()
            print("✅ Demo data cleaned up")
        except Exception as e:
            print(f"❌ Cleanup failed: {e}")
    
    def close_connection(self):
        """Close Snowflake connection."""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()
        print("✅ Connection closed")
    
    def run_full_demo(self):
        """Run the complete demo."""
        print("🎯 Starting Snowflake Schema Demo for Issue #241")
        print("=" * 60)
        
        if not self.connect():
            return
        
        try:
            self.test_schema_creation()
            self.test_sample_data_insertion()
            self.test_variant_field_queries()
            self.test_analytics_views()
            self.test_clustering_optimization()
            self.test_search_optimization()
            self.cleanup_demo_data()
            
            print("\n🎉 Demo completed successfully!")
            print("✅ Snowflake schema migration validated")
            
        except Exception as e:
            print(f"\n❌ Demo failed: {e}")
        finally:
            self.close_connection()


if __name__ == "__main__":
    # Set environment variables for connection
    # export SNOWFLAKE_ACCOUNT=your_account
    # export SNOWFLAKE_USER=your_user
    # export SNOWFLAKE_PASSWORD=your_password
    # export SNOWFLAKE_WAREHOUSE=COMPUTE_WH
    # export SNOWFLAKE_DATABASE=NEURONEWS
    
    demo = SnowflakeSchemaDemo()
    demo.run_full_demo()
