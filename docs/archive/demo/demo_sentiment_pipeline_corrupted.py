"""
Demo script for Sentiment Analysis Pipeline functionality.
Showcases sentiment analysis across news articles with various analysis     async          async def analyze_single_article(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze sentiment for a single article."""
        logger.info(f"[News] Analyzing: '{article['title']}'")
        logger.info(f"[Date] Published: {article['published_at']}")
        logger.info(f"[Topic] Topic: {article['topic']}")
        logger.info(f"[Content] Content length: {len(article['content'])} characters")

        # Analyze title sentiment
        title_result = self.analyzer.analyze(article["title"])ze sentiment for a single article."""
        logger.info(f"[News] Analyzing: '{article['title']}'")
        logger.info(f"[Date] Published: {article['published_at']}")
        logger.info(f"[Topic]  Topic: {article['topic']}")
        logger.info(f"[Content] Content length: {len(article['content'])} characters")

        # Analyze title sentiment
        title_result = self.analyzer.analyze(article["title"])yze_single_article(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze sentiment for a single article."""
        logger.info(f"[News] Analyzing: '{article['title']}'")
        logger.info(f"[Date] Published: {article['published_at']}")
        logger.info(f"[Topic]  Topic: {article['topic']}")
        logger.info(f"[Content] Content length: {len(article['content'])} characters")

        # Analyze title sentiment
        title_result = self.analyzer.analyze(article["title"])
