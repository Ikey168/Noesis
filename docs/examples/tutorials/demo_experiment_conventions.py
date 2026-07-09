#!/usr/bin/env python3
"""
Demonstration of MLflow Experiment Structure & Naming Conventions
Issue #220: Show usage of standardized experiments, tags, and naming

This script demonstrates the proper usage of the MLflow tracking conventions
established in Issue #220, including standard experiments, required tags,
and naming conventions.
"""

import os
import sys
from typing import Dict, Any
import mlflow
import numpy as np
from datetime import datetime

# Add the project root to the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.mlops.tracking import (
    mlrun, 
    generate_run_name, 
    STANDARD_EXPERIMENTS,
    validate_experiment_name,
    validate_required_tags
)


def setup_demo_environment():
    """Setup environment variables for the demo."""
    print("🔧 Setting up demo environment variables...")
    
    # Set required environment variables
    os.environ["NEURONEWS_ENV"] = "dev"
    os.environ["NEURONEWS_PIPELINE"] = "demo_pipeline"
    os.environ["NEURONEWS_DATA_VERSION"] = "v1.2.3"
    
    # Set MLflow tracking to local file store
    os.environ["MLFLOW_TRACKING_URI"] = "file:./mlruns"
    
    print("✅ Environment variables set:")
    print(f"   NEURONEWS_ENV: {os.environ['NEURONEWS_ENV']}")
    print(f"   NEURONEWS_PIPELINE: {os.environ['NEURONEWS_PIPELINE']}")
    print(f"   NEURONEWS_DATA_VERSION: {os.environ['NEURONEWS_DATA_VERSION']}")
    print(f"   MLFLOW_TRACKING_URI: {os.environ['MLFLOW_TRACKING_URI']}")


def demonstrate_experiment_validation():
    """Demonstrate experiment name validation."""
    print("\n" + "="*60)
    print("🧪 EXPERIMENT NAME VALIDATION")
    print("="*60)
    
    print("\n📋 Standard experiments:")
    for exp_name, description in STANDARD_EXPERIMENTS.items():
        print(f"   ✅ {exp_name}: {description}")
    
    print("\n🔍 Testing experiment validation:")
    
    # Test valid experiment names
    for exp_name in STANDARD_EXPERIMENTS.keys():
        is_valid = validate_experiment_name(exp_name)
        print(f"   ✅ '{exp_name}': {is_valid}")
    
    # Test non-standard experiment name (should warn but allow)
    print(f"   ⚠️  'custom_experiment': {validate_experiment_name('custom_experiment')}")


def demonstrate_run_naming():
    """Demonstrate standardized run naming."""
    print("\n" + "="*60)
    print("🏷️  RUN NAMING CONVENTIONS")
    print("="*60)
    
    print("\n📝 Generated run names:")
    
    # Standard component names
    components = [
        "embeddings_indexer",
        "rag_answerer", 
        "sentiment_analyzer",
        "clustering_engine"
    ]
    
    for component in components:
        # Without descriptor
        run_name1 = generate_run_name(component)
        print(f"   {component}: {run_name1}")
        
        # With descriptor
        run_name2 = generate_run_name(component, "baseline")
        print(f"   {component} (baseline): {run_name2}")


def demonstrate_tag_validation():
    """Demonstrate required tag validation."""
    print("\n" + "="*60)
    print("🏷️  TAG VALIDATION")
    print("="*60)
    
    print("\n✅ Valid tags example:")
    valid_tags = {
        "git.sha": "a1b2c3d4e5f6789012345678901234567890abcd",
        "env": "dev",
        "pipeline": "embeddings_indexer",
        "data_version": "v1.2.3",
        "notes": "Baseline run for comparison",
        "model_type": "transformer"
    }
    
    for key, value in valid_tags.items():
        print(f"   {key}: {value}")
    
    missing = validate_required_tags(valid_tags)
    print(f"\n   Missing required tags: {missing if missing else 'None'}")
    
    print("\n❌ Invalid tags example:")
    invalid_tags = {
        "git.sha": "a1b2c3d4e5f6789012345678901234567890abcd",
        "env": "dev",
        # Missing: pipeline, data_version
        "notes": "Missing required tags"
    }
    
    for key, value in invalid_tags.items():
        print(f"   {key}: {value}")
    
    missing = validate_required_tags(invalid_tags)
    print(f"\n   Missing required tags: {missing}")


