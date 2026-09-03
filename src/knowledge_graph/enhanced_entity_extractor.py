"""
Enhanced Entity Relationship Extractor for Knowledge Graph Population - Issue #36

This module provides comprehensive entity extraction and relationship detection
specifically designed to work with the optimized NLP pipeline and populate
the Neptune knowledge graph with structured entity relationships.

Key Features:
- Advanced named entity recognition (People, Organizations, Technologies, Policies)
- Sophisticated relationship extraction between entities
- Integration with optimized NLP pipeline from Issue #35
- AWS Neptune graph population with entity linking
- SPARQL/Gremlin query support for verification
"""

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# Import optimized NLP components from Issue #35.  Keep the symbols bound in
# lean installations so callers get a deliberate availability error rather
# than an environment-dependent NameError.
OPTIMIZED_NLP_AVAILABLE = False
IntegratedNLPProcessor = None
NLPConfig = None
try:
    from src.nlp.nlp_integration import IntegratedNLPProcessor
    from src.nlp.optimized_nlp_pipeline import NLPConfig
except Exception:  # optional heavyweight NLP dependencies
    pass
else:
    OPTIMIZED_NLP_AVAILABLE = True

# Import existing components
KNOWLEDGE_GRAPH_AVAILABLE = False
NERProcessor = None
try:
    from src.nlp.ner_processor import NERProcessor
except Exception:  # optional model dependencies
    pass
else:
    KNOWLEDGE_GRAPH_AVAILABLE = True

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class EnhancedEntity:
    """Enhanced entity with additional metadata for knowledge graph population."""

    text: str
    label: str
    start: int
    end: int
    confidence: float = 0.0
    normalized_form: str = ""
    entity_id: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)
    source_article_id: Optional[str] = None
    mention_count: int = 1

    def __post_init__(self):
        if not self.normalized_form:
            self.normalized_form = self._normalize_text(self.text)
        if not self.entity_id:
            self.entity_id = self._generate_entity_id()

    def _normalize_text(self, text: str) -> str:
        """Normalize entity text for consistent matching."""
        # Remove extra whitespace and convert to proper case
        normalized = re.sub(r"\s+", " ", text.strip())

        # Handle organization suffixes
        if self.label == "ORGANIZATION":
            normalized = re.sub(
                r"\b(Inc\.?|LLC\.?|Corp\.?|Ltd\.?|Co\.?)\b",
                "",
                normalized,
                flags=re.IGNORECASE,
            )
            normalized = normalized.strip()

        # Handle person names (capitalize properly)
        if self.label == "PERSON":
            normalized = " ".join(word.capitalize()
                                  for word in normalized.split())

        return normalized

    def _generate_entity_id(self) -> str:
        """Generate a unique entity ID based on normalized form and type."""
        import hashlib

        content = "{0}:{1}".format(self.label, self.normalized_form)
        return hashlib.md5(content.encode()).hexdigest()[:12]


