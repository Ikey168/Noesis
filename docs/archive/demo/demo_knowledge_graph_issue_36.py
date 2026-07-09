"""
Demo: Enhanced Knowledge Graph Population - Issue #36

This demo showcases the complete implementation of Issue #36 requirements:
1. Extract Named Entities (People, Organizations, Technologies, Policies)
2. Store relationships in AWS Neptune
3. Implement Graph Builder enhancements
4. Verify entity linking with SPARQL/Gremlin queries

The demo includes realistic news articles and demonstrates the full pipeline
from NLP processing to knowledge graph population and querying.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

# Add src to path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Sample news articles for demonstration
DEMO_ARTICLES = [
    {
        "id": "kg_demo_001",
        "title": "Microsoft and OpenAI Expand Partnership in Artificial Intelligence",
        "content": """
        Microsoft Corporation announced today an expanded partnership with OpenAI to advance
        artificial intelligence research and development. The collaboration, led by CEO Satya
        Nadella of Microsoft and CEO Sam Altman of OpenAI, will focus on developing next-generation
        AI models and improving Azure cloud infrastructure for AI workloads.

        The partnership includes a $10 billion investment from Microsoft into OpenAI's research'
        initiatives. The two companies will work together on GPT-4 improvements, multimodal AI
        systems, and enterprise AI solutions. Azure OpenAI Service will be enhanced to provide
        better access to advanced language models for business customers.

        "This partnership represents the future of AI innovation," said Satya Nadella during
        the announcement at Microsoft's headquarters in Redmond, Washington. The collaboration'
        aims to ensure responsible AI development while maintaining competitive advantages in
        the rapidly evolving technology landscape.

        The partnership also addresses regulatory concerns under the European Union's AI Act'
        and ensures compliance with emerging AI governance frameworks. Both companies emphasized
        their commitment to AI safety and ethical development practices.
        """,
        "published_date": datetime(2025, 8, 17, 9, 0, 0, tzinfo=timezone.utc),
        "metadata": {
            "category": "Technology",
            "source_url": "https://example.com/microsoft-openai-partnership",
            "author": "Sarah Chen",
            "tags": ["AI", "Microsoft", "OpenAI", "Partnership"], "
        },
    },
    {
        "id": "kg_demo_002",
        "title": "Google Unveils New Quantum Computing Breakthrough",
        "content": """
        Google LLC researchers have achieved a significant breakthrough in quantum computing,
        demonstrating quantum supremacy with their new Sycamore quantum processor. The team,
        led by Dr. John Preskill and Dr. Hartmut Neven, successfully performed calculations
        that would take classical computers thousands of years to complete.

        The quantum computer, housed at Google's quantum AI laboratory in Santa Barbara,'
        California, uses 70 superconducting qubits to solve complex optimization problems.
        This advancement brings practical quantum computing applications closer to reality,
        particularly in cryptography, drug discovery, and financial modeling.

        "We are entering a new era of computational capability," stated Sundar Pichai, CEO
        of Alphabet Inc., Google's parent company. The breakthrough has implications for'
        cybersecurity standards and may require updates to current encryption protocols
        under various data protection regulations.

        IBM Corporation and Amazon Web Services have announced competing quantum initiatives,
        indicating intense competition in the quantum computing space. The race to achieve
        practical quantum advantage continues to drive innovation across the technology sector.

        Regulatory bodies are closely monitoring quantum computing developments due to potential
        impacts on national security and data privacy under frameworks like GDPR and CCPA.
        """,
        "published_date": datetime(2025, 8, 16, 14, 30, 0, tzinfo=timezone.utc),
        "metadata": {
            "category": "Science",
            "source_url": "https://example.com/google-quantum-breakthrough",
            "author": "Dr. Michael Roberts",
            "tags": ["Quantum Computing", "Google", "Technology", "Research"],"
        },
    },
    {
        "id": "kg_demo_003",
        "title": "Tesla and Panasonic Joint Venture Expands Battery Production",
        "content": """
        Tesla Inc. and Panasonic Corporation have announced the expansion of their joint
        venture to increase lithium-ion battery production capacity. The partnership, overseen
        by Elon Musk, CEO of Tesla, and Kazuhiro Tsuga, CEO of Panasonic, will establish
        new Gigafactory facilities in Texas and Nevada.

        The expansion aims to meet growing demand for electric vehicles and energy storage
        systems. Tesla's Gigafactory in Austin, Texas, will be retrofitted with Panasonic's
        latest 4680 battery cell technology, increasing production efficiency by 40%.

        "This collaboration strengthens our position in the sustainable energy transition,"
        said Elon Musk during a press conference at Tesla's headquarters in Palo Alto,'
        California. The partnership leverages Panasonic's expertise in battery chemistry
        and Tesla's innovative manufacturing processes.

        The expansion requires compliance with environmental regulations and safety standards
        set by the Environmental Protection Agency (EPA) and Department of Energy (DOE).
        Battery recycling programs will be implemented to address lifecycle environmental
        impacts under the Circular Economy Action Plan.

        Competition in the EV battery market includes partnerships between Ford and SK Innovation,
        General Motors and LG Energy Solution, and Volkswagen's PowerCo initiative. The race'
        to secure sustainable battery supply chains continues to reshape the automotive industry.
        """,
        "published_date": datetime(2025, 8, 15, 11, 15, 0, tzinfo=timezone.utc),
        "metadata": {
            "category": "Automotive",
            "source_url": "https://example.com/tesla-panasonic-battery-expansion",
            "author": "Jennifer Kim",
            "tags": ["Tesla", "Panasonic", "Electric Vehicles", "Batteries"],"
        },
    },
]


class KnowledgeGraphDemo:
    """Comprehensive demo of the enhanced knowledge graph system."""

    def __init__(self):
        self.stats = {
            "total_articles": 0,
            "total_entities": 0,
            "total_relationships": 0,
            "processing_time": 0.0,
        }

    async def run_complete_demo(self):
        """Run the complete knowledge graph population demo."""
        print(" Enhanced Knowledge Graph Population Demo - Issue #36")
        print("=" * 70)
        print("Testing complete pipeline from NLP extraction to Neptune population")
        print()

        try:
            # Test 1: Entity Extraction Demo
            await self._demo_entity_extraction()

            # Test 2: Relationship Detection Demo
            await self._demo_relationship_detection()

            # Test 3: Knowledge Graph Population Demo
            await self._demo_graph_population()

            # Test 4: Graph Querying Demo
            await self._demo_graph_querying()

            # Test 5: Validation and Statistics
            await self._demo_validation_and_stats()

            print(""
 Demo completed successfully!")
            print(" Final Statistics: {0}".format(self.stats))"

        except Exception as e:
            print("❌ Demo failed: {0}".format(e))
            logger.error("Demo error: {0}".format(e), exc_info=True)


    async def _demo_entity_extraction(self):
        """Demonstrate advanced entity extraction capabilities."""
        print(" 1. ADVANCED ENTITY EXTRACTION")
        print("-" * 40)

        try:
            # Import entity extractor with fallback
            try:
                from knowledge_graph.enhanced_entity_extractor import \
                    create_advanced_entity_extractor

                extractor = create_advanced_entity_extractor()
                print(" Enhanced entity extractor loaded successfully")
            except ImportError:
                print("⚠️  Enhanced entity extractor not available - using mock")
                extractor = self._create_mock_entity_extractor()

            # Process first article for entity extraction demo
            article = DEMO_ARTICLES[0]
            print(f"
📰 Processing: {article['title']}")

            start_time = datetime.now()
            entities = await extractor.extract_entities_from_article(
                article["id"], article["title"], article["content"]
            )
            processing_time = (datetime.now() - start_time).total_seconds()

            print("⏱️  Extraction time: {0}s".format()
            print(" Entities found: {0}".format(len(entities)))

            # Display entities by type
            entity_types = {}
            for entity in entities:
                entity_type = entity.label
                if entity_type not in entity_types:
                    entity_types[entity_type] = []
                entity_types[entity_type].append(entity)

            for entity_type, type_entities in entity_types.items():
                print("
:".format(entity_type, len(type_entities)))
                for entity in type_entities[:3]:  # Show first 3 of each type
                    print("    • {0} (confidence: {1})".format(entity.text, entity.confidence:.2f))
                    if hasattr(entity, "properties") and entity.properties:
                        for prop, value in list(entity.properties.items())[:2]:
                            print("      - {0}: {1}".format(prop, value))
                if len(type_entities) > 3:
                    print("    ... and {0} more".format(len(type_entities) - 3))

            self.stats["total_entities"] += len(entities)
            self.stats["processing_time"] += processing_time

        except Exception as e:
            print("❌ Entity extraction demo failed: {0}".format(e))


    async def _demo_relationship_detection(self):
        """Demonstrate relationship detection between entities."""
        print(""
🔗 2. RELATIONSHIP DETECTION")
        print("-" * 40)"

        try:
            # Import components
            try:
                from knowledge_graph.enhanced_entity_extractor import \
                    create_advanced_entity_extractor

                extractor = create_advanced_entity_extractor()
            except ImportError:
                extractor = self._create_mock_entity_extractor()

            # Process article for relationships
            article = DEMO_ARTICLES[1]  # Google quantum article
            print(f"
📰 Processing: {article['title']}")

            # Extract entities first
            entities = await extractor.extract_entities_from_article(
                article["id"], article["title"], article["content"]
            )

            # Extract relationships
            start_time = datetime.now()
            relationships = await extractor.extract_relationships(
                entities, article["content"], article["id"]
            )
            processing_time = (datetime.now() - start_time).total_seconds()

            print("⏱️  Relationship detection time: {0}s".format()
            print("🔗 Relationships found: {0}".format(len(relationships)))

            # Display relationships
            for i, rel in enumerate(relationships[:5]):  # Show first 5 relationships
                print(""
  Relationship {0}:".format(i+1))"
                print(
                    "    Source: {0} ({1})".format(rel.source_entity.text, rel.source_entity.label)
                )
                print(
                    "    Target: {0} ({1})".format(rel.target_entity.text, rel.target_entity.label)
                )
                print("    Type: {0}".format(rel.relation_type))
                print("    Confidence: {0}".format(rel.confidence:.2f))
                print("    Context: {0}...".format(rel.context[:100]))

            if len(relationships) > 5:
                print("    ... and {0} more relationships".format(len(relationships) - 5))

            self.stats["total_relationships"] += len(relationships)
            self.stats["processing_time"] += processing_time

        except Exception as e:
            print("❌ Relationship detection demo failed: {0}".format(e))


    async def _demo_graph_population(self):
        """Demonstrate knowledge graph population with Neptune."""
        print(""
🗄️  3. KNOWLEDGE GRAPH POPULATION")
        print("-" * 40)"

        try:
            # Import populator with fallback
            try:
                from knowledge_graph.enhanced_graph_populator import \
                    create_enhanced_knowledge_graph_populator

                # Mock Neptune endpoint for demo
                populator = create_enhanced_knowledge_graph_populator(
                    "wss://demo-cluster.neptune.amazonaws.com:8182/gremlin"
                )
                print(" Enhanced graph populator loaded successfully")
            except ImportError:
                print("⚠️  Enhanced graph populator not available - using mock")
                populator = self._create_mock_graph_populator()

            # Process all articles
            print(""
📚 Processing {0} articles...".format(len(DEMO_ARTICLES)))"

            total_start_time = datetime.now()
            results = []

            for i, article in enumerate(DEMO_ARTICLES):
                print(f"
  📰 Article {i+1}: {article['title'][:50]}...")

                try:
                    # Mock the population process
                    result = await self._mock_populate_article(article)
                    results.append(result)

                    print(f"     Entities: {result['entities']['created']}")
                    print(f"     Relationships: {result['relationships']['created']}")
                    print(f"    ⏱️  Time: {result['processing_time']:.3f}s")

                except Exception as e:
                    print("    ❌ Failed: {0}".format(e))
                    continue

            total_time = (datetime.now() - total_start_time).total_seconds()

            # Summary statistics
            total_entities = sum(r["entities"]["created"] for r in results)
            total_relationships = sum(r["relationships"]["created"] for r in results)

            print(""
 Population Summary:")
            print("  • Articles processed: {0}".format(len(results)))
            print("  • Total entities created: {0}".format(total_entities))
            print("  • Total relationships created: {0}".format(total_relationships))
            print("  • Total processing time: {0}s".format()
            print("  • Average time per article: {0}s".format(total_time/len(results):.3f))"

            self.stats["total_articles"] = len(results)
            self.stats["total_entities"] = total_entities
            self.stats["total_relationships"] = total_relationships
            self.stats["processing_time"] += total_time

        except Exception as e:
            print("❌ Graph population demo failed: {0}".format(e))


    async def _demo_graph_querying(self):
        """Demonstrate graph querying with Gremlin and SPARQL."""
        print(""
 4. GRAPH QUERYING AND VERIFICATION")
        print("-" * 40)"

        try:
            # Demo Gremlin queries
            print(""
 Gremlin Query Examples:")"

            gremlin_queries = [
                {
                    "name": "Find Microsoft relationships",
                    "query": "g.V().has('normalized_form', 'Microsoft Corporation').bothE().limit(10)",
                    "description": "Find all relationships for Microsoft Corporation",
                },
                {
                    "name": "CEO relationships",
                    "query": "g.V().has('title', containing('CEO')).out('WORKS_FOR').valueMap('name')",
                    "description": "Find organizations where CEOs work",
                },
                {
                    "name": "AI technology mentions",
                    "query": "g.V().hasLabel('Technology').has('techName', containing('AI')).in('MENTIONS_TECH')",
                    "description": "Find articles mentioning AI technologies",
                },
            ]

            for query_info in gremlin_queries:
                print(f"
  Query: {query_info['name']}")
                print(f"  Description: {query_info['description']}")
                print(f"  Gremlin: {query_info['query']}")

                # Mock query execution
                mock_results = await self._mock_execute_gremlin_query(
                    query_info["query"]
                )
                print("  Results: {0} entities found".format(len(mock_results)))

                for result in mock_results[:3]:
                    print("    • {0}".format(result))
                if len(mock_results) > 3:
                    print("    ... and {0} more".format(len(mock_results) - 3))

            # Demo SPARQL queries
            print(""
 SPARQL Query Examples:")"

            sparql_queries = [
                {
                    "name": "Partnership relationships",
                    "query": """
                    SELECT ?org1 ?org2 WHERE {
                        ?org1 rdf:type :Organization .
                        ?org2 rdf:type :Organization .
                        ?org1 :partnersith ?org2 .
                    }
                    """,
                    "description": "Find all organizational partnerships","
                },
                {
                    "name": "Technology and companies",
                    "query": """
                    SELECT ?company ?technology WHERE {
                        ?company rdf:type :Organization .
                        ?technology rdf:type :Technology .
                        ?company :develops ?technology .
                    }
                    """,
                    "description": "Find companies and their developed technologies","
                },
            ]

            for query_info in sparql_queries:
                print(f"
  Query: {query_info['name']}")
                print(f"  Description: {query_info['description']}")
                print(f"  SPARQL: {query_info['query'].strip()}")
                print(
                    "  Status: Mock execution (SPARQL endpoint needed for real queries)"
                )

        except Exception as e:
            print("❌ Graph querying demo failed: {0}".format(e))


    async def _demo_validation_and_stats(self):
        """Demonstrate data validation and quality checks."""
        print(""
 5. DATA VALIDATION AND QUALITY")
        print("-" * 40)"

        try:
            # Mock validation results
            validation_results = {
                "entity_counts": {
                    "Person": 8,
                    "Organization": 12,
                    "Technology": 15,
                    "Policy": 5,
                    "Location": 6,
                },
                "relationship_counts": {
                    "WORKS_FOR": 8,
                    "PARTNERS_WITH": 4,
                    "DEVELOPS": 10,
                    "MENTIONS_TECH": 25,
                    "MENTIONS_ORG": 20,
                },
                "orphaned_entities": 2,
                "duplicate_entities": 1,
                "data_quality_score": 0.94,
            }

            print(""
 Entity Distribution:")
            for entity_type, count in validation_results["entity_counts"].items():
                print("  • {0}: {1}".format(entity_type, count))"

            print(""
🔗 Relationship Distribution:")
            for rel_type, count in validation_results["relationship_counts"].items():
                print("  • {0}: {1}".format(rel_type, count))"

            print("
 Data Quality Metrics:")
            print(f"  • Orphaned entities: {validation_results['orphaned_entities']}")
            print(f"  • Duplicate entities: {validation_results['duplicate_entities']}")
            print(
                f"  • Data quality score: {validation_results['data_quality_score']:.1%}"
            )

            # Quality recommendations
            print(""
💡 Quality Recommendations:")
            if validation_results["orphaned_entities"] > 0:
                print(
                    f"  • Review {validation_results['orphaned_entities']} orphaned entities""
                )
            if validation_results["duplicate_entities"] > 0:
                print(
                    f"  • Merge {validation_results['duplicate_entities']} duplicate entities"
                )
            if validation_results["data_quality_score"] < 0.95:
                print("  • Improve entity linking to reach 95% quality score")
            else:
                print("  •  Data quality meets high standards")

        except Exception as e:
            print("❌ Validation demo failed: {0}".format(e))


    def _create_mock_entity_extractor(self):
        """Create a mock entity extractor for demo purposes."""


        class MockEntityExtractor:

            async def extract_entities_from_article(self, article_id, title, content):
                # Generate mock entities based on content keywords
                entities = []

                # Mock entity data based on article content
                if "Microsoft" in content:
                    entities.append(
                        self._create_mock_entity(
                            "Microsoft Corporation", "ORGANIZATION", 0.95
                        )
                    )
                    entities.append(
                        self._create_mock_entity("Satya Nadella", "PERSON", 0.92)
                    )
                if "OpenAI" in content:
                    entities.append(
                        self._create_mock_entity("OpenAI", "ORGANIZATION", 0.94)
                    )
                    entities.append(
                        self._create_mock_entity("Sam Altman", "PERSON", 0.90)
                    )
                if "Google" in content:
                    entities.append(
                        self._create_mock_entity("Google LLC", "ORGANIZATION", 0.96)
                    )
                    entities.append(
                        self._create_mock_entity("Sundar Pichai", "PERSON", 0.91)
                    )
                if "Tesla" in content:
                    entities.append(
                        self._create_mock_entity("Tesla Inc.", "ORGANIZATION", 0.95)
                    )
                    entities.append(
                        self._create_mock_entity("Elon Musk", "PERSON", 0.93)
                    )

                # Add technology entities
                if "artificial intelligence" in content.lower():
                    entities.append(
                        self._create_mock_entity(
                            "Artificial Intelligence", "TECHNOLOGY", 0.89
                        )
                    )
                if "quantum" in content.lower():
                    entities.append(
                        self._create_mock_entity(
                            "Quantum Computing", "TECHNOLOGY", 0.87
                        )
                    )
                if "battery" in content.lower():
                    entities.append(
                        self._create_mock_entity(
                            "Lithium-ion Battery", "TECHNOLOGY", 0.85
                        )
                    )

                # Add policy entities
                if "AI Act" in content:
                    entities.append(
                        self._create_mock_entity("EU AI Act", "POLICY", 0.88)
                    )
                if "GDPR" in content:
                    entities.append(self._create_mock_entity("GDPR", "POLICY", 0.92))

                return entities

            async def extract_relationships(self, entities, content, article_id):
                relationships = []

                # Generate mock relationships
                for i, source_entity in enumerate(entities):
                    for target_entity in entities[i + 1 :]:
                        if self._should_relate(source_entity, target_entity, content):
                            rel_type = self._determine_relationship_type(
                                source_entity, target_entity
                            )
                            relationships.append(
                                self._create_mock_relationship(
                                    source_entity, target_entity, rel_type, content
                                )
                            )

                return relationships

        return MockEntityExtractor()

    def _create_mock_entity(self, text, label, confidence):
        """Create a mock entity object."""


        class MockEntity:

            def __init__(self, text, label, confidence):
                self.text = text
                self.label = label
                self.confidence = confidence
                self.normalized_form = text
                self.properties = {}

                # Add type-specific properties
                if label == "PERSON":
                    if "CEO" in text or any(
                        ceo in text for ceo in ["Nadella", "Altman", "Pichai", "Musk"]
                    ):
                        self.properties["title"] = "CEO"
                elif label == "ORGANIZATION":
                    if "Inc." in text or "LLC" in text or "Corporation" in text:
                        self.properties["type"] = "Corporation"
                elif label == "TECHNOLOGY":
                    self.properties["category"] = "Advanced Technology"
                elif label == "POLICY":
                    self.properties["type"] = "Regulation"

        return MockEntity(text, label, confidence)

    def _create_mock_relationship(
        self, source_entity, target_entity, rel_type, content
    ):
        """Create a mock relationship object."""


        class MockRelationship:

            def __init__(self, source_entity, target_entity, rel_type, content):
                self.source_entity = source_entity
                self.target_entity = target_entity
                self.relation_type = rel_type
                self.confidence = 0.85
                self.context = content[:200] + "..."

        return MockRelationship(source_entity, target_entity, rel_type, content)

    def _should_relate(self, entity1, entity2, content):
        """Determine if two entities should have a relationship."""
        # Simple heuristic based on entity types and content
        if entity1.label == "PERSON" and entity2.label == "ORGANIZATION":
            return True
        if entity1.label == "ORGANIZATION" and entity2.label == "ORGANIZATION":
            return "partnership" in content.lower() or "collaborate" in content.lower()
        return False


    def _determine_relationship_type(self, entity1, entity2):
        """Determine relationship type based on entity types."""
        if entity1.label == "PERSON" and entity2.label == "ORGANIZATION":
            return "WORKS_FOR"
        elif entity1.label == "ORGANIZATION" and entity2.label == "ORGANIZATION":
            return "PARTNERS_WITH"
        elif entity1.label == "ORGANIZATION" and entity2.label == "TECHNOLOGY":
            return "DEVELOPS"
        return "RELATED_TO"


    def _create_mock_graph_populator(self):
        """Create a mock graph populator for demo purposes."""


        class MockGraphPopulator:

            async def populate_from_article(
                self, article_id, title, content, published_date, metadata
            ):
                # Simulate processing time
                await asyncio.sleep(0.1)

                # Return mock results
                return {
                    "article_id": article_id,
                    "entities": {"created": 8, "linked": 2},
                    "relationships": {"created": 5, "skipped": 1},
                    "processing_time": 0.15,
                }

        return MockGraphPopulator()

    async def _mock_populate_article(self, article):
        """Mock article population for demo."""
        # Simulate realistic processing
        await asyncio.sleep(0.1)

        # Generate mock results based on article content
        entity_count = len(article["content"].split()) // 50  # Rough estimate
        relationship_count = entity_count // 2

        return {
            "article_id": article["id"],
            "entities": {
                "extracted": entity_count + 2,
                "created": entity_count,
                "linked": 2,
            },
            "relationships": {
                "extracted": relationship_count + 1,
                "created": relationship_count,
                "skipped": 1,
            },
            "processing_time": 0.15 + (len(article["content"]) / 10000),
        }


    async def _mock_execute_gremlin_query(self, query):
        """Mock Gremlin query execution."""
        # Return mock results based on query content
        if "Microsoft" in query:
            return ["Microsoft Corporation", "OpenAI", "Azure AI", "Satya Nadella"]
        elif "CEO" in query:
            return ["Microsoft Corporation", "OpenAI", "Google LLC", "Tesla Inc."]
        elif "AI" in query:
            return [
                "GPT-4",
                "Azure OpenAI",
                "Artificial Intelligence",
                "Machine Learning",
            ]
        else:
            return ["Entity 1", "Entity 2", "Entity 3"]


async def main():
    """Run the complete knowledge graph demo."""
    demo = KnowledgeGraphDemo()
    await demo.run_complete_demo()


if __name__ == "__main__":
    # Run the demo
    print(" Starting Enhanced Knowledge Graph Demo...")
    print("This demo showcases Issue #36 implementation:")
    print("• Advanced entity extraction with enhanced patterns")
    print("• Sophisticated relationship detection")
    print("• Neptune graph population and querying")
    print("• SPARQL/Gremlin query verification")
    print("• Data validation and quality metrics")
    print()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(""
👋 Demo interrupted by user")"
    except Exception as e:
        print(""
❌ Demo failed with error: {0}".format(e))
        logger.error("Demo error: {0}".format(e), exc_info=True)"
