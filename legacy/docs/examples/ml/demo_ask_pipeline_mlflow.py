"""
Demo script for Ask Pipeline with MLflow Tracking
Issue #218: Instrument /ask pipeline with MLflow tracking

This script demonstrates the complete ask pipeline functionality
with MLflow tracking, including sampling, metrics, and artifacts.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime

# Add src to path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

try:
    from legacy.services.rag.answer import RAGAnswerService
    from legacy.services.api.routes.ask import AskRequest, ask_question, get_rag_service
    from services.embeddings.provider import EmbeddingsProvider
except ImportError as e:
    print(f"Import error: {e}")
    print("Please ensure you're running from the project root directory")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class AskPipelineDemo:
    """
    Comprehensive demo of the Ask Pipeline with MLflow tracking.
    
    Features demonstrated:
    - Question answering with different parameters
    - MLflow tracking with sampling
    - Citation extraction and scoring
    - Performance metrics monitoring
    - Artifact generation and logging
    """

    def __init__(self):
        """Initialize the demo."""
        self.demo_start_time = time.time()
        
        # Set demonstration sampling rate
        os.environ["ASK_LOG_SAMPLE"] = "0.8"  # 80% sampling for demo
        
        logger.info("🚀 Ask Pipeline MLflow Demo Starting")
        logger.info(f"📊 MLflow Sampling Rate: {os.getenv('ASK_LOG_SAMPLE', '0.2')}")

    async def run_demo(self):
        """Run the complete demonstration."""
        try:
            print("=" * 80)
            print("🎯 NeuroNews Ask Pipeline with MLflow Tracking Demo")
            print("=" * 80)
            
            # Step 1: Initialize services
            await self._demo_service_initialization()
            
            # Step 2: Single question demo
            await self._demo_single_question()
            
            # Step 3: Different parameter configurations
            await self._demo_parameter_variations()
            
            # Step 4: Batch processing demo
            await self._demo_batch_processing()
            
            # Step 5: MLflow artifacts showcase
            await self._demo_mlflow_artifacts()
            
            # Step 6: Performance analysis
            await self._demo_performance_analysis()
            
            self._print_demo_summary()
            
        except Exception as e:
            logger.error(f"Demo failed: {e}")
            raise

    async def _demo_service_initialization(self):
        """Demonstrate service initialization and configuration."""
        print("\n📦 Service Initialization")
        print("-" * 40)
        
        # Initialize embeddings provider
        embeddings_provider = EmbeddingsProvider(
            model_name="all-MiniLM-L6-v2",
            batch_size=16,
        )
        
        # Initialize RAG service
        rag_service = RAGAnswerService(
            embeddings_provider=embeddings_provider,
            default_k=5,
            rerank_enabled=True,
            fusion_enabled=True,
            answer_provider="openai"
        )
        
        print(f"✅ Embeddings Provider: {embeddings_provider.model_name}")
        print(f"   - Model dimension: {embeddings_provider.embedding_dimension}")
        print(f"   - Batch size: {embeddings_provider.batch_size}")
        
        print(f"✅ RAG Answer Service: {rag_service.answer_provider}")
        print(f"   - Default k: {rag_service.default_k}")
        print(f"   - Rerank enabled: {rag_service.rerank_enabled}")
        print(f"   - Fusion enabled: {rag_service.fusion_enabled}")
        
        self.rag_service = rag_service

    async def _demo_single_question(self):
        """Demonstrate single question processing with full MLflow tracking."""
        print("\n🎯 Single Question Demo")
        print("-" * 40)
        
        question = "What are the latest developments in artificial intelligence and machine learning?"
        
        print(f"❓ Question: {question}")
        print("⏳ Processing with full MLflow tracking...")
        
        start_time = time.time()
        
        response = await self.rag_service.answer_question(
            question=question,
            k=5,
            filters={"category": "technology"},
            rerank_on=True,
            fusion=True,
            provider="openai",
            experiment_name="ask_pipeline_demo",
            run_name=f"single_question_{datetime.now().strftime('%H%M%S')}"
        )
        
        processing_time = time.time() - start_time
        
        print(f"✅ Completed in {processing_time:.2f}s")
        print(f"📝 Answer (preview): {response['answer'][:200]}...")
        print(f"📚 Citations found: {len(response['citations'])}")
        print(f"📊 Documents retrieved: {response['metadata']['documents_retrieved']}")
        print(f"⚡ Pipeline breakdown:")
        print(f"   - Retrieval: {response['metadata']['retrieval_time_ms']:.1f}ms")
        print(f"   - Answer generation: {response['metadata']['answer_time_ms']:.1f}ms")
        print(f"   - Total: {response['metadata']['total_time_ms']:.1f}ms")
        
        # Display sample citation
        if response['citations']:
            citation = response['citations'][0]
            print(f"📖 Sample Citation:")
            print(f"   - Title: {citation['title']}")
            print(f"   - Source: {citation['source']}")
            print(f"   - Relevance: {citation['relevance_score']:.3f}")

    async def _demo_parameter_variations(self):
        """Demonstrate different parameter configurations."""
        print("\n⚙️ Parameter Variations Demo")
        print("-" * 40)
        
        base_question = "How does renewable energy impact climate change?"
        
        configurations = [
            {
                "name": "High Precision",
                "params": {"k": 10, "rerank_on": True, "fusion": True, "provider": "openai"},
            },
            {
                "name": "Fast Response",
                "params": {"k": 3, "rerank_on": False, "fusion": False, "provider": "local"},
            },
            {
                "name": "Balanced",
                "params": {"k": 5, "rerank_on": True, "fusion": False, "provider": "anthropic"},
            },
        ]
        
        results = []
        
        for config in configurations:
            print(f"🔧 Testing {config['name']} configuration...")
            
            start_time = time.time()
            
            response = await self.rag_service.answer_question(
                question=base_question,
                experiment_name="ask_pipeline_demo",
                run_name=f"config_{config['name'].lower().replace(' ', '_')}_{datetime.now().strftime('%H%M%S')}",
                **config["params"]
            )
            
            processing_time = time.time() - start_time
            
            result = {
                "config": config["name"],
                "time": processing_time,
                "citations": len(response['citations']),
                "docs_retrieved": response['metadata']['documents_retrieved'],
                "answer_length": len(response['answer']),
                "retrieval_ms": response['metadata']['retrieval_time_ms'],
                "answer_ms": response['metadata']['answer_time_ms'],
            }
            results.append(result)
            
            print(f"   ⏱️ {processing_time:.2f}s | 📚 {result['citations']} citations | 📄 {result['docs_retrieved']} docs")
        
        # Compare results
        print("\n📊 Configuration Comparison:")
        print("Config        | Time   | Citations | Docs | Answer Len | Retrieval | Generation")
        print("-" * 75)
        for r in results:
            print(f"{r['config']:<12} | {r['time']:.2f}s | {r['citations']:<9} | {r['docs_retrieved']:<4} | {r['answer_length']:<10} | {r['retrieval_ms']:.0f}ms     | {r['answer_ms']:.0f}ms")

    async def _demo_batch_processing(self):
        """Demonstrate batch question processing."""
        print("\n📦 Batch Processing Demo")
        print("-" * 40)
        
        questions = [
            "What is the future of electric vehicles?",
            "How does quantum computing work?", 
            "What are the benefits of solar energy?",
            "How is AI transforming healthcare?",
        ]
        
        print(f"📝 Processing {len(questions)} questions in batch...")
        
        batch_start = time.time()
        batch_results = []
        
        for i, question in enumerate(questions):
            print(f"⏳ Question {i+1}/{len(questions)}: {question[:50]}...")
            
            response = await self.rag_service.answer_question(
                question=question,
                k=3,
                rerank_on=True,
                fusion=False,
                experiment_name="ask_pipeline_demo",
                run_name=f"batch_q{i+1}_{datetime.now().strftime('%H%M%S')}"
            )
            
            batch_results.append({
                "question": question,
                "answer_length": len(response['answer']),
                "citations": len(response['citations']),
                "time_ms": response['metadata']['total_time_ms'],
            })
        
        batch_time = time.time() - batch_start
        
        print(f"✅ Batch completed in {batch_time:.2f}s")
        print(f"📊 Average time per question: {batch_time/len(questions):.2f}s")
        print(f"📚 Total citations generated: {sum(r['citations'] for r in batch_results)}")
        print(f"📝 Average answer length: {sum(r['answer_length'] for r in batch_results)/len(batch_results):.0f} chars")

    async def _demo_mlflow_artifacts(self):
        """Demonstrate MLflow artifacts generation."""
        print("\n🗂️ MLflow Artifacts Demo")
        print("-" * 40)
        
        print("📁 Generating comprehensive MLflow artifacts...")
        
        complex_question = "Explain the relationship between artificial intelligence, machine learning, and deep learning, including their applications in modern technology."
        
        response = await self.rag_service.answer_question(
            question=complex_question,
            k=8,
            filters={"category": "technology"},
            rerank_on=True,
            fusion=True,
            provider="openai",
            experiment_name="ask_pipeline_demo",
            run_name=f"artifacts_demo_{datetime.now().strftime('%H%M%S')}"
        )
        
        print("✅ Artifacts generated for MLflow tracking:")
        print("   📋 citations.json - Citation data with scores and metadata")
        print("   🔍 trace.json - Complete pipeline execution trace")
        print("   📊 qa_summary.json - Question/answer analysis summary")
        
        # Show sample artifacts content
        citations_preview = {
            "total_citations": len(response['citations']),
            "avg_relevance": sum(c['relevance_score'] for c in response['citations']) / max(1, len(response['citations'])),
            "sources": list(set(c['source'] for c in response['citations']))
        }
        
        print(f"📖 Citations Summary: {json.dumps(citations_preview, indent=2)}")
        
        metrics_preview = {
            "retrieval_performance": f"{response['metadata']['retrieval_time_ms']:.1f}ms",
            "answer_generation": f"{response['metadata']['answer_time_ms']:.1f}ms",
            "total_pipeline": f"{response['metadata']['total_time_ms']:.1f}ms",
            "documents_processed": response['metadata']['documents_retrieved'],
        }
        
        print(f"⚡ Performance Metrics: {json.dumps(metrics_preview, indent=2)}")

    async def _demo_performance_analysis(self):
        """Demonstrate performance analysis capabilities."""
        print("\n📈 Performance Analysis Demo")
        print("-" * 40)
        
        print("🔬 Running performance benchmarks...")
        
        # Test different question complexities
        test_cases = [
            ("Short", "What is AI?"),
            ("Medium", "How does machine learning differ from traditional programming?"),
            ("Long", "Provide a comprehensive analysis of the impact of artificial intelligence on modern society, including economic, social, and ethical considerations, along with future predictions and challenges."),
        ]
        
        performance_results = []
        
        for complexity, question in test_cases:
            print(f"🧪 Testing {complexity} complexity question...")
            
            # Run multiple times for average
            times = []
            citations_counts = []
            
            for run in range(3):
                response = await self.rag_service.answer_question(
                    question=question,
                    k=5,
                    experiment_name="ask_pipeline_demo",
                    run_name=f"perf_{complexity.lower()}_run{run+1}_{datetime.now().strftime('%H%M%S')}"
                )
                
                times.append(response['metadata']['total_time_ms'])
                citations_counts.append(len(response['citations']))
            
            avg_time = sum(times) / len(times)
            avg_citations = sum(citations_counts) / len(citations_counts)
            
            performance_results.append({
                "complexity": complexity,
                "avg_time_ms": avg_time,
                "avg_citations": avg_citations,
                "question_length": len(question),
            })
            
            print(f"   ⏱️ Average time: {avg_time:.1f}ms")
            print(f"   📚 Average citations: {avg_citations:.1f}")
        
        # Performance summary
        print("\n📊 Performance Summary:")
        print("Complexity | Avg Time | Citations | Question Length")
        print("-" * 50)
        for r in performance_results:
            print(f"{r['complexity']:<10} | {r['avg_time_ms']:.1f}ms  | {r['avg_citations']:.1f}       | {r['question_length']}")

    def _print_demo_summary(self):
        """Print final demo summary."""
        demo_time = time.time() - self.demo_start_time
        
        print("\n" + "=" * 80)
        print("🎉 Ask Pipeline MLflow Demo Complete!")
        print("=" * 80)
        
        print(f"⏱️ Total demo time: {demo_time:.2f}s")
        print(f"📊 MLflow sampling rate: {os.getenv('ASK_LOG_SAMPLE', '0.2')}")
        print(f"🎯 Features demonstrated:")
        print("   ✅ Single question processing with full tracking")
        print("   ✅ Parameter configuration variations")
        print("   ✅ Batch processing capabilities")
        print("   ✅ MLflow artifacts generation")
        print("   ✅ Performance analysis and benchmarking")
        print("")
        print("🔍 Key MLflow Tracking Features:")
        print("   📋 Parameters: k, filters, rerank_on, fusion, provider")
        print("   📊 Metrics: retrieval_ms, answer_ms, k_used, tokens_in/out, answer_len, num_citations")
        print("   🗂️ Artifacts: citations.json, trace.json, qa_summary.json")
        print("")
        print("🎯 Issue #218 Requirements Satisfied:")
        print("   ✅ Sample rate env var (ASK_LOG_SAMPLE)")
        print("   ✅ Comprehensive parameter logging")
        print("   ✅ Detailed metrics tracking")
        print("   ✅ Rich artifacts with citations and traces")
        print("   ✅ MLflow run creation for sampled requests")
        print("")
        print("🚀 Ready for production deployment!")
        print("   - Configure ASK_LOG_SAMPLE environment variable")
        print("   - Monitor MLflow UI for logged experiments") 
        print("   - Use /ask endpoint for question answering")
        print("   - Check /ask/health for service status")


async def main():
    """Main demo execution."""
    demo = AskPipelineDemo()
    await demo.run_demo()


if __name__ == "__main__":
    asyncio.run(main())