"""

from src.nlp.sentiment_analysis import create_analyzer
from src.nlp.article_processor import ArticleProcessor
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
SAMPLE_ARTICLES = [
    {
        "id": "sentiment_tech_1",
        "title": "Apple Reports Record-Breaking Q4 Earnings",
        "content": """
        Apple Inc. announced outstanding quarterly results today, exceeding analyst expectations
        with revenue of $94.8 billion. The company's innovative product lineup, including the
        iPhone 15 and new MacBook Pro models, drove exceptional growth across all segments.
        CEO Tim Cook expressed enthusiasm about the future, highlighting strong customer
        satisfaction and expanding market share. The stock price surged 8% in after-hours
        trading, reflecting investor confidence in Apple's continued success and strategic vision.
        """,
        "url": "https://example.com/apple-earnings",
        "source": "TechNews Today",
        "published_at": "2024-10-30T16:0:00Z",
        "topic": "Technology",
    },
    {
        "id": "sentiment_market_1",
        "title": "Global Markets Face Uncertainty Amid Economic Concerns",
        "content": """
        Financial markets experienced significant volatility today as investors grappled with
        mounting economic uncertainties. The Dow Jones fell 2.3%, while the S&P 500 declined
        1.8% amid concerns about inflation and potential recession risks. Analysts warn that
        the current market conditions reflect deep-seated worries about global economic
        stability. Trading volumes spiked as investors rushed to safer assets, causing
        widespread selling pressure across multiple sectors.
        """,
        "url": "https://example.com/market-uncertainty",
        "source": "Financial Times",
        "published_at": "2024-10-30T14:30:00Z",
        "topic": "Finance",
    },
    {
        "id": "sentiment_health_1",
        "title": "Breakthrough Medical Research Offers Hope for Cancer Patients",
        "content": """
        Researchers at Johns Hopkins University have achieved a remarkable breakthrough in
        cancer treatment, developing a new immunotherapy approach that shows promising results
        in clinical trials. The innovative treatment has demonstrated a 78% success rate in
        early-stage trials, offering new hope to thousands of patients worldwide. Dr. Sarah
        Chen, lead researcher, expressed optimism about the potential to revolutionize cancer
        care. The medical community has responded with excitement and support for this
        groundbreaking advancement.
        """,
        "url": "https://example.com/cancer-breakthrough",
        "source": "Medical Journal Today",
        "published_at": "2024-10-30T12:0:00Z",
        "topic": "Healthcare",
    },
    {
        "id": "sentiment_climate_1",
        "title": "Climate Change Report Reveals Alarming Temperature Trends",
        "content": """
        A disturbing new climate report published by the IPCC reveals accelerating global
        temperature increases, with 2024 projected to be the hottest year on record. The
        data shows concerning trends in ice cap melting, rising sea levels, and extreme
        weather patterns. Scientists warn that without immediate action, the consequences
        could be catastrophic for future generations. Environmental groups are calling for
        urgent policy changes to address this critical crisis facing humanity.
        """,
        "url": "https://example.com/climate-report",
        "source": "Environmental Science Daily",
        "published_at": "2024-10-30T10:15:00Z",
        "topic": "Environment",
    },
    {
        "id": "sentiment_neutral_1",
        "title": "New Transportation Infrastructure Project Announced",
        "content": """
        The Department of Transportation announced plans for a new infrastructure project
        that will upgrade transportation networks across three states. The initiative
        includes road improvements, bridge maintenance, and public transit enhancements.
        The project timeline spans five years with an estimated budget of $2.8 billion.
        Officials provided detailed specifications and implementation phases during today's
        press conference. Construction is scheduled to begin in the first quarter of 2025.
        """,
        "url": "https://example.com/infrastructure-project",
        "source": "Government News",
        "published_at": "2024-10-30T09:0:00Z",
        "topic": "Infrastructure",
    }
]


class SentimentPipelineDemo:
    """Demonstrates sentiment analysis pipeline functionality."""

    def __init__(self):
        self.analyzer = None
        self.results = []

    async def initialize(self):
        """Initialize the sentiment analyzer."""
        logger.info(" Initializing Sentiment Analysis Pipeline Demo...")
        logger.info("=" * 60)

        try:
            self.analyzer = create_analyzer()
            logger.info(
                " Sentiment analyzer initialized with model: {0}".format(
                    self.analyzer.model_name)
            )
            return True
        except Exception as e:
            logger.error(
                "[ERROR] Failed to initialize sentiment analyzer: {0}".format(e))
            return False

    async def analyze_single_article(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze sentiment for a single article."""
        logger.info(f""
[News] Analyzing: '{article['title'}}'")
        logger.info(f"[Date] Published: {article['published_at'}})"
        logger.info(f"[Topic]  Topic: {article['topic'}})"
        logger.info(f" Content length: {len(article['content'})} characters)

        # Analyze title sentiment"
        title_result = self.analyzer.analyze(article["title"])

        # Analyze content sentiment
        content_result = self.analyzer.analyze(article["content"])

        # Calculate overall sentiment (weighted average)
        title_weight = 0.3
        content_weight = 0.7

        if title_result["label"] == content_result["label"]:
            overall_label = title_result["label"]
            overall_score = (
                title_result["score"] * title_weight
                + content_result["score"] * content_weight
            )
        else:
            # If different, use content sentiment as primary
            overall_label = content_result["label"]
            overall_score = content_result["score"]

        result = {
            "article_id": article["id"],
            "title": article["title"],
            "topic": article["topic"],
            "source": article["source"],
            "published_at": article["published_at"},
            "title_sentiment": {
                "label": title_result["label"],
                "score": round(title_result["score"}, 3),
            },
            "content_sentiment": {
                "label": content_result["label"],
                "score": round(content_result["score"}, 3),
            },
            "overall_sentiment": {
                "label": overall_label,
                "score": round(overall_score, 3),
            },
            "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Log results
        logger.info(
            f" Title Sentiment: {title_result['label'}} ({title_result['score'}:.3f})
        )
        logger.info("
            f"[Content] Content Sentiment: {content_result['label'}} ({content_result['score'}:.3f})
        )"
        logger.info(" Overall Sentiment: {0} ({1})".format()

        return result

    async def analyze_batch_articles(
        self, articles: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Analyze sentiment for multiple articles."""
        logger.info(
            ""
🔄 Starting batch sentiment analysis for {0} articles...".format(len(articles))"
        )

        results = []
        for i, article in enumerate(articles, 1):
            logger.info(""
--- Article {0}/{1} ---".format(i, len(articles)))"
            result = await self.analyze_single_article(article)
            results.append(result)

        return results

    def analyze_sentiment_trends(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze overall sentiment trends across articles."""
        logger.info(""
 Analyzing Sentiment Trends Across {0} Articles".format(len(results)))
        logger.info("=" * 60)"

        # Count sentiments by label
        sentiment_counts = {"POSITIVE": 0, "NEGATIVE": 0, "NEUTRAL": 0}
        topic_sentiments = {}
        source_sentiments = {}

        total_score = 0
        for result in results:
            overall = result["overall_sentiment"]
            sentiment_counts[overall["label"]] += 1
            total_score += overall["score"]

            # Track by topic
            topic = result["topic"]
            if topic not in topic_sentiments:
                topic_sentiments[topic] = {
                    "POSITIVE": 0,
                    "NEGATIVE": 0,
                    "NEUTRAL": 0,
                    "scores": [},
                }
            topic_sentiments[topic][overall["label"]] += 1
            topic_sentiments[topic]["scores"].append(overall["score"])

            # Track by source
            source = result["source"]
            if source not in source_sentiments:
                source_sentiments[source] = {"POSITIVE": 0, "NEGATIVE": 0, "NEUTRAL": 0}
            source_sentiments[source][overall["label"]] += 1

        # Calculate percentages
        total_articles = len(results)
        sentiment_percentages = {
            label: (count / total_articles) * 100
            for label, count in sentiment_counts.items()
        }

        # Log overall trends
        logger.info("Overall Sentiment Distribution:")
        for label, count in sentiment_counts.items():
            percentage = sentiment_percentages[label]
            logger.info("  {0}: {1} articles ({2}%)".format(label, count, percentage:.1f))

        logger.info(""
Average Sentiment Score: {0}".format()"

        # Log topic-based trends
        logger.info(""
Sentiment by Topic:")"
        for topic, sentiments in topic_sentiments.items():
            total_topic = sum(
                sentiments[label] for label in ["POSITIVE", "NEGATIVE", "NEUTRAL"]
            )
            avg_score = sum(sentiments["scores"]) / len(sentiments["scores"])
            dominant = max(
                sentiments, key=lambda x: sentiments[x] if x != "scores" else 0
            )
            logger.info(
                "  📂 {0}: {1} dominant (avg score: {2})".format(
            )

        # Log source-based trends
        logger.info(""
Sentiment by Source:")"
        for source, sentiments in source_sentiments.items():
            dominant = max(sentiments, key=sentiments.get)
            logger.info("  [News] {0}: {1} dominant".format(source, dominant))

        return {
            "total_articles": total_articles,
            "sentiment_distribution": sentiment_counts,
            "sentiment_percentages": sentiment_percentages,
            "average_score": total_score / total_articles,
            "topic_analysis": topic_sentiments,
            "source_analysis": source_sentiments,
        }


    def save_results(self, results: List[Dict[str, Any]], trends: Dict[str, Any]):
        """Save analysis results to JSON file."""
        output_data = {
            "analysis_metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "analyzer_model": self.analyzer.model_name,
                "total_articles_analyzed": len(results),
            },
            "article_results": results,
            "trend_analysis": trends,
        }

        output_file = "sentiment_analysis_results.json"
        with open(output_file, "w") as f:
            json.dump(output_data, f, indent=2)

        logger.info("💾 Results saved to {0}".format(output_file))
        return output_file


    async def demonstrate_real_time_analysis(self):
        """Demonstrate real-time sentiment analysis."""
        logger.info(""
⚡ Real-time Sentiment Analysis Demo")
        logger.info("=" * 50)"

        sample_texts = [
            "This is absolutely fantastic news! Great job everyone!",
            "I'm really concerned about these troubling developments.",
            "The quarterly report shows standard performance metrics.",
            "Breakthrough technology will revolutionize the industry!",
            "Market volatility creates uncertainty for investors.",'
        ]

        for i, text in enumerate(sample_texts, 1):
            result = self.analyzer.analyze(text)
            logger.info(f"{i}. Text: '{text}')
            logger.info("
                f"   Sentiment: {result['label'}} (confidence: {result['score'}:.3f})
            )


    def generate_summary_report(
        self, results: List[Dict[str, Any]], trends: Dict[str, Any]
    ):"
        """Generate a comprehensive summary report."""
        logger.info(""
 SENTIMENT ANALYSIS PIPELINE SUMMARY REPORT")
        logger.info("=" * 70)

        logger.info(" Analysis Overview:")
        logger.info(f"   • Total articles processed: {trends['total_articles'}})"
        logger.info("   • Analysis model: {0}".format(self.analyzer.model_name))
        logger.info(f"   • Average sentiment score: {trends['average_score'}:.3f}")""

        logger.info(""
 Key Findings:")"

        # Most positive article
        most_positive = max(
            results,
            key=lambda x: (
                x["overall_sentiment"]["score"]
                if x["overall_sentiment"]["label"] == "POSITIVE"
                else 0
            ),
        )
        logger.info(f"    Most Positive: '{most_positive['title'}}')"
        logger.info(f"      Score: {most_positive['overall_sentiment']['score'}:.3f})

        # Most negative article
        most_negative = max(
            results,
            key=lambda x: ("
                x["overall_sentiment"]["score"]
                if x["overall_sentiment"]["label"] == "NEGATIVE"
                else 0
            ),
        )
        logger.info(f"   📉 Most Negative: '{most_negative['title'}}')"
        logger.info(f"      Score: {most_negative['overall_sentiment']['score'}:.3f})

        # Sentiment distribution"
        dist = trends["sentiment_percentages"]
        logger.info("
 Sentiment Distribution:")
        logger.info(f"   🟢 Positive: {dist['POSITIVE'}:.1f}%)"
        logger.info(f"   🔴 Negative: {dist['NEGATIVE'}:.1f}%)"
        logger.info(f"   ⚪ Neutral: {dist['NEUTRAL'}:.1f}%)
"
        logger.info(""
 Analysis completed successfully!")"


async def main():
    """Main demo execution."""
    demo = SentimentPipelineDemo()

    try:
        # Initialize
except Exception:
    pass
        if not await demo.initialize():
            return

        logger.info("[News] Loaded {0} sample articles for analysis".format(len(SAMPLE_ARTICLES)))

        # Analyze articles
        results = await demo.analyze_batch_articles(SAMPLE_ARTICLES)

        # Analyze trends
        trends = demo.analyze_sentiment_trends(results)

        # Real-time demo
        await demo.demonstrate_real_time_analysis()

        # Generate summary
        demo.generate_summary_report(results, trends)

        # Save results
        output_file = demo.save_results(results, trends)

        logger.info(""
 Sentiment Analysis Pipeline Demo Completed Successfully!")
        logger.info("[Content] Detailed results available in: {0}".format(output_file))"

    except Exception as e:
        logger.error("[ERROR] Demo failed: {0}".format(e))
        return 1

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
