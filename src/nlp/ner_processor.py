"""
Named Entity Recognition (NER) processor using Hugging Face Transformers.
Extracts People, Organizations, Locations, Technologies, and Policies from text.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

import torch
from transformers import pipeline

logger = logging.getLogger(__name__)


class NERProcessor:
    """
    Named Entity Recognition processor that extracts entities from article text.

    Supports extraction of:
    - PERSON: People's names
    - ORG: Organizations, companies, agencies
    - GPE: Geopolitical entities (countries, cities, states)
    - MISC: Miscellaneous entities (technologies, policies, etc.)
    """

    # Entity type mappings for consistency
    ENTITY_TYPE_MAPPING = {
        "PER": "PERSON",
        "PERSON": "PERSON",
        "ORG": "ORGANIZATION",
        "ORGANIZATION": "ORGANIZATION",
        "GPE": "LOCATION",
        "LOC": "LOCATION",
        "LOCATION": "LOCATION",
        "MISC": "MISCELLANEOUS",
        "MISCELLANEOUS": "MISCELLANEOUS",
    }

    # Technology-related keywords for enhanced detection
    TECHNOLOGY_KEYWORDS = {
        "artificial intelligence",
        "ai",
        "machine learning",
        "ml",
        "deep learning",
        "neural network",
        "blockchain",
        "cryptocurrency",
        "bitcoin",
        "ethereum",
        "cloud computing",
        "aws",
        "azure",
        "google cloud",
        "kubernetes",
        "docker",
        "python",
        "javascript",
        "react",
        "node.js",
        "tensorflow",
        "pytorch",
        "api",
        "rest api",
        "graphql",
        "microservices",
        "devops",
        "ci/cd",
        "cybersecurity",
        "data science",
        "big data",
        "analytics",
        "iot",
        "internet of things",
        "5g",
        "quantum computing",
        "edge computing",
    }

    # Policy-related keywords for enhanced detection
    POLICY_KEYWORDS = {
        "gdpr",
        "ccpa",
        "privacy policy",
        "data protection",
        "regulation",
        "compliance",
        "security policy",
        "terms of service",
        "user agreement",
        "copyright",
        "intellectual property",
        "patent",
        "trademark",
        "open source",
        "license",
        "mit license",
        "apache license",
    }

    def __init__(
        self,
        model_name: str = "dbmdz/bert-large-cased-finetuned-conll03-english",
        device: str = None,
        max_length: int = 512,
        confidence_threshold: float = 0.7,
        *,
        backend: str = "transformers",
        language: str = "en",
    ):
        """
        Initialize the NER processor.

        Args:
            model_name: Name of the pre-trained NER model
            device: Device to run the model on ('cpu', 'cuda', or None for auto-detect)
            max_length: Maximum sequence length for tokenization
            confidence_threshold: Minimum confidence score for entity extraction
        """
        self.model_name = model_name
        self.max_length = max_length
        self.confidence_threshold = confidence_threshold
        self.backend = backend
        self.language = language
        if backend not in {"transformers", "gliner2"}:
            raise ValueError("Unsupported NER backend")
        self.stats = {
            "total_texts_processed": 0,
            "total_entities_extracted": 0,
            "entity_type_counts": {},
            "processing_errors": 0,
        }
        if backend == "gliner2":
            from src.integrations.entities import GLiNER2Extractor

            self.domain_extractor = GLiNER2Extractor(
                device=device or "cpu", threshold=confidence_threshold
            )
            self.model_name = self.domain_extractor.MODEL
            return

        # Determine device
        if device is None:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = device

        logger.info(
            "Initializing NER processor with model: {0} on device: {1}".format(
                model_name, self.device
            )
        )

        try:
            # Initialize the NER pipeline
            self.ner_pipeline = pipeline(
                "ner",
                model=model_name,
                tokenizer=model_name,
                aggregation_strategy="simple",
                device=0 if self.device == "cuda" else -1,
            )

            logger.info("NER processor initialized successfully")

        except Exception as e:
            logger.error("Failed to initialize NER processor: {0}".format(e))
            raise

        # Statistics tracking
        self.stats = {
            "total_texts_processed": 0,
            "total_entities_extracted": 0,
            "entity_type_counts": {},
            "processing_errors": 0,
        }

    def extract_entities(
        self, text: str, article_id: str = None, *, revision_id: str = None
    ) -> List[Dict[str, Any]]:
        """
        Extract named entities from the given text.

        Args:
            text: Input text to process
            article_id: Optional article identifier for logging

        Returns:
            List of extracted entities with metadata
        """
        if self.backend == "gliner2":
            # Preserve original offsets and surface backend errors explicitly.
            # Model proposals must not pass through heuristic relabelling or
            # cleaned-text offsets from the legacy transformer path.
            try:
                run = self.domain_extractor.extract(
                    text,
                    language=self.language,
                    article_id=article_id,
                    revision_id=revision_id,
                )
            except Exception:
                self.stats["processing_errors"] += 1
                raise
            entities = [
                {
                    **entity,
                    "provenance": run["receipt"]["request"],
                    "extraction_receipt_sha256": run["receipt"]["sha256"],
                }
                for entity in run["entities"]
            ]
            self.stats["total_texts_processed"] += 1
            self.stats["total_entities_extracted"] += len(entities)
            for entity in entities:
                label = entity["type"]
                counts = self.stats["entity_type_counts"]
                counts[label] = counts.get(label, 0) + 1
            return entities

        if not text or not text.strip():
            logger.warning(
                "Empty or invalid text provided for article {0}".format(article_id)
            )
            return []

        try:
            # Clean and preprocess text
            cleaned_text = self._preprocess_text(text)

            # Extract entities using the NER model
            raw_entities = self._extract_with_model(cleaned_text)

            # Post-process and enhance entities
            processed_entities = self._post_process_entities(raw_entities, cleaned_text)

            # Update statistics
            self.stats["total_texts_processed"] += 1
            self.stats["total_entities_extracted"] += len(processed_entities)

            for entity in processed_entities:
                entity_type = entity["type"]
                self.stats["entity_type_counts"][entity_type] = (
                    self.stats["entity_type_counts"].get(entity_type, 0) + 1
                )

            logger.info(
                "Extracted {0} entities from article {1}".format(
                    len(processed_entities), article_id
                )
            )
            return processed_entities

        except Exception as e:
            self.stats["processing_errors"] += 1
            logger.error(
                "Error extracting entities from article {0}: {1}".format(article_id, e)
            )
            return []

    def _preprocess_text(self, text: str) -> str:
        """
        Clean and preprocess text for NER.

        Args:
            text: Raw input text

        Returns:
            Cleaned text
        """
        # Remove excessive whitespace
        text = re.sub(r"\s+", " ", text)

        # Remove special characters that might interfere with tokenization
        text = re.sub(r"[^\w\s\.\,\;\:\!\?\-\(\)\"\'\/]", " ", text)

        # Truncate if too long (keeping some buffer for tokenization)
        if len(text) > self.max_length * 4:  # Rough estimate of token to char ratio
            text = text[: self.max_length * 4]
            # Try to cut at sentence boundary
            last_period = text.rfind(".")
            if last_period > self.max_length * 2:
                text = text[: last_period + 1]

        return text.strip()

    def _extract_with_model(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract entities using the NER model.

        Args:
            text: Preprocessed text

        Returns:
            Raw entities from the model
        """
        try:
            # Split text into chunks if it's too long
            text_chunks = self._split_text(text)
            all_entities = []

            for chunk in text_chunks:
                entities = self.ner_pipeline(chunk)

                # Filter by confidence threshold
                filtered_entities = [
                    entity
                    for entity in entities
                    if entity["score"] >= self.confidence_threshold
                ]

                all_entities.extend(filtered_entities)

            return all_entities

        except Exception as e:
            logger.error("Error in model prediction: {0}".format(e))
            return []

    def _split_text(self, text: str) -> List[str]:
        """
        Split text into chunks that fit within model's max length.

        Args:
            text: Input text

        Returns:
            List of text chunks
        """
        # Simple sentence-based splitting
        sentences = re.split(r"[.!?]+", text)
        chunks = []
        current_chunk = ""

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            # Estimate tokens (rough approximation: 1 token ≈ 4 characters)
            estimated_tokens = len(current_chunk + sentence) // 4

            if estimated_tokens > self.max_length - 50:  # Leave some buffer
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = sentence
                else:
                    # Single sentence is too long, take it as is
                    chunks.append(sentence[: self.max_length * 4])
            else:
                current_chunk += " " + sentence if current_chunk else sentence

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks if chunks else [text]

    def _post_process_entities(
        self, entities: List[Dict[str, Any]], text: str
    ) -> List[Dict[str, Any]]:
        """
        Post-process and enhance extracted entities.

        Args:
            entities: Raw entities from the model
            text: Original text for context

        Returns:
            Enhanced entities with additional metadata
        """
        processed_entities = []
        seen_entities = set()  # To avoid duplicates

        for entity in entities:
            # Normalize entity text
            entity_text = entity["word"].strip()
            if not entity_text:
                continue

            # Clean up subword tokens (BERT-style)
            entity_text = re.sub(r"##", "", entity_text)
            entity_text = re.sub(r"\s+", " ", entity_text).strip()

            # Skip if too short or already seen
            if len(entity_text) < 2 or entity_text.lower() in seen_entities:
                continue

            # Map entity type
            entity_type = self.ENTITY_TYPE_MAPPING.get(
                entity["entity_group"], entity["entity_group"]
            )

            # Enhanced entity detection for specific domains
            enhanced_type = self._enhance_entity_type(entity_text, entity_type, text)

            processed_entity = {
                "text": entity_text,
                "type": enhanced_type,
                "confidence": float(entity["score"]),
                "start_position": int(entity.get("start", 0)),
                "end_position": int(entity.get("end", 0)),
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            }

            processed_entities.append(processed_entity)
            seen_entities.add(entity_text.lower())

        # Sort by confidence score (highest first)
        processed_entities.sort(key=lambda x: x["confidence"], reverse=True)

        return processed_entities

    def _enhance_entity_type(
        self, entity_text: str, original_type: str, context: str
    ) -> str:
        """
        Enhance entity type classification with domain-specific rules.

        Args:
            entity_text: The entity text
            original_type: Original type from the model
            context: Full text context

        Returns:
            Enhanced entity type
        """
        entity_lower = entity_text.lower()

        # Technology detection
        for tech_keyword in self.TECHNOLOGY_KEYWORDS:
            if tech_keyword in entity_lower:
                return "TECHNOLOGY"

        # Policy detection
        for policy_keyword in self.POLICY_KEYWORDS:
            if policy_keyword in entity_lower:
                return "POLICY"

        # Organization patterns for tech companies
        if original_type == "ORGANIZATION":
            tech_org_patterns = [
                r"\b\w+\s*(inc|corp|ltd|llc|technologies|systems|software|labs)\b",
                r"\b(google|microsoft|apple|amazon|facebook|meta|tesla|nvidia|intel)\b",
            ]
            for pattern in tech_org_patterns:
                if re.search(pattern, entity_lower):
                    return "TECHNOLOGY_ORGANIZATION"

        return original_type

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get processing statistics.

        Returns:
            Dictionary with processing statistics
        """
        return {
            "total_texts_processed": self.stats["total_texts_processed"],
            "total_entities_extracted": self.stats["total_entities_extracted"],
            "average_entities_per_text": (
                self.stats["total_entities_extracted"]
                / max(1, self.stats["total_texts_processed"])
            ),
            "entity_type_distribution": self.stats["entity_type_counts"],
            "processing_errors": self.stats["processing_errors"],
            "model_info": {
                "model_name": self.model_name,
                "device": self.device,
                "confidence_threshold": self.confidence_threshold,
            },
        }

    def reset_statistics(self):
        """Reset processing statistics."""
        self.stats = {
            "total_texts_processed": 0,
            "total_entities_extracted": 0,
            "entity_type_counts": {},
            "processing_errors": 0,
        }
        logger.info("NER processor statistics reset")


def create_ner_processor(model_name: str = None, **kwargs) -> NERProcessor:
    """
    Factory function to create a NER processor.

    Args:
        model_name: Name of the pre-trained model
        **kwargs: Additional arguments for NERProcessor

    Returns:
        Configured NERProcessor instance
    """
    if model_name is None:
        model_name = "dbmdz/bert-large-cased-finetuned-conll03-english"

    return NERProcessor(model_name=model_name, **kwargs)
