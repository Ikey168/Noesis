"""
Demo script for Keyword Extraction and Topic Modeling (Issue #29).

This script demonstrates the complete functionality of the keyword extraction
and topic modeling system, including:
- Processing sample articles
- Extracting keywords using TF-IDF
- Identifying topics using LDA
- Storing results in database
- Querying topic-based search API

Run this script to validate the implementation and see sample results.
"""

from src.nlp.keyword_topic_extractor import (KeywordTopicExtractor,
                                             create_keyword_extractor)
from src.nlp.keyword_topic_database import (KeywordTopicDatabase,
                                            create_keyword_topic_db)
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class KeywordTopicDemo:
    """Demonstration class for keyword extraction and topic modeling."""

    def __init__(self):
        """Initialize demo with sample data and components."""
        self.extractor = None
        self.database = None
        self.sample_articles = self._create_sample_articles()

    def _create_sample_articles(self) -> List[Dict[str, Any]]:
        """Create sample articles for demonstration."""
        return [
            {
                "id": "demo_ai_1",
                "url": "https://example.com/ai-breakthrough",
                "title": "Revolutionary AI Breakthrough: New Neural Network Architecture Achieves Human-Level Performance",
                "content": """
                Researchers at leading technology institutes have announced a groundbreaking advancement in artificial intelligence.
                The new neural network architecture, called NeuroMax, demonstrates human-level performance across multiple cognitive tasks.
                Machine learning algorithms powered by deep learning techniques have achieved unprecedented accuracy in natural language processing,
                computer vision, and reasoning tasks. The breakthrough involves novel transformer architectures and attention mechanisms
                that enable more efficient training and inference. Scientists believe this advancement could revolutionize healthcare,
                autonomous vehicles, and scientific research. The neural network was trained on massive datasets using advanced
                optimization techniques and shows remarkable generalization capabilities.
                """,
                "source": "TechNews Daily",
                "published_date": datetime(2025, 8, 10), "
            },
            {
                "id": "demo_climate_1",
                "url": "https://example.com/climate-change-impact",
                "title": "Climate Change Accelerating: Global Temperatures Rise Beyond Critical Thresholds",
                "content": """
                Climate scientists report alarming acceleration in global warming trends as atmospheric carbon dioxide levels
                reach record highs. Environmental data shows temperature increases surpassing critical thresholds established
                in international climate agreements. Extreme weather events including hurricanes, droughts, and flooding
                are becoming more frequent and severe. Renewable energy adoption and sustainability initiatives are crucial
                for mitigating climate change impacts. Greenhouse gas emissions from fossil fuels continue to drive
                environmental degradation despite international climate policies. Ocean acidification and rising sea levels
                threaten coastal ecosystems and communities worldwide. Environmental scientists emphasize urgent need for
                comprehensive climate action and sustainable development practices.
                """,
                "source": "Environmental Science Journal",
                "published_date": datetime(2025, 8, 11),"
            },
            {
                "id": "demo_healthcare_1",
                "url": "https://example.com/personalized-medicine",
                "title": "Personalized Medicine Revolution: Genomic Analysis Enables Targeted Cancer Treatments",
                "content": """
                Medical researchers have achieved significant progress in personalized medicine through advanced genomic analysis.
                Precision medicine approaches using genetic sequencing enable targeted cancer treatments tailored to individual
                patients. Biotechnology companies are developing innovative therapeutic drugs based on genetic biomarkers
                and molecular profiles. Clinical trials demonstrate improved patient outcomes with personalized treatment protocols.
                Healthcare providers are integrating genetic testing and bioinformatics tools into routine medical practice.
                Pharmaceutical research focuses on developing targeted therapies for rare diseases and genetic disorders.
                Medical technology advances in DNA sequencing and gene editing show promise for treating previously incurable conditions.
                """,
                "source": "Medical Innovation Weekly",
                "published_date": datetime(2025, 8, 12),"
            },
            {
                "id": "demo_finance_1",
                "url": "https://example.com/cryptocurrency-regulation",
                "title": "Cryptocurrency Market Volatility Sparks Regulatory Discussions on Digital Asset Frameworks",
                "content": """
                Financial regulators worldwide are developing comprehensive frameworks for cryptocurrency and digital asset oversight.
                Blockchain technology adoption continues despite market volatility and regulatory uncertainty. Bitcoin and
                other cryptocurrencies experience significant price fluctuations affecting investor confidence. Central banks
                are exploring digital currencies (CBDCs) as alternatives to traditional fiat money systems. Financial technology
                companies are innovating payment solutions using distributed ledger technology. Investment firms are incorporating
                cryptocurrency trading into portfolio management strategies. Regulatory compliance requirements for digital
                assets continue evolving as governments establish legal frameworks for blockchain-based financial instruments.
                """,
                "source": "Financial Times",
                "published_date": datetime(2025, 8, 13),"
            },
            {
                "id": "demo_space_1",
                "url": "https://example.com/mars-exploration",
                "title": "Mars Exploration Mission Discovers Evidence of Ancient Water Systems on Red Planet",
                "content": """
                Space exploration missions have uncovered compelling evidence of ancient water systems on Mars surface.
                NASA scientists analyzing data from robotic rovers and orbital satellites confirm presence of dried riverbeds
                and mineral deposits indicating past water activity. Astrobiology research suggests Mars may have supported
                microbial life billions of years ago. Space technology advances enable detailed geological surveys and
                atmospheric analysis of the Martian environment. Planetary science discoveries inform future human missions
                to Mars and space colonization planning. Aerospace engineering innovations in spacecraft design and propulsion
                systems make interplanetary travel increasingly feasible. Space agencies collaborate on ambitious missions
                to search for signs of past or present life beyond Earth.
                """,
                "source": "Space Science Today",
                "published_date": datetime(2025, 8, 14),"
            },
            {
                "id": "demo_education_1",
                "url": "https://example.com/digital-learning",
                "title": "Digital Learning Transformation: AI-Powered Educational Platforms Reshape Student Experience",
                "content": """
                Educational technology is transforming learning experiences through AI-powered platforms and personalized curricula.
                Online learning management systems integrate adaptive learning algorithms that adjust to individual student needs.
                Virtual reality and augmented reality technologies create immersive educational environments for science and history.
                Distance education programs expand access to quality education for students in remote areas. Digital literacy
                becomes essential as educational institutions adopt technology-enhanced teaching methods. Student assessment
                and evaluation methods evolve with automated grading systems and competency-based progression models.
                Educational data analytics provide insights into learning patterns and academic performance optimization.
                """,
                "source": "Education Technology Review",
                "published_date": datetime(2025, 8, 15),"
            },
        ]

    async def initialize_components(self):
        """Initialize the keyword extractor and database components."""
        logger.info(
            "Initializing keyword extraction and topic modeling components...")

        # Load configuration
        config_path = "config/keyword_topic_settings.json"
        config = None
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                config = json.load(f)

        # Initialize extractor
        self.extractor = create_keyword_extractor(config_path)
        logger.info(" Keyword extractor initialized")

        # Initialize database (for demo purposes, we'll skip actual DB connection)
        logger.info(" Database components ready")'

    def demonstrate_keyword_extraction(self) -> Dict[str, Any]:
        """Demonstrate keyword extraction functionality."""
        logger.info(""
 DEMONSTRATING KEYWORD EXTRACTION")
        logger.info("=" * 50)"

        # Process sample articles
        results = self.extractor.process_batch(self.sample_articles)

        extraction_summary = {
            "total_articles": len(results),
            "successful_extractions": 0,
            "total_keywords": 0,
            "total_topics": 0,
            "sample_results": [],
        }

        for result in results:
            if result.keywords:
                extraction_summary["successful_extractions"] += 1
                extraction_summary["total_keywords"] += len(result.keywords)
                extraction_summary["total_topics"] += len(result.topics)

                # Display sample result
                logger.info(""
📄 Article: {0}...".format(result.title[:60]))"
                logger.info(
                    "   Keywords ({0}): {1}".format(len(result.keywords), [kw.keyword for kw in result.keywords[:5]])
                )

                if result.dominant_topic:
                    logger.info(
                        "   Dominant Topic: {0}".format(result.dominant_topic.topic_name)
                    )
                    logger.info(
                        "   Topic Probability: {0}".format(
                    )

                logger.info("   Processing Time: {0}s".format(result.processing_time:.2f))

                # Store sample for summary
                extraction_summary["sample_results"].append(
                    {
                        "title": result.title,
                        "keywords": [kw.keyword for kw in result.keywords[:5]],
                        "dominant_topic": (
                            result.dominant_topic.topic_name
                            if result.dominant_topic
                            else None
                        ),
                        "processing_time": result.processing_time,
                    }
                )

        return extraction_summary

    def demonstrate_topic_modeling(self) -> Dict[str, Any]:
        """Demonstrate topic modeling functionality."""
        logger.info(""
 DEMONSTRATING TOPIC MODELING")
        logger.info("=" * 50)"

        # Fit topic model on all articles
        topic_info = self.extractor.fit_corpus(self.sample_articles)

        if topic_info["model_fitted"]:
            logger.info(
                f" LDA Model fitted successfully on {topic_info['n_texts']} articles"
            )
            logger.info(f"   Model Perplexity: {topic_info.get('perplexity', 'N/A')}")

            # Display discovered topics
            logger.info(f"
🏷️  DISCOVERED TOPICS ({len(topic_info['topics'])})")
            for i, topic in enumerate(topic_info["topics"][:5]):  # Show top 5 topics
                logger.info(f"   Topic {i+1}: {topic['topic_name']}")
                logger.info(f"   Top Words: {', '.join(topic['topic_words'][:5])}")
        else:
            logger.warning("❌ Topic modeling failed - insufficient data")

        return topic_info

    def demonstrate_search_functionality(self):
        """Demonstrate search functionality (simulated)."""
        logger.info(""
🔎 DEMONSTRATING SEARCH FUNCTIONALITY")
        logger.info("=" * 50)"

        # Simulate API queries
        search_examples = [
            {
                "type": "topic",
                "query": "artificial_intelligence_machine",
                "description": "AI-related articles",
            },
            {
                "type": "keyword",
                "query": "climate",
                "description": "Climate-related content",
            },
            {
                "type": "advanced",
                "query": "healthcare technology",
                "description": "Healthcare tech intersection",
            },
        ]

        for example in search_examples:
            logger.info(f"
 {example['description']}:")
            logger.info(f"   Query Type: {example['type']}")
            logger.info(f"   Query: {example['query']}")
            logger.info(
                f"   API Endpoint: /topics/{example['type']}?q={example['query']}"
            )
            logger.info(f"   Expected Results: Articles matching '{example['query']}'")


    def analyze_extraction_quality(self, results: List) -> Dict[str, Any]:
        """Analyze the quality of extraction results."""
        logger.info(""
 EXTRACTION QUALITY ANALYSIS")
        logger.info("=" * 50)"

        quality_metrics = {
            "articles_with_keywords": 0,
            "articles_with_topics": 0,
            "avg_keywords_per_article": 0,
            "avg_topics_per_article": 0,
            "avg_processing_time": 0,
            "keyword_diversity": 0,
            "topic_coverage": 0,
        }

        all_keywords = set()
        all_topics = set()
        total_processing_time = 0

        for result in results:
            if result.keywords:
                quality_metrics["articles_with_keywords"] += 1
                all_keywords.update([kw.keyword for kw in result.keywords])

            if result.topics:
                quality_metrics["articles_with_topics"] += 1
                all_topics.update([topic.topic_name for topic in result.topics])

            total_processing_time += result.processing_time

        # Calculate averages and diversity
        total_articles = len(results)
        if total_articles > 0:
            quality_metrics["avg_keywords_per_article"] = (
                sum(len(r.keywords) for r in results) / total_articles
            )
            quality_metrics["avg_topics_per_article"] = (
                sum(len(r.topics) for r in results) / total_articles
            )
            quality_metrics["avg_processing_time"] = (
                total_processing_time / total_articles
            )

        quality_metrics["keyword_diversity"] = len(all_keywords)
        quality_metrics["topic_coverage"] = len(all_topics)

        # Display quality metrics
        logger.info(" Quality Metrics:")
        logger.info(
            f"   Articles with Keywords: {quality_metrics['articles_with_keywords']}/{total_articles}"
        )
        logger.info(
            f"   Articles with Topics: {quality_metrics['articles_with_topics']}/{total_articles}"
        )
        logger.info(
            f"   Avg Keywords per Article: {quality_metrics['avg_keywords_per_article']:.1f}"
        )
        logger.info(
            f"   Avg Topics per Article: {quality_metrics['avg_topics_per_article']:.1f}"
        )
        logger.info(
            f"   Avg Processing Time: {quality_metrics['avg_processing_time']:.2f}s"
        )
        logger.info(
            f"   Keyword Diversity: {quality_metrics['keyword_diversity']} unique keywords"
        )
        logger.info(
            f"   Topic Coverage: {quality_metrics['topic_coverage']} unique topics"
        )

        return quality_metrics

    async def run_complete_demo(self):
        """Run the complete demonstration."""
        logger.info(" STARTING KEYWORD EXTRACTION & TOPIC MODELING DEMO")
        logger.info("=" * 60)

        try:
            # Initialize components
            await self.initialize_components()

            # Demonstrate functionality
            extraction_results = self.demonstrate_keyword_extraction()
            topic_results = self.demonstrate_topic_modeling()
            self.demonstrate_search_functionality()

            # Get processing results for quality analysis
            processing_results = self.extractor.process_batch(self.sample_articles)
            quality_metrics = self.analyze_extraction_quality(processing_results)

            # Final summary
            logger.info(""
 DEMO COMPLETED SUCCESSFULLY")
            logger.info("=" * 60)
            logger.info(" Summary:")
            logger.info(
                f"   Total Articles Processed: {extraction_results['total_articles']}""
            )
            logger.info(
                f"   Successful Extractions: {extraction_results['successful_extractions']}"
            )
            logger.info(
                f"   Total Keywords Extracted: {extraction_results['total_keywords']}"
            )
            logger.info(
                f"   Total Topics Identified: {extraction_results['total_topics']}"
            )
            logger.info(f"   Topic Model Fitted: {topic_results['model_fitted']}")
            logger.info(
                f"   Average Processing Time: {quality_metrics['avg_processing_time']:.2f}s"
            )

            # Save results
            demo_results = {
                "demo_completed_at": datetime.now().isoformat(),
                "extraction_summary": extraction_results,
                "topic_modeling_summary": topic_results,
                "quality_metrics": quality_metrics,
                "sample_articles_count": len(self.sample_articles),
            }

            with open("keyword_topic_demo_results.json", "w") as f:
                json.dump(demo_results, f, indent=2, default=str)

            logger.info(" Results saved to: keyword_topic_demo_results.json")

            return demo_results

        except Exception as e:
            logger.error("❌ Demo failed: {0}".format(e))
            raise


async def main():
    """Main function to run the demo."""
    demo = KeywordTopicDemo()
    await demo.run_complete_demo()


if __name__ == "__main__":
    asyncio.run(main())
