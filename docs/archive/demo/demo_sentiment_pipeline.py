"""
Demo script for Sentiment Analysis Pipeline functionality.
Showcases sentiment analysis across news articles with various analysis features.
"""

import asyncio
import logging
from typing import Any, Dict, List

from src.nlp.sentiment_analysis import create_analyzer
from src.nlp.article_processor import ArticleProcessor

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
        treating advanced-stage patients, offering new hope for those with limited options.
        Medical experts are calling this development revolutionary and a significant step forward
        in the fight against cancer. The research team expects to begin Phase III trials next year,
        bringing hope to millions of patients worldwide who could benefit from this
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
        A comprehensive climate study released today shows that global temperatures have risen
        by 1.2 degrees Celsius since pre-industrial times, with the past decade marking the
        warmest period on record. Scientists warn that without immediate action, the impact
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
        logger.info("[INIT] Initializing Sentiment Analysis Pipeline Demo...")
        logger.info("=" * 60)

        try:
            self.analyzer = create_analyzer()
            logger.info(
                f"[OK] Sentiment analyzer initialized with model: {self.analyzer.model_name}"
            )
            return True
        except Exception as e:
            logger.error(f"[ERROR] Failed to initialize sentiment analyzer: {e}")
            return False

    async def analyze_single_article(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze sentiment for a single article."""
        logger.info(f"[News] Analyzing: '{article['title']}'")
        logger.info(f"[Date] Published: {article['published_at']}")
        logger.info(f"[Topic] Topic: {article['topic']}")
        logger.info(f"[Content] Content length: {len(article['content'])} characters")

        # Analyze title sentiment
        title_result = self.analyzer.analyze(article["title"])

        # Analyze content sentiment
        content_result = self.analyzer.analyze(article["content"])

        result = {
            "article_id": article["id"],
            "title": article["title"],
            "topic": article["topic"],
            "title_sentiment": {
                "label": title_result.label,
                "score": title_result.score,
                "confidence": title_result.confidence,
            },
            "content_sentiment": {
                "label": content_result.label,
                "score": content_result.score,
                "confidence": content_result.confidence,
            },
            "overall_sentiment": {
                "label": content_result.label,  # Use content as primary
                "score": content_result.score,
                "confidence": content_result.confidence,
            },
        }

        logger.info(f"[Result] Title: {title_result.label} ({title_result.score:.3f})")
        logger.info(f"[Result] Content: {content_result.label} ({content_result.score:.3f})")
        logger.info("-" * 60)

        return result

    async def analyze_batch_articles(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Analyze sentiment for multiple articles."""
        logger.info(f"[Batch] Starting batch analysis of {len(articles)} articles...")
        
        results = []
        for i, article in enumerate(articles, 1):
            logger.info(f"[Progress] Processing article {i}/{len(articles)}")
            result = await self.analyze_single_article(article)
            results.append(result)
            
        logger.info(f"[Complete] Batch analysis completed: {len(results)} articles processed")
        return results

    async def analyze_sentiment_trends(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze overall sentiment trends across articles."""
        logger.info("[Trends] Analyzing sentiment trends across articles...")
        
        if not results:
            return {"error": "No results to analyze"}

        # Count sentiment labels
        sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0}
        total_score = 0
        topic_sentiments = {}

        for result in results:
            label = result["overall_sentiment"]["label"]
            score = result["overall_sentiment"]["score"]
            topic = result["topic"]

            sentiment_counts[label] += 1
            total_score += score

            if topic not in topic_sentiments:
                topic_sentiments[topic] = {"scores": [], "labels": []}
            
            topic_sentiments[topic]["scores"].append(score)
            topic_sentiments[topic]["labels"].append(label)

        # Calculate statistics
        total_articles = len(results)
        avg_score = total_score / total_articles if total_articles > 0 else 0
        
        # Determine overall trend
        if sentiment_counts["positive"] > sentiment_counts["negative"]:
            overall_trend = "positive"
        elif sentiment_counts["negative"] > sentiment_counts["positive"]:
            overall_trend = "negative"
        else:
            overall_trend = "neutral"

        trend_analysis = {
            "total_articles": total_articles,
            "overall_trend": overall_trend,
            "average_score": avg_score,
            "sentiment_distribution": sentiment_counts,
            "topic_breakdown": {}
        }

        # Analyze by topic
        for topic, data in topic_sentiments.items():
            topic_avg = sum(data["scores"]) / len(data["scores"])
            topic_sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0}
            for label in data["labels"]:
                topic_sentiment_counts[label] += 1
            
            trend_analysis["topic_breakdown"][topic] = {
                "average_score": topic_avg,
                "article_count": len(data["scores"]),
                "sentiment_distribution": topic_sentiment_counts
            }

        logger.info(f"[Trends] Overall trend: {overall_trend} (avg score: {avg_score:.3f})")
        logger.info(f"[Trends] Distribution: {sentiment_counts}")
        
        return trend_analysis

    async def run_full_demo(self):
        """Run the complete sentiment analysis demonstration."""
        logger.info("[DEMO] Starting Sentiment Analysis Pipeline Demo")
        logger.info("=" * 80)

        # Initialize
        if not await self.initialize():
            logger.error("[DEMO] Failed to initialize. Exiting.")
            return

        # Analyze all sample articles
        results = await self.analyze_batch_articles(SAMPLE_ARTICLES)
        self.results = results

        # Analyze trends
        trends = await self.analyze_sentiment_trends(results)

        # Summary
        logger.info("\n" + "=" * 80)
        logger.info("[SUMMARY] Sentiment Analysis Demo Complete")
        logger.info("=" * 80)
        logger.info(f"Articles Processed: {len(results)}")
        logger.info(f"Overall Sentiment: {trends['overall_trend']}")
        logger.info(f"Average Score: {trends['average_score']:.3f}")
        logger.info(f"Distribution: {trends['sentiment_distribution']}")
        
        for topic, data in trends["topic_breakdown"].items():
            logger.info(f"{topic}: {data['article_count']} articles, avg: {data['average_score']:.3f}")

        return {
            "results": results,
            "trends": trends,
            "summary": {
                "total_processed": len(results),
                "overall_sentiment": trends["overall_trend"],
                "average_score": trends["average_score"]
            }
        }


async def main():
    """Main demo execution function."""
    demo = SentimentPipelineDemo()
    await demo.run_full_demo()


if __name__ == "__main__":
    asyncio.run(main())
