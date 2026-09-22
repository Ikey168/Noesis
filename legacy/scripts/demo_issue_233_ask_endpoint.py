"""
Demo and verification script for Issue #233: Answering pipeline + citations (FastAPI /ask)

This script tests the /ask endpoint both programmatically and via curl
to verify DoD requirements are met.
"""

import asyncio
import json
import os
import sys
import time
import subprocess
from typing import Any, Dict

# Add to path for imports
sys.path.append(os.path.dirname(__file__))

try:
    from legacy.services.api.routes.ask import AskRequest, ask_question, get_rag_service
    from legacy.services.rag.answer import RAGAnswerService
except ImportError as e:
    print(f"Import error: {e}")
    print("Please ensure you're running from the project root directory")
    sys.exit(1)


async def test_ask_pipeline():
    """Test the ask pipeline programmatically."""
    print("=" * 60)
    print("Testing Ask Pipeline - Issue #233")
    print("=" * 60)
    
    # Test question
    question = "What are the latest developments in artificial intelligence?"
    
    print(f"Question: {question}")
    print()
    
    # Create request
    request = AskRequest(
        question=question,
        k=5,
        rerank_on=True,
        fusion=True,
        provider="openai"
    )
    
    # Get service and process
    print("Initializing RAG service...")
    rag_service = get_rag_service()
    
    print("Processing question...")
    start_time = time.time()
    
    try:
        response = await ask_question(request, rag_service)
        
        end_time = time.time()
        total_time = (end_time - start_time) * 1000
        
        print(f"✅ Processing completed in {total_time:.1f}ms")
        print()
        
        # Display results
        print("RESPONSE:")
        print("-" * 40)
        print(f"Question: {response.question}")
        print(f"Answer: {response.answer[:200]}...")
        print()
        
        print(f"Citations ({len(response.citations)}):")
        for i, citation in enumerate(response.citations[:3], 1):
            print(f"  {i}. {citation.get('title', 'N/A')}")
            print(f"     URL: {citation.get('url', 'N/A')}")
            print(f"     Score: {citation.get('relevance_score', 'N/A')}")
            print()
        
        print("Metadata:")
        metadata = response.metadata
        print(f"  - Retrieval time: {metadata.get('retrieval_time_ms', 'N/A')}ms")
        print(f"  - Answer time: {metadata.get('answer_time_ms', 'N/A')}ms")
        print(f"  - Total time: {metadata.get('total_time_ms', 'N/A')}ms")
        print(f"  - Documents retrieved: {metadata.get('documents_retrieved', 'N/A')}")
        print(f"  - Provider: {metadata.get('provider_used', 'N/A')}")
        print(f"  - Request ID: {response.request_id}")
        print(f"  - MLflow tracked: {response.tracked_in_mlflow}")
        
        # DoD verification
        print()
        print("DoD VERIFICATION:")
        print("-" * 40)
        
        # Check answer exists
        has_answer = bool(response.answer and len(response.answer.strip()) > 0)
        print(f"✅ Answer provided: {has_answer}")
        
        # Check 3+ citations
        has_citations = len(response.citations) >= 3
        print(f"✅ 3+ citations: {has_citations} ({len(response.citations)} citations)")
        
        # Check latency stats
        has_latency = 'total_time_ms' in metadata and metadata['total_time_ms'] > 0
        print(f"✅ Latency stats: {has_latency} ({metadata.get('total_time_ms', 'N/A')}ms)")
        
        success = has_answer and has_citations and has_latency
        print(f"\\n{'✅ ALL DOD REQUIREMENTS MET' if success else '❌ DoD requirements not met'}")
        
        return success
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return False


def test_curl_endpoint():
    """Test the curl endpoint as required by DoD."""
    print()
    print("=" * 60)
    print("Testing curl /ask endpoint - DoD Requirement")
    print("=" * 60)
    
    # Note: This would require the server to be running
    curl_command = [
        "curl",
        "-X", "GET",
        "http://localhost:8000/api/ask?q=What%20is%20machine%20learning?&k=5",
        "-H", "accept: application/json"
    ]
    
    print("Curl command to test:")
    print(" ".join(curl_command))
    print()
    print("Note: To test with curl, run:")
    print("1. python legacy/services/api/main.py")
    print("2. curl 'http://localhost:8000/api/ask?q=What%20is%20AI?&k=5'")
    print()


async def main():
    """Main demo function."""
    print("Issue #233 Demo: Answering pipeline + citations (FastAPI /ask)")
    print()
    
    # Test programmatically
    success = await test_ask_pipeline()
    
    # Show curl testing info
    test_curl_endpoint()
    
    if success:
        print("🎉 Issue #233 implementation appears to be working!")
        print("   Ready for commit and PR creation.")
    else:
        print("❌ Issue #233 implementation needs fixes.")
    
    return success


if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result else 1)