def run_indexing_demo():
    """Demonstrate neuro_news_indexing experiment."""
    print("\n" + "="*60)
    print("📊 INDEXING EXPERIMENT DEMO")
    print("="*60)
    
    # Generate proper run name
    run_name = generate_run_name("embeddings_indexer", "sentence_transformers_test")
    
    try:
        with mlrun(
            name=run_name,
            experiment="neuro_news_indexing",
            tags={
                "data_version": "v1.2.3",
                "notes": "Testing sentence-transformers with improved chunking strategy",
                "model_type": "transformer",
                "task_type": "embedding",
                "dataset_type": "production"
            }
        ) as run:
            print(f"🚀 Started run: {run_name}")
            print(f"📊 Experiment: neuro_news_indexing")
            print(f"🆔 Run ID: {run.info.run_id}")
            
            # Log parameters (following naming conventions)
            mlflow.log_param("model_name", "sentence-transformers/all-MiniLM-L6-v2")
            mlflow.log_param("chunk_size", 512)
            mlflow.log_param("overlap", 50)
            mlflow.log_param("batch_size", 32)
            
            # Simulate some metrics
            embedding_time = np.random.uniform(40, 60)
            accuracy = np.random.uniform(0.85, 0.95)
            memory_usage = np.random.uniform(2000, 3000)
            
            # Log metrics (following naming conventions)
            mlflow.log_metric("embedding_time_ms", embedding_time)
            mlflow.log_metric("accuracy", accuracy)
            mlflow.log_metric("memory_mb", memory_usage)
            mlflow.log_metric("embedding_dimension", 384)
            
            print(f"📈 Logged metrics:")
            print(f"   embedding_time_ms: {embedding_time:.2f}")
            print(f"   accuracy: {accuracy:.4f}")
            print(f"   memory_mb: {memory_usage:.2f}")
            
            print("✅ Indexing experiment completed successfully!")
            
    except Exception as e:
        print(f"❌ Indexing experiment failed: {e}")


def run_ask_demo():
    """Demonstrate neuro_news_ask experiment.""" 
    print("\n" + "="*60)
    print("❓ ASK PIPELINE EXPERIMENT DEMO")
    print("="*60)
    
    # Generate proper run name
    run_name = generate_run_name("rag_answerer", "gpt4_fusion_test")
    
    try:
        with mlrun(
            name=run_name,
            experiment="neuro_news_ask",
            tags={
                "data_version": "v1.2.3",
                "notes": "Testing GPT-4 with reciprocal rank fusion",
                "model_type": "llm",
                "task_type": "rag",
                "dataset_type": "production"
            }
        ) as run:
            print(f"🚀 Started run: {run_name}")
            print(f"📊 Experiment: neuro_news_ask")
            print(f"🆔 Run ID: {run.info.run_id}")
            
            # Log parameters
            mlflow.log_param("llm_model", "gpt-4-turbo")
            mlflow.log_param("retrieval_k", 10)
            mlflow.log_param("temperature", 0.1)
            mlflow.log_param("max_tokens", 1000)
            mlflow.log_param("fusion_method", "reciprocal_rank")
            mlflow.log_param("rerank_enabled", True)
            
            # Simulate some metrics
            retrieval_time = np.random.uniform(100, 200)
            answer_time = np.random.uniform(1000, 2000)
            relevance_score = np.random.uniform(0.80, 0.95)
            citation_count = np.random.randint(3, 8)
            
            # Log metrics
            mlflow.log_metric("retrieval_ms", retrieval_time)
            mlflow.log_metric("answer_ms", answer_time)
            mlflow.log_metric("relevance_score", relevance_score)
            mlflow.log_metric("num_citations", citation_count)
            mlflow.log_metric("total_latency_ms", retrieval_time + answer_time)
            
            print(f"📈 Logged metrics:")
            print(f"   retrieval_ms: {retrieval_time:.2f}")
            print(f"   answer_ms: {answer_time:.2f}")
            print(f"   relevance_score: {relevance_score:.4f}")
            print(f"   num_citations: {citation_count}")
            
            print("✅ Ask pipeline experiment completed successfully!")
            
    except Exception as e:
        print(f"❌ Ask pipeline experiment failed: {e}")


