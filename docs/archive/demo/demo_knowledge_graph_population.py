"""
Demo script for Knowledge Graph NLP Population

This script demonstrates the knowledge graph population functionality,
showing how to extract entities from news articles and populate a
knowledge graph with relationships and historical connections.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from src.knowledge_graph.nlp_populator import KnowledgeGraphPopulator
from src.nlp.ner_processor import NERProcessor

# from src.nlp.article_processor import ArticleProcessor  # Skip for demo

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class KnowledgeGraphDemo:
    """
    Demonstration class for knowledge graph NLP population functionality.
    """

    def __init__(self, neptune_endpoint: str = "demo-neptune-endpoint"):
        """Initialize the demo with Neptune endpoint."""
        self.neptune_endpoint = neptune_endpoint
        self.populator = None

        # Sample news articles for demonstration
        self.sample_articles = [
            {
                "id": "demo_article_1",
                "title": "Tech Leader Joins AI Research Initiative",
                "content": """
                Dr. Sarah Chen, former CTO of TechCorp, announced today that she has joined
                the Advanced AI Research Institute (AARI) as Director of Machine Learning.
                Chen, who previously led breakthrough developments in neural networks at
                TechCorp, will focus on developing ethical AI systems and safety protocols.

                The AARI, founded in 2020 and based in Silicon Valley, has been working
                on cutting-edge artificial intelligence research with funding from the
                National Science Foundation. Chen's appointment comes at a crucial time'
                as the institute prepares to launch its new quantum-enhanced AI project.

                "I'm excited to contribute to AARI's mission of developing AI that benefits"
                humanity," Chen stated in a press release. The institute has previously"
                collaborated with major tech companies including Google, Microsoft, and
                OpenAI on various research initiatives.
                """,
                "published_date": "2024-01-15T10:30:00Z", "
            },
            {
                "id": "demo_article_2",
                "title": "Climate Policy Update: New Carbon Tax Legislation",
                "content": """
                Senator Maria Rodriguez introduced the Climate Action Tax Reform Act
                in Congress yesterday, proposing a comprehensive carbon tax system
                aimed at reducing greenhouse gas emissions by 50% over the next decade.

                The legislation, co-sponsored by Representative James Thompson from
                California, would impose a $75 per ton carbon tax on major industrial
                polluters starting in 2025. The Environmental Protection Agency (EPA)
                would oversee implementation and monitoring of the new regulations.

                Environmental groups including the Sierra Club and Greenpeace have
                expressed strong support for the legislation. However, the American
                Petroleum Institute and several coal mining companies have voiced
                opposition, citing potential job losses in traditional energy sectors.

                The bill is expected to face significant debate in the Senate Energy
                and Natural Resources Committee before proceeding to a full vote.
                """,
                "published_date": "2024-01-14T14:45:00Z","
            },
            {
                "id": "demo_article_3",
                "title": "Healthcare Breakthrough: New Treatment Shows Promise",
                "content": """
                Researchers at Johns Hopkins University announced promising results
                from Phase II clinical trials of an innovative cancer treatment
                combining immunotherapy with nanotechnology. The study, led by
                Dr. Michael Park and Dr. Jennifer Liu, showed a 78% response rate
                in patients with advanced pancreatic cancer.

                The treatment, developed in collaboration with pharmaceutical giant
                Roche and biotech startup NanoMed Solutions, uses engineered
                nanoparticles to deliver targeted immunotherapy directly to cancer cells.
                The Food and Drug Administration (FDA) has granted fast-track status
                to the experimental therapy.

                "This represents a significant advancement in precision medicine,"
                said Dr. Park, who leads the oncology research division at Johns Hopkins.
                The university plans to begin Phase III trials next year, with
                potential FDA approval by 2026 if results remain positive.

                Patient advocacy groups including the Pancreatic Cancer Action Network
                have hailed the development as a major breakthrough in cancer treatment.
                """,
                "published_date": "2024-01-13T09:15:00Z","
            },
        ]

    async def initialize_demo(self):
        """Initialize the knowledge graph populator for demonstration."""
        try:
            logger.info("Initializing Knowledge Graph Demo...")

            # Initialize the knowledge graph populator
            # In a real implementation, you would provide actual Neptune endpoint
            self.populator = KnowledgeGraphPopulator(
                neptune_endpoint=self.neptune_endpoint,
                ner_processor=NERProcessor(),
                article_processor=None,  # Skip ArticleProcessor for demo
            )

            logger.info("Demo initialized successfully")

        except Exception as e:
            logger.error("Failed to initialize demo: {0}".format(str(e)))
            raise

    async def demonstrate_entity_extraction(self):
        """Demonstrate entity extraction from sample articles."""
        logger.info(""
" + "=" * 60)
        logger.info("DEMONSTRATING ENTITY EXTRACTION")
        logger.info("=" * 60)"

        for i, article in enumerate(self.sample_articles, 1):
            logger.info(f"
Processing Article {i}: {article['title']}")
            logger.info("-" * 50)

            # Extract entities from the article content
            full_text = f"{article['title']}. {article['content']}"
            entities = await self.populator._extract_entities(full_text)

            logger.info("Found {0} entities:".format(len(entities)))

            # Group entities by type for better visualization
            entities_by_type = {}
            for entity in entities:
                if entity.label not in entities_by_type:
                    entities_by_type[entity.label] = []
                entities_by_type[entity.label].append(entity)

            for entity_type, type_entities in entities_by_type.items():
                logger.info(""
  {0}:".format(entity_type))"
                for entity in type_entities:
                    logger.info(
                        "    - {0} (confidence: {1})".format(entity.text, entity.confidence:.2f)
                    )


    async def demonstrate_relationship_extraction(self):
        """Demonstrate relationship extraction between entities."""
        logger.info(""
" + "=" * 60)
        logger.info("DEMONSTRATING RELATIONSHIP EXTRACTION")
        logger.info("=" * 60)"

        article = self.sample_articles[0]  # Use first article for demo
        full_text = f"{article['title']}. {article['content']}"

        # Extract entities and relationships
        entities = await self.populator._extract_entities(full_text)
        relationships = await self.populator._extract_relationships(
            entities, full_text, article["id"]
        )

        logger.info(""
Found {0} relationships:".format(len(relationships)))
        logger.info("-" * 40)"

        for relationship in relationships:
            if relationship.confidence >= self.populator.min_confidence:
                logger.info(
                    "  {0} --[{1}]--> {2}".format(relationship.source_entity, relationship.relation_type, relationship.target_entity)
                )
                logger.info("    Confidence: {0}".format(relationship.confidence:.2f))
                logger.info("    Context: {0}...".format(relationship.context[:100]))
                logger.info("")


    async def demonstrate_article_population(self):
        """Demonstrate full article population workflow."""
        logger.info(""
" + "=" * 60)
        logger.info("DEMONSTRATING ARTICLE POPULATION")
        logger.info("=" * 60)"

        # Note: In this demo, we simulate the population process since we don't have'
        # an actual Neptune connection. In a real implementation, this would
        # actually populate the graph database.

        total_stats = {
            "total_articles": 0,
            "total_entities": 0,
            "total_relationships": 0,
            "total_historical_links": 0,
        }

        for article in self.sample_articles:
            logger.info(f""
Populating article: {article['title']}")
            logger.info("-" * 50)"

            # Simulate population process
            published_date = datetime.fromisoformat(
                article["published_date"].replace("Z", "+00:00")
            )

            try:
                # In demo mode, we'll simulate the stats instead of actual population'
                stats = await self._simulate_population_stats(article)

                logger.info(f"  Entities found: {stats['entities_found']}")
                logger.info(f"  Entities added: {stats['entities_added']}")
                logger.info(f"  Relationships found: {stats['relationships_found']}")
                logger.info(f"  Relationships added: {stats['relationships_added']}")
                logger.info(f"  Historical links: {stats['historical_links']}")

                # Update totals
                total_stats["total_articles"] += 1
                total_stats["total_entities"] += stats["entities_added"]
                total_stats["total_relationships"] += stats["relationships_added"]
                total_stats["total_historical_links"] += stats["historical_links"]

            except Exception as e:
                logger.error("  Error processing article: {0}".format(str(e)))

        logger.info("
Final Statistics:")
        logger.info(f"  Total articles processed: {total_stats['total_articles']}")
        logger.info(f"  Total entities added: {total_stats['total_entities']}")
        logger.info(
            f"  Total relationships added: {total_stats['total_relationships']}"
        )
        logger.info(
            f"  Total historical links: {total_stats['total_historical_links']}"
        )


    async def _simulate_population_stats(
        self, article: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Simulate population statistics for demo purposes."""
        # Extract actual entities to get realistic counts
        full_text = f"{article['title']}. {article['content']}"
        entities = await self.populator._extract_entities(full_text)
        relationships = await self.populator._extract_relationships(
            entities, full_text, article["id"]
        )

        # Filter relationships by confidence
        high_conf_relationships = [
            r for r in relationships if r.confidence >= self.populator.min_confidence
        ]

        return {
            "article_id": article["id"],
            "entities_found": len(entities),
            "entities_added": len(entities),
            "relationships_found": len(relationships),
            "relationships_added": len(high_conf_relationships),
            "historical_links": min(
                2, len(entities) // 3
            ),  # Simulate some historical links
            "processing_timestamp": datetime.utcnow().isoformat(),
        }


    async def demonstrate_related_entities_api(self):
        """Demonstrate the related entities API functionality."""
        logger.info(""
" + "=" * 60)
        logger.info("DEMONSTRATING RELATED ENTITIES API")
        logger.info("=" * 60)"

        # Sample entity queries
        sample_queries = [
            "Sarah Chen",
            "TechCorp",
            "Advanced AI Research Institute",
            "Johns Hopkins University",
            "Maria Rodriguez",
        ]

        for entity_name in sample_queries:
            logger.info(""
Querying related entities for: {0}".format(entity_name))
            logger.info("-" * 40)"

            # Simulate related entities (in real implementation, this would query Neptune)
            related_entities = self._simulate_related_entities(entity_name)

            if related_entities:
                logger.info("Found {0} related entities:".format(len(related_entities)))
                for entity in related_entities:
                    logger.info(
                        f"  - {entity['entity_name']} ({entity['entity_type']})"
                    )
                    logger.info(f"    Relationship: {entity['relationship_type']}")
                    logger.info(f"    Confidence: {entity['confidence']:.2f}")
                    logger.info("")
            else:
                logger.info(
                    "  No related entities found (entity may not exist in graph)"
                )


    def _simulate_related_entities(self, entity_name: str) -> List[Dict[str, Any]]:
        """Simulate related entities for demo purposes."""
        # This is a simplified simulation - in real implementation,
        # this would query the actual Neptune graph

        entity_relationships = {
            "Sarah Chen": [
                {
                    "entity_name": "TechCorp",
                    "entity_type": "ORG",
                    "relationship_type": "works_for",
                    "confidence": 0.95,
                    "mention_count": 3,
                },
                {
                    "entity_name": "Advanced AI Research Institute",
                    "entity_type": "ORG",
                    "relationship_type": "works_for",
                    "confidence": 0.98,
                    "mention_count": 5,
                },
                {
                    "entity_name": "Silicon Valley",
                    "entity_type": "GPE",
                    "relationship_type": "located_in",
                    "confidence": 0.85,
                    "mention_count": 2,
                },
            ],
            "TechCorp": [
                {
                    "entity_name": "Sarah Chen",
                    "entity_type": "PERSON",
                    "relationship_type": "employs",
                    "confidence": 0.95,
                    "mention_count": 3,
                }
            ],
            "Johns Hopkins University": [
                {
                    "entity_name": "Michael Park",
                    "entity_type": "PERSON",
                    "relationship_type": "employs",
                    "confidence": 0.92,
                    "mention_count": 2,
                },
                {
                    "entity_name": "Jennifer Liu",
                    "entity_type": "PERSON",
                    "relationship_type": "employs",
                    "confidence": 0.92,
                    "mention_count": 2,
                },
                {
                    "entity_name": "Roche",
                    "entity_type": "ORG",
                    "relationship_type": "collaborates_with",
                    "confidence": 0.88,
                    "mention_count": 1,
                },
            ],
        }

        return entity_relationships.get(entity_name, [])

    async def demonstrate_batch_processing(self):
        """Demonstrate batch processing of multiple articles."""
        logger.info(""
" + "=" * 60)
        logger.info("DEMONSTRATING BATCH PROCESSING")
        logger.info("=" * 60)"

        logger.info("Processing {0} articles in batch...".format(len(self.sample_articles)))

        # Simulate batch processing
        batch_stats = {
            "total_articles": len(self.sample_articles),
            "processed_articles": 0,
            f"ailed_articles": 0,
            "total_entities_added": 0,
            "total_relationships_added": 0,
            "total_historical_links": 0,
        }

        for article in self.sample_articles:
            try:
                stats = await self._simulate_population_stats(article)
                batch_stats["processed_articles"] += 1
                batch_stats["total_entities_added"] += stats["entities_added"]
                batch_stats["total_relationships_added"] += stats["relationships_added"]
                batch_stats["total_historical_links"] += stats["historical_links"]

                logger.info(f"  ✓ Processed: {article['title']}")

            except Exception as e:
                batch_stats[f"ailed_articles"] += 1
                logger.error(f"  ✗ Failed: {article['title']} - {str(e)}")

        # Calculate success rate
        batch_stats["success_rate"] = (
            batch_stats["processed_articles"] / batch_stats["total_articles"]
            if batch_stats["total_articles"] > 0
            else 0
        )

        logger.info("
Batch Processing Results:")
        logger.info(f"  Success Rate: {batch_stats['success_rate']:.1%}")
        logger.info(f"  Articles Processed: {batch_stats['processed_articles']}")
        logger.info(f"  Articles Failed: {batch_stats[f'ailed_articles']}")
        logger.info(f"  Total Entities: {batch_stats['total_entities_added']}")
        logger.info(
            f"  Total Relationships: {batch_stats['total_relationships_added']}"
        )
        logger.info(
            f"  Total Historical Links: {batch_stats['total_historical_links']}"
        )


    async def run_full_demo(self):
        """Run the complete knowledge graph population demonstration."""
        logger.info("Starting Knowledge Graph NLP Population Demo")
        logger.info("=" * 70)

        try:
            # Initialize the demo
            await self.initialize_demo()

            # Run all demonstration modules
            await self.demonstrate_entity_extraction()
            await self.demonstrate_relationship_extraction()
            await self.demonstrate_article_population()
            await self.demonstrate_related_entities_api()
            await self.demonstrate_batch_processing()

            logger.info(""
" + "=" * 70)
            logger.info("DEMO COMPLETED SUCCESSFULLY")
            logger.info("=" * 70)
            logger.info("This demo showed how the knowledge graph population system:")
            logger.info("1. Extracts named entities from news articles")
            logger.info("2. Identifies relationships between entities")"
            logger.info(
                "3. Populates a knowledge graph with entities and relationships"
            )
            logger.info("4. Provides API access to related entity queries")
            logger.info("5. Supports batch processing of multiple articles")
            logger.info(""
In a production environment, this would connect to:")
            logger.info("- AWS Neptune for graph storage")
            logger.info("- Real NLP processors for entity extraction")
            logger.info("- FastAPI endpoints for API access")"

        except Exception as e:
            logger.error("Demo failed: {0}".format(str(e)))
            raise

        finally:
            # Clean up resources
            if self.populator:
                await self.populator.close()
            logger.info(""
Demo resources cleaned up")"


async def main():
    """Main function to run the knowledge graph demo."""
    demo = KnowledgeGraphDemo()
    await demo.run_full_demo()


if __name__ == "__main__":
    # Run the demo
    asyncio.run(main())