@dataclass
class EnhancedRelationship:
    """Enhanced relationship with additional context and validation."""

    source_entity: EnhancedEntity
    target_entity: EnhancedEntity
    relation_type: str
    confidence: float
    context: str = ""
    article_id: Optional[str] = None
    evidence_sentences: List[str] = field(default_factory=list)
    temporal_info: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert relationship to dictionary for Neptune storage."""
        return {
            "source_id": self.source_entity.entity_id,
            "target_id": self.target_entity.entity_id,
            "relation_type": self.relation_type,
            "confidence": self.confidence,
            "context": self.context,
            "article_id": self.article_id,
            "evidence_count": len(self.evidence_sentences),
            "temporal_info": self.temporal_info or {},
        }


class AdvancedEntityExtractor:
    """
    Advanced entity extractor that integrates with the optimized NLP pipeline
    and provides sophisticated entity recognition and relationship detection.
    """

    # Enhanced entity type mappings
    ENTITY_TYPES = {
        "PERSON": {
            "neptune_label": "Person",
            "properties": ["name", "title", "organization", "role"],
            "patterns": [
                # Full names
                r"\b[A-Z][a-z]+ [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b",
                r"\b(?:Dr\.|Prof\.|Mr\.|Ms\.|Mrs\.)\s+[A-Z][a-z]+\b",  # Titles
            ],
        },
        "ORGANIZATION": {
            "neptune_label": "Organization",
            "properties": ["orgName", "industry", "headquarters", "type"],
            "patterns": [
                r"\b[A-Z][a-z]*(?:\s+[A-Z][a-z]*)*\s+(?:Inc\.?|LLC|Corp\.?|Ltd\.?|Co\.?)\b",
                r"\b[A-Z][A-Z]+\b",  # Acronyms
                r"\bGoogle|Microsoft|Apple|Amazon|Facebook|Meta|Tesla|Twitter|LinkedIn\b",
            ],
        },
        "TECHNOLOGY": {
            "neptune_label": "Technology",
            "properties": ["techName", "category", "description", "version"],
            "keywords": [
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
                "kubernetes",
                "docker",
                "python",
                "javascript",
                "tensorflow",
                "pytorch",
                "api",
                "rest api",
                "graphql",
                "microservices",
                "cybersecurity",
                "data science",
                "big data",
                "iot",
                "5g",
                "quantum computing",
            ],
        },
        "POLICY": {
            "neptune_label": "Policy",
            "properties": ["policyName", "type", "jurisdiction", "status"],
            "keywords": [
                "gdpr",
                "ccpa",
                "privacy policy",
                "data protection",
                "regulation",
                "compliance",
                "security policy",
                "patent",
                "copyright",
                "trademark",
                "open source",
                "license",
                "terms of service",
                "user agreement",
            ],
        },
        "LOCATION": {
            "neptune_label": "Location",
            "properties": ["locationName", "type", "country", "region"],
            "patterns": [
                # City, State
                r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,\s*[A-Z]{2}\b",
                # Silicon Valley, etc.
                r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+Valley\b",
            ],
        },
    }

    # Relationship patterns and types
    RELATIONSHIP_PATTERNS = {
        "WORKS_FOR": [
            r"(\w+(?:\s+\w+)*)\s+(?:works?\s+(?:for|at)|is\s+employed\s+by|joins|joined)\s+(\w+(?:\s+\w+)*)",
            r"(\w+(?:\s+\w+)*),?\s+(?:CEO|CTO|president|director|manager|employee)\s+(?:of|at)\s+(\w+(?:\s+\w+)*)",
            r"(\w+(?:\s+\w+)*)\s+(?:leads|headed|managing)\s+(\w+(?:\s+\w+)*)",
        ],
        "PARTNERS_WITH": [
            r"(\w+(?:\s+\w+)*)\s+(?:partners?\s+with|collaborates?\s+with|teams?\s+up\s+with)\s+(\w+(?:\s+\w+)*)",
            r"(\w+(?:\s+\w+)*)\s+(?:and|&)\s+(\w+(?:\s+\w+)*)\s+(?:partnership|collaboration|alliance)",
        ],
        "COMPETES_WITH": [
            r"(\w+(?:\s+\w+)*)\s+(?:competes?\s+with|rivals?|challenges?)\s+(\w+(?:\s+\w+)*)",
            r"(\w+(?:\s+\w+)*)\s+(?:vs\.?|versus)\s+(\w+(?:\s+\w+)*)",
        ],
        "ACQUIRED_BY": [
            r"(\w+(?:\s+\w+)*)\s+(?:acquired|bought|purchased)\s+(?:by\s+)?(\w+(?:\s+\w+)*)",
            r"(\w+(?:\s+\w+)*)\s+(?:acquisition|buyout|purchase)\s+(?:by\s+)?(\w+(?:\s+\w+)*)",
        ],
        "DEVELOPS": [
            r"(\w+(?:\s+\w+)*)\s+(?:develops?|creates?|builds?|designs?)\s+(\w+(?:\s+\w+)*)",
            r"(\w+(?:\s+\w+)*)\s+(?:is\s+developing|has\s+developed|will\s+develop)\s+(\w+(?:\s+\w+)*)",
        ],
        "USES_TECHNOLOGY": [
            r"(\w+(?:\s+\w+)*)\s+(?:uses?|utilizes?|implements?|adopts?)\s+(\w+(?:\s+\w+)*)",
            r"(\w+(?:\s+\w+)*)\s+(?:powered\s+by|based\s+on|built\s+with)\s+(\w+(?:\s+\w+)*)",
        ],
        "REGULATES": [
            r"(\w+(?:\s+\w+)*)\s+(?:regulates?|governs?|oversees?)\s+(\w+(?:\s+\w+)*)",
            r"(\w+(?:\s+\w+)*)\s+(?:is\s+regulated\s+by|under\s+the\s+jurisdiction\s+of)\s+(\w+(?:\s+\w+)*)",
        ],
        "LOCATED_IN": [
            r"(\w+(?:\s+\w+)*)\s+(?:(?:is\s+)?(?:located|based|headquartered)\s+in)\s+(\w+(?:\s+\w+)*)",
            # Company, Location
            r"(\w+(?:\s+\w+)*),?\s+(\w+(?:\s+\w+)*(?:,\s*[A-Z]{2})?)",
        ],
    }

    def __init__(self, config: Dict[str, Any] = None):
        """
        Initialize the advanced entity extractor.

        Args:
            config: Configuration dictionary for the extractor
        """
        self.config = config or {}
        self.confidence_threshold = self.config.get(
            "confidence_threshold", 0.7)
        self.max_entity_distance = self.config.get("max_entity_distance", 200)
        self.min_relationship_confidence = self.config.get(
            "min_relationship_confidence", 0.6
        )

        # Initialize NLP components
        self.ner_processor = None
        self.nlp_pipeline = None

        # Entity and relationship caches
        self.entity_cache = {}
        self.relationship_cache = defaultdict(list)

        # Statistics
        self.stats = {
            "entities_extracted": 0,
            "relationships_found": 0,
            "articles_processed": 0,
            "cache_hits": 0,
            "processing_time": 0.0,
        }

        logger.info("AdvancedEntityExtractor initialized")

    async def initialize_nlp_components(self):
        """Initialize NLP processing components."""
        try:
            # Try to use optimized NLP pipeline first
            if (
                OPTIMIZED_NLP_AVAILABLE
                and NLPConfig is not None
                and IntegratedNLPProcessor is not None
            ):
                nlp_config = NLPConfig(
                    max_worker_threads=4,
                    batch_size=16,
                    enable_redis_cache=True,
                    use_gpu_if_available=True,
                )
                self.nlp_pipeline = IntegratedNLPProcessor(nlp_config)
                logger.info(
                    "Using optimized NLP pipeline for entity extraction")

            # Initialize NER processor
            if NERProcessor is None:
                raise RuntimeError("NER processor dependencies are unavailable")
            self.ner_processor = NERProcessor(
                confidence_threshold=self.confidence_threshold
            )

            logger.info("NLP components initialized successfully")

        except Exception as e:
            logger.error("Failed to initialize NLP components: {0}".format(e))
            raise

    async def extract_entities_from_article(
        self, article_id: str, title: str, content: str
    ) -> List[EnhancedEntity]:
        """
        Extract enhanced entities from an article using multiple techniques.

        Args:
            article_id: Unique identifier for the article
            title: Article title
            content: Article content

        Returns:
            List of enhanced entities with metadata
        """
        if not self.ner_processor:
            await self.initialize_nlp_components()

        start_time = datetime.now()

        try:
            full_text = "{0}. {1}".format(title, content)

            # Extract entities using NER processor
            ner_entities = self.ner_processor.extract_entities(
                full_text, article_id)

            # Convert to enhanced entities
            enhanced_entities = []
            for ent in ner_entities:
                enhanced_entity = EnhancedEntity(
                    text=ent.get("text", ""),
                    label=ent.get("type", ""),
                    start=ent.get("start", 0),
                    end=ent.get("end", 0),
                    confidence=ent.get("confidence", 0.0),
                    source_article_id=article_id,
                )

                # Add additional properties based on entity type
                enhanced_entity.properties = self._extract_entity_properties(
                    enhanced_entity, full_text
                )

                enhanced_entities.append(enhanced_entity)

            # Extract additional entities using pattern matching
            pattern_entities = await self._extract_entities_by_patterns(
                full_text, article_id
            )
            enhanced_entities.extend(pattern_entities)

            # Extract technology and policy entities using keyword matching
            keyword_entities = await self._extract_entities_by_keywords(
                full_text, article_id
            )
            enhanced_entities.extend(keyword_entities)

            # Deduplicate and merge similar entities
            deduplicated_entities = self._deduplicate_entities(
                enhanced_entities)

            # Update statistics
            processing_time = (datetime.now() - start_time).total_seconds()
            self.stats["entities_extracted"] += len(deduplicated_entities)
            self.stats["articles_processed"] += 1
            self.stats["processing_time"] += processing_time

            logger.info(
                "Extracted {0} entities from article {1}".format(
                    len(deduplicated_entities), article_id
                )
            )
            return deduplicated_entities

        except Exception as e:
            logger.error(
                "Error extracting entities from article {0}: {1}".format(
                    article_id, e)
            )
            return []

    async def extract_relationships(
        self, entities: List[EnhancedEntity], text: str, article_id: str
    ) -> List[EnhancedRelationship]:
        """
        Extract relationships between entities using advanced pattern matching.

        Args:
            entities: List of entities to find relationships between
            text: Source text containing the entities
            article_id: Identifier for the source article

        Returns:
            List of enhanced relationships with confidence scores
        """
        relationships = []

        try:
            # Pattern-based relationship extraction
            pattern_relationships = await self._extract_relationships_by_patterns(
                entities, text, article_id
            )
            relationships.extend(pattern_relationships)

            # Co-occurrence based relationships
            cooccurrence_relationships = (
                await self._extract_relationships_by_cooccurrence(
                    entities, text, article_id
                )
            )
            relationships.extend(cooccurrence_relationships)

            # Contextual relationship extraction
            contextual_relationships = await self._extract_relationships_by_context(
                entities, text, article_id
            )
            relationships.extend(contextual_relationships)

            # Filter by confidence threshold
            filtered_relationships = [
                rel
                for rel in relationships
                if rel.confidence >= self.min_relationship_confidence
            ]

            # Update statistics
            self.stats["relationships_found"] += len(filtered_relationships)

            logger.info(
                "Extracted {0} relationships for article {1}".format(
                    len(filtered_relationships), article_id
                )
            )
            return filtered_relationships

        except Exception as e:
            logger.error(
                "Error extracting relationships for article {0}: {1}".format(
                    article_id, e
                )
            )
            return []

    async def _extract_entities_by_patterns(
        self, text: str, article_id: str
    ) -> List[EnhancedEntity]:
        """Extract entities using regex patterns for specific types."""
        pattern_entities = []

        for entity_type, config in self.ENTITY_TYPES.items():
            if "patterns" not in config:
                continue

            for pattern in config["patterns"]:
                matches = re.finditer(pattern, text, re.IGNORECASE)

                for match in matches:
                    entity = EnhancedEntity(
                        text=match.group().strip(),
                        label=entity_type,
                        start=match.start(),
                        end=match.end(),
                        confidence=0.8,  # Pattern-based confidence
                        source_article_id=article_id,
                    )
                    pattern_entities.append(entity)

        return pattern_entities

    async def _extract_entities_by_keywords(
        self, text: str, article_id: str
    ) -> List[EnhancedEntity]:
        """Extract entities using keyword matching for technologies and policies."""
        keyword_entities = []
        text_lower = text.lower()

        for entity_type, config in self.ENTITY_TYPES.items():
            if "keywords" not in config:
                continue

            for keyword in config["keywords"]:
                pattern = r"\b" + re.escape(keyword) + r"\b"
                matches = re.finditer(pattern, text_lower)

                for match in matches:
                    # Get the original case from the text
                    original_text = text[match.start(): match.end()]

                    entity = EnhancedEntity(
                        text=original_text,
                        label=entity_type,
                        start=match.start(),
                        end=match.end(),
                        confidence=0.9,  # High confidence for keyword matches
                        source_article_id=article_id,
                    )
                    keyword_entities.append(entity)

        return keyword_entities

    def _extract_entity_properties(
        self, entity: EnhancedEntity, text: str
    ) -> Dict[str, Any]:
        """Extract additional properties for an entity based on context."""
        properties = {}

        # Extract context around the entity
        context_start = max(0, entity.start - 100)
        context_end = min(len(text), entity.end + 100)
        context = text[context_start:context_end]

        if entity.label == "PERSON":
            # Extract title/role information
            title_patterns = [
                r"(CEO|CTO|President|Director|Manager|Engineer|Scientist|Researcher)",
                r"(Dr\.|Prof\.|Mr\.|Ms\.|Mrs\.)",
            ]
            for pattern in title_patterns:
                match = re.search(pattern, context, re.IGNORECASE)
                if match:
                    properties["title"] = match.group(1)
                    break

        elif entity.label == "ORGANIZATION":
            # Extract organization type
            org_type_patterns = [
                r"(company|corporation|startup|university|government|agency|foundation)",
                r"(Inc\.?|LLC|Corp\.?|Ltd\.?|Co\.?)",
            ]
            for pattern in org_type_patterns:
                match = re.search(pattern, context, re.IGNORECASE)
                if match:
                    properties["type"] = match.group(1)
                    break

        elif entity.label == "TECHNOLOGY":
            # Extract technology category
            tech_categories = {
                "ai": ["artificial intelligence", "machine learning", "deep learning"],
                "blockchain": ["blockchain", "cryptocurrency", "bitcoin"],
                "cloud": ["cloud computing", "aws", "azure", "google cloud"],
                "programming": ["python", "javascript", "java", "c++"],
            }

            entity_text_lower = entity.text.lower()
            for category, keywords in tech_categories.items():
                if any(keyword in entity_text_lower for keyword in keywords):
                    properties["category"] = category
                    break

        return properties

    def _deduplicate_entities(
        self, entities: List[EnhancedEntity]
    ) -> List[EnhancedEntity]:
        """Deduplicate entities based on normalized form and merge similar ones."""
        entity_map = {}

        for entity in entities:
            key = "{0}:{1}".format(entity.label, entity.normalized_form)

            if key in entity_map:
                # Merge with existing entity
                existing = entity_map[key]
                existing.mention_count += 1
                existing.confidence = max(
                    existing.confidence, entity.confidence)

                # Merge properties
                for prop_key, prop_value in entity.properties.items():
                    if prop_key not in existing.properties:
                        existing.properties[prop_key] = prop_value

                # Add to aliases if text is different
                if entity.text not in existing.aliases and entity.text != existing.text:
                    existing.aliases.append(entity.text)
            else:
                entity_map[key] = entity

        return list(entity_map.values())

    async def _extract_relationships_by_patterns(
        self, entities: List[EnhancedEntity], text: str, article_id: str
    ) -> List[EnhancedRelationship]:
        """Extract relationships using predefined patterns."""
        relationships = []

        for relation_type, patterns in self.RELATIONSHIP_PATTERNS.items():
            for pattern in patterns:
                matches = re.finditer(pattern, text, re.IGNORECASE)

                for match in matches:
                    source_text = match.group(1).strip()
                    target_text = match.group(2).strip()

                    # Find matching entities
                    source_entity = self._find_matching_entity(
                        source_text, entities)
                    target_entity = self._find_matching_entity(
                        target_text, entities)

                    if (
                        source_entity
                        and target_entity
                        and source_entity != target_entity
                    ):
                        confidence = (
                            min(source_entity.confidence,
                                target_entity.confidence)
                            * 0.9
                        )

                        relationship = EnhancedRelationship(
                            source_entity=source_entity,
                            target_entity=target_entity,
                            relation_type=relation_type,
                            confidence=confidence,
                            context=match.group(0),
                            article_id=article_id,
                            evidence_sentences=[match.group(0)],
                        )
                        relationships.append(relationship)

        return relationships

    async def _extract_relationships_by_cooccurrence(
        self, entities: List[EnhancedEntity], text: str, article_id: str
    ) -> List[EnhancedRelationship]:
        """Extract relationships based on entity co-occurrence proximity."""
        relationships = []

        for i, entity1 in enumerate(entities):
            for entity2 in entities[i + 1:]:
                distance = abs(entity1.start - entity2.start)

                if distance <= self.max_entity_distance:
                    # Determine relationship type based on entity types
                    relation_type = self._infer_relationship_type(
                        entity1, entity2)

                    if relation_type:
                        # Calculate confidence based on distance and entity
                        # confidence
                        distance_factor = 1 - \
                            (distance / self.max_entity_distance)
                        confidence = (
                            min(entity1.confidence, entity2.confidence)
                            * distance_factor
                            * 0.7
                        )

                        # Extract context
                        context_start = max(
                            0, min(entity1.start, entity2.start) - 50)
                        context_end = min(len(text), max(
                            entity1.end, entity2.end) + 50)
                        context = text[context_start:context_end]

                        relationship = EnhancedRelationship(
                            source_entity=entity1,
                            target_entity=entity2,
                            relation_type=relation_type,
                            confidence=confidence,
                            context=context,
                            article_id=article_id,
                        )
                        relationships.append(relationship)

        return relationships

    async def _extract_relationships_by_context(
        self, entities: List[EnhancedEntity], text: str, article_id: str
    ) -> List[EnhancedRelationship]:
        """Extract relationships using contextual analysis."""
        relationships = []

        # Split text into sentences for contextual analysis
        sentences = re.split(r"[.!?]+", text)

        for sentence in sentences:
            sentence_entities = [
                entity
                for entity in entities
                if entity.start >= text.find(sentence)
                and entity.end <= text.find(sentence) + len(sentence)
            ]

            if len(sentence_entities) >= 2:
                # Analyze sentence for relationship indicators
                relationship_indicators = {
                    "WORKS_FOR": ["works", "employee", "CEO", "director", "manager"],
                    "PARTNERS_WITH": [
                        "partnership",
                        "collaboration",
                        "alliance",
                        "joint",
                    ],
                    "COMPETES_WITH": ["compete", "rival", "versus", "against"],
                    "DEVELOPS": ["develop", "create", "build", "design", "launch"],
                    "USES_TECHNOLOGY": ["use", "implement", "adopt", "deploy"],
                }

                sentence_lower = sentence.lower()

                for relation_type, indicators in relationship_indicators.items():
                    if any(indicator in sentence_lower for indicator in indicators):
                        # Create relationships between entities in this
                        # sentence
                        for i, entity1 in enumerate(sentence_entities):
                            for entity2 in sentence_entities[i + 1:]:
                                if self._validate_relationship_types(
                                    entity1, entity2, relation_type
                                ):
                                    confidence = (
                                        min(entity1.confidence,
                                            entity2.confidence)
                                        * 0.8
                                    )

                                    relationship = EnhancedRelationship(
                                        source_entity=entity1,
                                        target_entity=entity2,
                                        relation_type=relation_type,
                                        confidence=confidence,
                                        context=sentence,
                                        article_id=article_id,
                                        evidence_sentences=[sentence],
                                    )
                                    relationships.append(relationship)

        return relationships

    def _find_matching_entity(
        self, text: str, entities: List[EnhancedEntity]
    ) -> Optional[EnhancedEntity]:
        """Find an entity that matches the given text."""
        text_normalized = text.strip().lower()

        for entity in entities:
            if (
                entity.normalized_form.lower() == text_normalized
                or entity.text.lower() == text_normalized
                or text_normalized in [alias.lower() for alias in entity.aliases]
            ):
                return entity

        return None

    def _infer_relationship_type(
        self, entity1: EnhancedEntity, entity2: EnhancedEntity
    ) -> Optional[str]:
        """Infer relationship type based on entity types."""
        type_combinations = {
            ("PERSON", "ORGANIZATION"): "WORKS_FOR",
            ("ORGANIZATION", "ORGANIZATION"): "PARTNERS_WITH",
            ("ORGANIZATION", "TECHNOLOGY"): "DEVELOPS",
            ("PERSON", "TECHNOLOGY"): "DEVELOPS",
            ("ORGANIZATION", "LOCATION"): "LOCATED_IN",
            ("PERSON", "LOCATION"): "LOCATED_IN",
            ("TECHNOLOGY", "POLICY"): "REGULATES",
            ("ORGANIZATION", "POLICY"): "REGULATES",
        }

        key1 = (entity1.label, entity2.label)
        key2 = (entity2.label, entity1.label)

        return type_combinations.get(key1) or type_combinations.get(key2)

    def _validate_relationship_types(
        self, entity1: EnhancedEntity, entity2: EnhancedEntity, relation_type: str
    ) -> bool:
        """Validate if a relationship type is appropriate for the given entity types."""
        valid_combinations = {
            "WORKS_FOR": [("PERSON", "ORGANIZATION")],
            "PARTNERS_WITH": [("ORGANIZATION", "ORGANIZATION"), ("PERSON", "PERSON")],
            "COMPETES_WITH": [
                ("ORGANIZATION", "ORGANIZATION"),
                ("TECHNOLOGY", "TECHNOLOGY"),
            ],
            "DEVELOPS": [("ORGANIZATION", "TECHNOLOGY"), ("PERSON", "TECHNOLOGY")],
            "USES_TECHNOLOGY": [
                ("ORGANIZATION", "TECHNOLOGY"),
                ("PERSON", "TECHNOLOGY"),
            ],
            "LOCATED_IN": [("ORGANIZATION", "LOCATION"), ("PERSON", "LOCATION")],
            "REGULATES": [("POLICY", "TECHNOLOGY"), ("POLICY", "ORGANIZATION")],
        }

        if relation_type not in valid_combinations:
            return True  # Allow unknown relationship types

        valid_types = valid_combinations[relation_type]
        entity_types = (entity1.label, entity2.label)
        reverse_types = (entity2.label, entity1.label)

        return entity_types in valid_types or reverse_types in valid_types

    def get_extraction_statistics(self) -> Dict[str, Any]:
        """Get comprehensive extraction statistics."""
        return {
            "entities_extracted": self.stats["entities_extracted"],
            "relationships_found": self.stats["relationships_found"],
            "articles_processed": self.stats["articles_processed"],
            "cache_hits": self.stats["cache_hits"],
            "total_processing_time": self.stats["processing_time"],
            "average_entities_per_article": (
                self.stats["entities_extracted"] /
                    self.stats["articles_processed"]
                if self.stats["articles_processed"] > 0
                else 0
            ),
            "average_relationships_per_article": (
                self.stats["relationships_found"] /
                    self.stats["articles_processed"]
                if self.stats["articles_processed"] > 0
                else 0
            ),
        }


# Factory functions for easy usage


def create_advanced_entity_extractor(
    confidence_threshold: float = 0.7,
) -> AdvancedEntityExtractor:
    """Create an advanced entity extractor with standard configuration."""
    config = {
        "confidence_threshold": confidence_threshold,
        "max_entity_distance": 200,
        "min_relationship_confidence": 0.6,
    }
    return AdvancedEntityExtractor(config)


def create_high_precision_entity_extractor() -> AdvancedEntityExtractor:
    """Create a high-precision entity extractor for accurate results."""
    config = {
        "confidence_threshold": 0.85,
        "max_entity_distance": 150,
        "min_relationship_confidence": 0.8,
    }
    return AdvancedEntityExtractor(config)


def create_high_recall_entity_extractor() -> AdvancedEntityExtractor:
    """Create a high-recall entity extractor for comprehensive extraction."""
    config = {
        "confidence_threshold": 0.5,
        "max_entity_distance": 300,
        "min_relationship_confidence": 0.4,
    }
    return AdvancedEntityExtractor(config)


if __name__ == "__main__":
    # Example usage and testing

    async def main():
        # Sample article for testing
        sample_article = {
            "id": "test_article_001",
            "title": "Google and OpenAI Partner on AI Safety Research",
            "content": """
            Google has announced a new partnership with OpenAI to advance artificial intelligence safety research.
            The collaboration will focus on developing responsible AI technologies and addressing potential risks.

            Dr. Sarah Chen, Director of AI Research at Google, will lead the initiative alongside Dr. Sam Altman
            from OpenAI. The partnership aims to create new standards for AI development and deployment.

            The two organizations plan to use machine learning and deep learning techniques to build safer
            AI systems. This partnership follows recent discussions about AI regulation and ethical guidelines
            in the technology industry.

            Both companies are headquartered in Silicon Valley and have been working on similar AI projects.
            The partnership is expected to influence future AI policies and help establish industry best practices.
            """
        }

        # Create entity extractor
        extractor = create_advanced_entity_extractor()

        try:
            # Extract entities
            entities = await extractor.extract_entities_from_article(
                sample_article["id"], sample_article["title"], sample_article["content"]
            )

            print(" Extracted Entities:")
            for entity in entities:
                print(
                    "  • {0} ({1}) - Confidence: {2}".format(
                        entity.text, entity.label, entity.confidence
                    )
                )
                if entity.properties:
                    print("    Properties: {0}".format(entity.properties))

            # Extract relationships
            relationships = await extractor.extract_relationships(
                entities,
                sample_article["title"] + ". " + sample_article["content"],
                sample_article["id"],
            )

            print("\n🔗 Extracted Relationships:")
            for rel in relationships:
                print(
                    "  • {0} --[{1]}--> {2}".format(
                        rel.source_entity.text,
                        rel.relation_type,
                        rel.target_entity.text,
                    )
                )
                print("    Confidence: {0}".format(rel.confidence))
                print("    Context: {0}...".format(rel.context[:100]))

            # Get statistics
            stats = extractor.get_extraction_statistics()
            print("\n📊 Extraction Statistics:")
            for key, value in stats.items():
                print("  • {0}: {1}".format(key, value))

        except Exception as e:
            print("❌ Error in demonstration: {0}".format(e))

    # Run the example
    asyncio.run(main())