def run_research_demo():
    """Demonstrate research_prototypes experiment."""
    print("\n" + "="*60)
    print("🧪 RESEARCH PROTOTYPE DEMO")
    print("="*60)
    
    # Generate proper run name
    run_name = generate_run_name("clustering_engine", "dbscan_experiment")
    
    try:
        with mlrun(
            name=run_name,
            experiment="research_prototypes",
            tags={
                "data_version": "v1.2.3",
                "notes": "Experimenting with DBSCAN for news article clustering",
                "model_type": "clustering",
                "task_type": "unsupervised",
                "dataset_type": "synthetic"
            }
        ) as run:
            print(f"🚀 Started run: {run_name}")
            print(f"📊 Experiment: research_prototypes")
            print(f"🆔 Run ID: {run.info.run_id}")
            
            # Log parameters
            mlflow.log_param("algorithm", "dbscan")
            mlflow.log_param("eps", 0.3)
            mlflow.log_param("min_samples", 5)
            mlflow.log_param("metric", "cosine")
            mlflow.log_param("n_documents", 1000)
            
            # Simulate some metrics
            silhouette_score = np.random.uniform(0.4, 0.7)
            n_clusters = np.random.randint(15, 30)
            n_noise = np.random.randint(50, 100)
            processing_time = np.random.uniform(5000, 8000)
            
            # Log metrics
            mlflow.log_metric("silhouette_score", silhouette_score)
            mlflow.log_metric("n_clusters", n_clusters)
            mlflow.log_metric("n_noise_points", n_noise)
            mlflow.log_metric("processing_ms", processing_time)
            mlflow.log_metric("cluster_ratio", n_clusters / 1000)
            
            print(f"📈 Logged metrics:")
            print(f"   silhouette_score: {silhouette_score:.4f}")
            print(f"   n_clusters: {n_clusters}")
            print(f"   n_noise_points: {n_noise}")
            print(f"   processing_ms: {processing_time:.2f}")
            
            print("✅ Research prototype experiment completed successfully!")
            
    except Exception as e:
        print(f"❌ Research prototype experiment failed: {e}")


def demonstrate_error_handling():
    """Demonstrate error handling for invalid configurations."""
    print("\n" + "="*60)
    print("🚨 ERROR HANDLING DEMO")
    print("="*60)
    
    print("\n1️⃣ Testing missing required tags:")
    try:
        # Remove required environment variable
        original_pipeline = os.environ.pop("NEURONEWS_PIPELINE", None)
        
        with mlrun(
            name="test_run_missing_tags",
            experiment="neuro_news_indexing",
            tags={"notes": "This should fail due to missing pipeline tag"}
        ):
            print("This should not print - run should fail")
    except ValueError as e:
        print(f"   ✅ Correctly caught error: {e}")
    finally:
        # Restore environment variable
        if original_pipeline:
            os.environ["NEURONEWS_PIPELINE"] = original_pipeline
    
    print("\n2️⃣ Testing invalid environment:")
    try:
        # Set invalid environment
        original_env = os.environ.get("NEURONEWS_ENV")
        os.environ["NEURONEWS_ENV"] = "invalid_env"
        
        with mlrun(
            name="test_run_invalid_env",
            experiment="neuro_news_indexing",
            tags={
                "data_version": "v1.2.3",
                "notes": "This should fail due to invalid environment"
            }
        ):
            print("This should not print - run should fail")
    except ValueError as e:
        print(f"   ✅ Correctly caught error: {e}")
    finally:
        # Restore environment variable
        if original_env:
            os.environ["NEURONEWS_ENV"] = original_env
    
    print("\n3️⃣ Testing validation bypass:")
    try:
        with mlrun(
            name="test_run_no_validation",
            experiment="neuro_news_indexing",
            tags={"notes": "This should work with validation disabled"},
            validate_tags=False
        ) as run:
            print(f"   ✅ Run succeeded with validation disabled: {run.info.run_id}")
    except Exception as e:
        print(f"   ❌ Unexpected error: {e}")


def main():
    """Run the complete demonstration."""
    print("🎯 MLflow Experiment Structure & Naming Conventions Demo")
    print("=" * 80)
    print("Issue #220: Demonstrate standardized experiment organization,")
    print("naming conventions, and tagging strategies for NeuroNews MLflow")
    
    try:
        # Setup
        setup_demo_environment()
        
        # Demonstrations
        demonstrate_experiment_validation()
        demonstrate_run_naming()
        demonstrate_tag_validation()
        
        # Run actual MLflow experiments
        run_indexing_demo()
        run_ask_demo()
        run_research_demo()
        
        # Error handling
        demonstrate_error_handling()
        
        # Summary
        print("\n" + "="*80)
        print("🎉 DEMO COMPLETED SUCCESSFULLY!")
        print("="*80)
        
        print(f"\n📊 View results in MLflow UI:")
        print(f"   mlflow ui --backend-store-uri {os.environ.get('MLFLOW_TRACKING_URI', 'file:./mlruns')}")
        print(f"   Then navigate to: http://localhost:5000")
        
        print(f"\n📁 Experiments created:")
        for exp_name in STANDARD_EXPERIMENTS.keys():
            print(f"   • {exp_name}")
        
        print(f"\n🏷️  All runs include standardized tags:")
        print(f"   • Required: git.sha, env, pipeline, data_version")
        print(f"   • Optional: notes, model_type, task_type, dataset_type")
        
        print(f"\n📝 Documentation: docs/subsystems/mlops/experiments.md")
        
    except Exception as e:
        print(f"\n❌ Demo failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
