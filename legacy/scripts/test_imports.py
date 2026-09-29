#!/usr/bin/env python3
import sys
import os
import traceback

# Add the project root to Python path
sys.path.insert(0, '/workspaces/NeuroNews')

print("Testing imports for Issue #235 evaluation framework...")
print("=" * 60)

try:
    print("1. Testing embeddings provider import...")
    from services.embeddings.provider import EmbeddingProvider
    print("✅ EmbeddingProvider import successful")
except Exception as e:
    print(f"❌ EmbeddingProvider import failed: {e}")
    traceback.print_exc()

try:
    print("\\n2. Testing RAG answer service import...")
    from legacy.services.rag.answer import RAGAnswerService
    print("✅ RAGAnswerService import successful")
except Exception as e:
    print(f"❌ RAGAnswerService import failed: {e}")
    traceback.print_exc()

try:
    print("\\n3. Testing MLflow tracking import...")
    from services.mlops.tracking import mlrun
    print("✅ MLflow tracking import successful")
except Exception as e:
    print(f"❌ MLflow tracking import failed: {e}")
    traceback.print_exc()

try:
    print("\\n4. Testing API routes import...")
    from legacy.services.api.routes.ask import AskRequest, ask_question, get_rag_service
    print("✅ API routes import successful")
except Exception as e:
    print(f"❌ API routes import failed: {e}")
    traceback.print_exc()

print("\\n" + "=" * 60)
print("Import testing complete!")
