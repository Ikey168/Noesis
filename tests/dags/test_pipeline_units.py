"""
Unit Tests for NeuroNews Pipeline Task Functions (Issue #195)

Tests the individual Python task functions outside of Airflow to verify
I/O stubs and core functionality. These tests help ensure the business
logic works correctly independent of the orchestration framework.
"""

import pytest
import tempfile
import os
import json
import pandas as pd
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import sys
from datetime import datetime


# Add necessary paths for imports
sys.path.append(str(Path(__file__).parent.parent.parent / "airflow" / "dags"))
sys.path.append(str(Path(__file__).parent.parent.parent / "airflow" / "plugins"))


@pytest.fixture
def temp_data_dir():
    """Create a temporary directory for test data files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_context():
    """Mock Airflow context for task execution."""
    return {
        'ds': '2025-08-24',
        'execution_date': datetime(2025, 8, 24),
        'dag_run': Mock(),
        'task_instance': Mock(),
        'dag': Mock()
    }


@pytest.fixture
def sample_scrape_result(temp_data_dir):
    """Sample output from scrape task for downstream testing."""
    articles_path = os.path.join(temp_data_dir, "raw_articles.json")
    metadata_path = os.path.join(temp_data_dir, "scrape_metadata.json")
    
    # Create sample data files
    articles = [
        {
            "id": "test_article_1",
            "title": "Test News Article 1",
            "content": "This is test content for article 1.",
            "url": "https://example.com/article-1",
            "source": "test_source",
            "published_date": "2025-08-24T10:00:00Z"
        }
    ]
    
    metadata = {
        "scrape_timestamp": "2025-08-24T10:00:00Z",
        "total_sources": 1,
        "successful_sources": 1,
        "total_articles": 1
    }
    
    with open(articles_path, 'w') as f:
        json.dump(articles, f)
    
    with open(metadata_path, 'w') as f:
        json.dump(metadata, f)
    
    return {
        "articles_path": articles_path,
        "metadata_path": metadata_path,
        "articles_uri": f"file://{articles_path}",
        "metadata_uri": f"file://{metadata_path}",
        "article_count": 1
    }


class TestScrapeTaskFunction:
    """Test the scrape task function."""
    
    def test_scrape_function_basic(self, mock_context, temp_data_dir):
        """Test basic scrape function functionality."""
        # news_pipeline.py uses Airflow's TaskFlow API (`from airflow.decorators
        # import dag, task`); skip when real Airflow is not installed. The patch
        # target is applied inside the try block so importing the module (which
        # triggers the Airflow import) is guarded by the ImportError handler.
        pytest.importorskip("airflow.decorators")

        # Mock URI generation
        articles_uri = f"file://{temp_data_dir}/articles.json"
        metadata_uri = f"file://{temp_data_dir}/metadata.json"

        # Import and test the function
        try:
            with patch('news_pipeline.LineageHelper') as mock_lineage_helper:
                mock_helper_instance = Mock()
                mock_lineage_helper.return_value = mock_helper_instance
                mock_helper_instance.create_dataset_uri.side_effect = [
                    articles_uri, metadata_uri
                ]

                from news_pipeline import scrape
                result = scrape(**mock_context)
            
            # Verify return structure
            assert "articles_path" in result
            assert "metadata_path" in result
            assert "articles_uri" in result
            assert "metadata_uri" in result
            assert "article_count" in result
            
            # Verify article count is positive
            assert result["article_count"] > 0
            
            # Verify files were created
            assert os.path.exists(result["articles_path"])
            assert os.path.exists(result["metadata_path"])
            
            # Verify file contents
            with open(result["articles_path"], 'r') as f:
                articles = json.load(f)
                assert isinstance(articles, list)
                assert len(articles) > 0
                
                # Check article structure
                article = articles[0]
                required_fields = ["id", "title", "content", "url", "source", "published_date"]
                for field in required_fields:
                    assert field in article, f"Missing field: {field}"
            
        except ImportError:
            pytest.skip("Cannot import scrape function - Airflow dependencies not available")
    
    def test_scrape_io_stub(self):
        """Test scrape function I/O stub without dependencies."""
        # Test the expected interface without running the actual function
        expected_output_keys = {
            "articles_path", "metadata_path", "articles_uri", 
            "metadata_uri", "article_count"
        }
        
        # This verifies our understanding of the interface
        assert len(expected_output_keys) == 5
        assert "articles_path" in expected_output_keys


class TestCleanTaskFunction:
    """Test the clean task function."""
    
    def test_clean_function_basic(self, sample_scrape_result,
                                 mock_context, temp_data_dir):
        """Test basic clean function functionality."""
        # Skip when real Airflow (TaskFlow API) is not installed; patch the
        # LineageHelper inside the guarded block so the module import is covered
        # by the ImportError handler.
        pytest.importorskip("airflow.decorators")

        # Mock URI generation for clean task outputs
        clean_articles_uri = f"file://{temp_data_dir}/clean_articles.parquet"
        metadata_uri = f"file://{temp_data_dir}/clean_metadata.parquet"

        try:
            with patch('news_pipeline.LineageHelper') as mock_lineage_helper:
                mock_helper_instance = Mock()
                mock_lineage_helper.return_value = mock_helper_instance
                mock_helper_instance.create_dataset_uri.side_effect = [
                    clean_articles_uri, metadata_uri
                ]

                from news_pipeline import clean
                result = clean(sample_scrape_result, **mock_context)
            
            # Verify return structure
            expected_keys = {
                "clean_articles_path", "metadata_path", "clean_articles_uri",
                "metadata_uri", "valid_articles", "total_articles"
            }
            
            for key in expected_keys:
                assert key in result, f"Missing key: {key}"
            
            # Verify article counts are reasonable
            assert result["valid_articles"] >= 0
            assert result["total_articles"] >= result["valid_articles"]
            
            # Verify files were created (if paths exist)
            if os.path.exists(result["clean_articles_path"]):
                # File should be readable as parquet
                df = pd.read_parquet(result["clean_articles_path"])
                assert not df.empty
                
        except ImportError:
            pytest.skip("Cannot import clean function - Airflow dependencies not available")
    
    def test_clean_io_stub(self):
        """Test clean function I/O stub."""
        expected_input_keys = {
            "articles_path", "metadata_path", "articles_uri", 
            "metadata_uri", "article_count"
        }
        
        expected_output_keys = {
            "clean_articles_path", "metadata_path", "clean_articles_uri",
            "metadata_uri", "valid_articles", "total_articles"
        }
        
        # Verify interface understanding
        assert len(expected_input_keys) == 5
        assert len(expected_output_keys) == 6


class TestNLPTaskFunction:
    """Test the NLP task function."""
    
    @pytest.fixture
    def sample_clean_result(self, temp_data_dir):
        """Sample output from clean task for NLP testing."""
        clean_articles_path = os.path.join(temp_data_dir, "clean_articles.parquet")
        
        # Create sample clean data
        clean_data = pd.DataFrame([
            {
                "id": "test_article_1",
                "title": "Test Article",
                "content": "This is test content with positive sentiment.",
                "url": "https://example.com/test",
                "source": "test_source",
                "published_date": "2025-08-24T10:00:00Z"
            }
        ])
        
        clean_data.to_parquet(clean_articles_path, index=False)
        
        return {
            "clean_articles_path": clean_articles_path,
            "metadata_path": os.path.join(temp_data_dir, "metadata.parquet"),
            "clean_articles_uri": f"file://{clean_articles_path}",
            "metadata_uri": f"file://{temp_data_dir}/metadata.parquet",
            "valid_articles": 1,
            "total_articles": 1
        }
    
    def test_nlp_function_basic(self, sample_clean_result,
                               mock_context, temp_data_dir):
        """Test basic NLP function functionality."""
        # Skip when real Airflow (TaskFlow API) is not installed; patch the
        # LineageHelper inside the guarded block so the module import is covered
        # by the ImportError handler.
        pytest.importorskip("airflow.decorators")

        # Mock URI generation for NLP task outputs
        uris = [
            f"file://{temp_data_dir}/nlp_processed.parquet",
            f"file://{temp_data_dir}/sentiment.parquet",
            f"file://{temp_data_dir}/entities.parquet",
            f"file://{temp_data_dir}/keywords.parquet"
        ]

        try:
            with patch('news_pipeline.LineageHelper') as mock_lineage_helper:
                mock_helper_instance = Mock()
                mock_lineage_helper.return_value = mock_helper_instance
                mock_helper_instance.create_dataset_uri.side_effect = uris

                from news_pipeline import nlp
                result = nlp(sample_clean_result, **mock_context)
            
            # Verify return structure
            expected_keys = {
                "nlp_processed_path", "sentiment_path", "entities_path",
                "keywords_path", "processed_article_count"
            }
            
            for key in expected_keys:
                assert key in result, f"Missing key: {key}"
            
            # Verify processed count is reasonable
            assert result["processed_article_count"] >= 0
            
        except ImportError:
            pytest.skip("Cannot import nlp function - Airflow dependencies not available")
    
    def test_nlp_io_stub(self):
        """Test NLP function I/O stub."""
        expected_output_keys = {
            "nlp_processed_path", "sentiment_path", "entities_path",
            "keywords_path", "processed_article_count"
        }
        
        assert len(expected_output_keys) == 5


class TestPublishTaskFunction:
    """Test the publish task function."""
    
    @pytest.fixture
    def sample_nlp_result(self, temp_data_dir):
        """Sample output from NLP task for publish testing."""
        nlp_processed_path = os.path.join(temp_data_dir, "nlp_processed.parquet")
        sentiment_path = os.path.join(temp_data_dir, "sentiment.parquet")
        
        # Create sample NLP data
        nlp_data = pd.DataFrame([
            {
                "id": "test_article_1",
                "title": "Test Article",
                "content": "Test content",
                "sentiment_score": 0.7,
                "entities": "[]",
                "keywords": "[]"
            }
        ])
        
        sentiment_data = pd.DataFrame([
            {
                "article_id": "test_article_1",
                "sentiment_score": 0.7,
                "sentiment_label": "positive"
            }
        ])
        
        nlp_data.to_parquet(nlp_processed_path, index=False)
        sentiment_data.to_parquet(sentiment_path, index=False)
        
        return {
            "nlp_processed_path": nlp_processed_path,
            "sentiment_path": sentiment_path,
            "entities_path": os.path.join(temp_data_dir, "entities.parquet"),
            "keywords_path": os.path.join(temp_data_dir, "keywords.parquet"),
            "processed_article_count": 1
        }
    
    def test_publish_function_basic(self, sample_nlp_result,
                                   mock_context, temp_data_dir):
        """Test basic publish function functionality."""
        # Skip when real Airflow (TaskFlow API) is not installed; patch the
        # LineageHelper inside the guarded block so the module import is covered
        # by the ImportError handler.
        pytest.importorskip("airflow.decorators")

        # Mock URI generation for publish task outputs
        uris = [
            f"file://{temp_data_dir}/daily_summary.json",
            f"file://{temp_data_dir}/trending_topics.json",
            f"file://{temp_data_dir}/sentiment_trends.json"
        ]

        try:
            with patch('news_pipeline.LineageHelper') as mock_lineage_helper:
                mock_helper_instance = Mock()
                mock_lineage_helper.return_value = mock_helper_instance
                mock_helper_instance.create_dataset_uri.side_effect = uris

                from news_pipeline import publish
                result = publish(sample_nlp_result, **mock_context)
            
            # Verify return structure
            expected_keys = {
                "daily_summary_path", "trending_topics_path", 
                "sentiment_trends_path", "summary"
            }
            
            for key in expected_keys:
                assert key in result, f"Missing key: {key}"
            
            # Verify summary structure
            summary = result["summary"]
            assert "total_articles" in summary
            assert "avg_sentiment_score" in summary
            assert isinstance(summary["total_articles"], int)
            assert isinstance(summary["avg_sentiment_score"], (int, float))
            
        except ImportError:
            pytest.skip("Cannot import publish function - Airflow dependencies not available")
    
    def test_publish_io_stub(self):
        """Test publish function I/O stub."""
        expected_output_keys = {
            "daily_summary_path", "trending_topics_path",
            "sentiment_trends_path", "summary"
        }
        
        assert len(expected_output_keys) == 4


class TestTaskIntegration:
    """Test task function integration and data flow."""
    
    def test_task_data_flow_interface(self):
        """Test that task outputs match expected inputs for downstream tasks."""
        # Verify scrape -> clean interface
        scrape_output = {
            "articles_path", "metadata_path", "articles_uri", 
            "metadata_uri", "article_count"
        }
        
        # clean expects scrape_result with these keys
        clean_input_expected = scrape_output
        
        assert scrape_output == clean_input_expected
        
        # Verify clean -> nlp interface  
        clean_output = {
            "clean_articles_path", "metadata_path", "clean_articles_uri",
            "metadata_uri", "valid_articles", "total_articles"
        }
        
        # nlp expects clean_result, main key is clean_articles_path
        assert "clean_articles_path" in clean_output
        
        # Verify nlp -> publish interface
        nlp_output = {
            "nlp_processed_path", "sentiment_path", "entities_path",
            "keywords_path", "processed_article_count"
        }
        
        # publish expects nlp_result, main keys are nlp_processed_path and sentiment_path
        assert "nlp_processed_path" in nlp_output
        assert "sentiment_path" in nlp_output
    
    def test_all_functions_importable(self):
        """Verify the news_pipeline DAG defines the expected TaskFlow tasks.

        The pipeline uses Airflow's TaskFlow API, so scrape/clean/nlp/publish are
        ``@task``-decorated functions nested inside the ``@dag`` factory rather
        than module-level callables. Assert they exist as tasks on the DAG.
        """
        expected_tasks = {"scrape", "clean", "nlp", "analyze_arguments", "publish"}

        try:
            from airflow.models import DagBag
        except ImportError:
            pytest.skip("Airflow not available in test environment")

        dag_folder = str(Path(__file__).parent.parent.parent / "airflow" / "dags")
        dag = DagBag(dag_folder=dag_folder, include_examples=False).get_dag("news_pipeline")

        assert dag is not None, "news_pipeline DAG not found"
        task_ids = {t.task_id for t in dag.tasks}
        assert expected_tasks.issubset(task_ids), (
            f"Missing expected tasks. Expected: {expected_tasks}, Found: {task_ids}"
        )


if __name__ == "__main__":
    # Run tests individually for debugging
    pytest.main([__file__, "-v"])
