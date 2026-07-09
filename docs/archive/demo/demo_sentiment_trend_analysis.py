#!/usr/bin/env python3
"""
Historical Sentiment Trend Analysis Demo

This script demonstrates the complete functionality of the Historical Sentiment
Trend Analysis system, including trend analysis, alert generation, and data visualization.
"""

from src.nlp.sentiment_trend_analyzer import (
    SentimentTrendAnalyzer, SentimentTrendPoint, TopicTrendSummary, TrendAlert,
    analyze_sentiment_trends_for_topic, generate_daily_sentiment_alerts)
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

# Add src directory to path for imports
sys.path.append(str(Path(__file__).parent.parent))


# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class SentimentTrendDemo:
    """Demo class for Historical Sentiment Trend Analysis."""

    def __init__(self):
        """Initialize demo with sample configuration."""
        self.config = self._load_config()
        self.redshift_config = self._get_redshift_config()
        self.sample_data = self._generate_sample_data()

    def _load_config(self) -> Dict:
        """Load configuration for demo."""
        try:
            config_path = (
except Exception:
    pass
                Path(__file__).parent.parent
                / "config"
                / "sentiment_trend_analysis_settings.json"
            )
            with open(config_path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning(
                "Configuration file not found. Using default settings.")
            return {
                "analysis_settings": {
                    "default_time_granularity": "daily",
                    "min_articles_per_data_point": 3,
                    "confidence_threshold": 0.7,
                    "trend_analysis_methods": ["linear_regression", "correlation"},
                },
                "alert_thresholds": {
                    "significant_shift": 0.3,
                    "trend_reversal": 0.25,
                    "volatility_spike": 0.4,
                },
            }

    def _get_redshift_config(self) -> Dict:
        """Get Redshift configuration for demo (mock values)."""
        return {
            "redshift_host": "demo-redshift.amazonaws.com",
            "redshift_port": 5439,
            "redshift_database": "neuronews_demo",
            "redshift_user": "demo_user",
            "redshift_password": "demo_password",
        }

    def _generate_sample_data(self) -> List[Dict]:
        """Generate realistic sample sentiment data for demo."""
        base_date = datetime.now(timezone.utc).date()
        topics = ["technology", "politics", "economy", "health", "environment"]

        sample_data = []
        article_id = 1

        for days_ago in range(30, 0, -1):  # 30 days of data
            current_date = base_date - timedelta(days=days_ago)

            for topic in topics:
                num_articles = 2 + (days_ago % 4)  # Varying number of articles

                for article_num in range(num_articles):
                    # Create varying sentiment patterns by topic
                    if topic == "technology":
                        # Generally positive with occasional dips
                        base_sentiment = 0.3 + (0.4 * (30 - days_ago) / 30)
                        sentiment_noise = (article_num % 3 - 1) * 0.2
                    elif topic == "politics":
                        # Volatile sentiment
                        base_sentiment = 0.1 * ((-1) ** (days_ago % 3))
                        sentiment_noise = (article_num % 5 - 2) * 0.3
                    elif topic == "economy":
                        # Declining then recovering
                        if days_ago > 20:
                            base_sentiment = 0.2 - (days_ago - 20) * 0.5
                        else:
                            base_sentiment = -0.3 + (20 - days_ago) * 0.4
                        sentiment_noise = (article_num % 3 - 1) * 0.15
                    elif topic == "health":
                        # Generally stable with recent improvement
                        base_sentiment = 0.1 + (0.3 * (30 - days_ago) / 30)
                        sentiment_noise = (article_num % 2 - 0.5) * 0.1
                    else:  # environment
                        # Concerning trend with recent alerts
                        base_sentiment = 0.2 - (30 - days_ago) * 0.2
                        sentiment_noise = (article_num % 4 - 2) * 0.25

                    sentiment_score = max(
                        -1.0, min(1.0, base_sentiment + sentiment_noise)
                    )

                    # Determine sentiment label
                    if sentiment_score > 0.1:
                        sentiment_label = "positive"
                    elif sentiment_score < -0.1:
                        sentiment_label = "negative"
                    else:
                        sentiment_label = "neutral"

                    # Generate confidence based on score magnitude
                    confidence = 0.6 + min(0.4, abs(sentiment_score) * 0.5)

                    sample_data.append(
                        {
                            "id": "demo_article_{0}".format(article_id),
                            "title": "{0} News Article {1}".format(topic.title(), article_id),
                            "content": "Sample content for {0} article {1}".format(topic, article_id),
                            "publish_date": current_date,
                            "sentiment_label": sentiment_label,
                            "sentiment_score": round(sentiment_score, 3),
                            "sentiment_confidence": round(confidence, 3),
                            "topic": topic,
                            "keywords": ["{0}_keyword_{1]".format(topic, i) for i in range(1, 4)},
                            "source": "demo_source_{0}".format((article_id % 5) + 1),
                        }
                    )
                    article_id += 1

        logger.info(
            "Generated {0} sample articles across {1} topics".format(
                len(sample_data), len(topics))
        )
        return sample_data

    async def demo_trend_analysis(self) -> Dict[str, TopicTrendSummary]:
        """Demonstrate historical trend analysis for all topics."""
        print(""
" + "=" * 80)
        print("HISTORICAL SENTIMENT TREND ANALYSIS DEMO")
        print("=" * 80)"

        # Create mock analyzer for demo
        analyzer = MockSentimentTrendAnalyzer(self.sample_data, self.redshift_config)

        # Analyze trends for all topics
        start_date = datetime.now(timezone.utc) - timedelta(days=30)
        end_date = datetime.now(timezone.utc)

        print(
            ""
Analyzing sentiment trends from {0} to {1}".format(start_date.date(), end_date.date())"
        )
        print("Time granularity: daily")
        print("Data points: {0} articles".format(len(self.sample_data)))

        summaries = await analyzer.analyze_historical_trends(
            topic=None,  # Analyze all topics
            start_date=start_date,
            end_date=end_date,
            time_granularity="daily",
        )

        # Display results
        print(""
 TREND ANALYSIS RESULTS")
        print("-" * 50)"

        topic_summaries = {}
        for summary in summaries:
            topic_summaries[summary.topic] = summary
            self._display_topic_summary(summary)

        return topic_summaries

    async def demo_alert_generation(self) -> List[TrendAlert]:
        """Demonstrate sentiment alert generation."""
        print(""
" + "=" * 80)
        print("SENTIMENT ALERT GENERATION DEMO")
        print("=" * 80)"

        # Create mock analyzer for demo
        analyzer = MockSentimentTrendAnalyzer(self.sample_data, self.redshift_config)

        print(""
Generating sentiment alerts for the last 7 days...")"

        alerts = await analyzer.generate_sentiment_alerts(
            topic=None, lookback_days=7  # Check all topics
        )

        print(""
🚨 GENERATED ALERTS: {0}".format(len(alerts)))
        print("-" * 50)"

        if alerts:
            for alert in alerts:
                self._display_alert(alert)
        else:
            print("No significant sentiment changes detected in the specified period.")

        return alerts

    async def demo_topic_specific_analysis(
        self, topic: str
    ) -> Optional[TopicTrendSummary]:
        """Demonstrate topic-specific trend analysis."""
        print(""
" + "=" * 80)
        print("TOPIC-SPECIFIC ANALYSIS: {0}".format(topic.upper()))
        print("=" * 80)"

        # Create mock analyzer for demo
        analyzer = MockSentimentTrendAnalyzer(self.sample_data, self.redshift_config)

        print(""
Analyzing sentiment trends for topic: {0}".format(topic))"

        summary = await analyzer.get_topic_trend_summary(topic, days=30)

        if summary:
            self._display_detailed_topic_analysis(summary)
            return summary
        else:
            print("No sufficient data found for topic: {0}".format(topic))
            return None


    async def demo_real_time_monitoring(self) -> None:
        """Demonstrate real-time monitoring capabilities."""
        print(""
" + "=" * 80)
        print("REAL-TIME MONITORING DEMO")
        print("=" * 80)"

        # Create mock analyzer for demo
        analyzer = MockSentimentTrendAnalyzer(self.sample_data, self.redshift_config)

        print(""
Simulating real-time sentiment monitoring...")
        print("Checking for alerts every 30 seconds (demo simulation)")"

        for cycle in range(3):  # Demo 3 monitoring cycles
            print(""
⏰ Monitoring Cycle {0}".format(cycle + 1))
            print("-" * 30)"

            # Check for new alerts
            alerts = await analyzer.generate_sentiment_alerts(lookback_days=1)

            if alerts:
                print("🚨 {0} new alerts detected:".format(len(alerts)))
                for alert in alerts[:2]:  # Show first 2 alerts
                    print("  • {0}: {1} ({2})".format(alert.topic, alert.alert_type, alert.severity))
            else:
                print(" No new alerts detected")

            # Simulate checking active alerts
            active_alerts = await analyzer.get_active_alerts(limit=5)
            print(" Total active alerts: {0}".format(len(active_alerts)))

            # Simulate delay
            await asyncio.sleep(1)  # In real implementation, this would be 30 seconds


    def demo_data_visualization(
        self, topic_summaries: Dict[str, TopicTrendSummary]
    ) -> None:
        """Demonstrate data visualization capabilities."""
        print(""
" + "=" * 80)
        print("DATA VISUALIZATION DEMO")
        print("=" * 80)"

        try:
            # Create visualization plots
except Exception:
    pass
            fig, axes = plt.subplots(2, 2, figsize=(15, 12))
            fig.suptitle(
                "Historical Sentiment Trend Analysis - Demo Results", fontsize=16
            )

            # Plot 1: Sentiment trends by topic
            ax1 = axes[0, 0]
            for topic, summary in topic_summaries.items():
                dates = [point.date for point in summary.data_points]
                scores = [point.sentiment_score for point in summary.data_points]
                ax1.plot(dates, scores, marker="o", label=topic, linewidth=2)

            ax1.set_title("Sentiment Trends by Topic")
            ax1.set_xlabel("Date")
            ax1.set_ylabel("Sentiment Score")
            ax1.legend()
            ax1.grid(True, alpha=0.3)
            ax1.axhline(y=0, color="black", linestyle="--", alpha=0.5)

            # Plot 2: Average sentiment by topic
            ax2 = axes[0, 1]
            topics = list(topic_summaries.keys())
            avg_sentiments = [
                summary.average_sentiment for summary in topic_summaries.values()
            ]
            colors = [
                "green" if s > 0.1 else "red" if s < -0.1 else "orange"
                for s in avg_sentiments
            ]

            bars = ax2.bar(topics, avg_sentiments, color=colors, alpha=0.7)
            ax2.set_title("Average Sentiment by Topic")
            ax2.set_xlabel("Topic")
            ax2.set_ylabel("Average Sentiment Score")
            ax2.axhline(y=0, color="black", linestyle="--", alpha=0.5)

            # Add value labels on bars
            for bar, value in zip(bars, avg_sentiments):
                height = bar.get_height()
                ax2.text(
                    bar.get_x() + bar.get_width() / 2.0,
                    height + (0.1 if height >= 0 else -0.3),
                    "{0}".format(value:.2f),
                    ha="center",
                    va="bottom" if height >= 0 else "top",
                )

            # Plot 3: Sentiment volatility by topic
            ax3 = axes[1, 0]
            volatilities = [
                summary.sentiment_volatility for summary in topic_summaries.values()
            ]
            ax3.bar(topics, volatilities, color="purple", alpha=0.7)
            ax3.set_title("Sentiment Volatility by Topic")
            ax3.set_xlabel("Topic")
            ax3.set_ylabel("Volatility (Standard Deviation)")

            # Plot 4: Article count by topic
            ax4 = axes[1, 1]
            article_counts = [
                summary.statistical_summary.get("total_articles", 0)
                for summary in topic_summaries.values()
            ]
            ax4.bar(topics, article_counts, color="blue", alpha=0.7)
            ax4.set_title("Total Articles by Topic")
            ax4.set_xlabel("Topic")
            ax4.set_ylabel("Number of Articles")

            plt.tight_layout()

            # Save the plot
            output_path = (
                Path(__file__).parent.parent
                / "demo"
                / "sentiment_trend_analysis_demo.png"
            )
            output_path.parent.mkdir(exist_ok=True)
            plt.savefig(output_path, dpi=300, bbox_inches="tight")

            print(" Visualization saved to: {0}".format(output_path))
            print("   The plot shows:")
            print("   • Sentiment trends over time for each topic")
            print("   • Average sentiment comparison across topics")
            print("   • Sentiment volatility analysis")
            print("   • Article volume by topic")

        except ImportError:
            print(" Visualization requires matplotlib and seaborn")
            print("   Install with: pip install matplotlib seaborn")
        except Exception as e:
            print(" Visualization error: {0}".format(e))


    def _display_topic_summary(self, summary: TopicTrendSummary) -> None:
        """Display topic trend summary in a formatted way."""
        print(""
 Topic: {0}".format(summary.topic.upper()))"
        print(
            "   Time Range: {0} to {1}".format(summary.time_range[0].date(), summary.time_range[1].date())
        )
        print("   Current Sentiment: {0}".format()
        print("   Average Sentiment: {0}".format()
        print("   Trend Direction: {0}".format(summary.trend_direction))
        print("   Trend Strength: {0}".format()
        print("   Volatility: {0}".format()
        print("   Data Points: {0}".format(len(summary.data_points)))
        print(
            f"   Total Articles: {summary.statistical_summary.get('total_articles', 'N/A')}
        )

        # Display trend interpretation"
        if summary.trend_direction == "increasing":
            trend_emoji = ""
            trend_desc = "improving sentiment"
        elif summary.trend_direction == "decreasing":
            trend_emoji = "📉"
            trend_desc = "declining sentiment"
        else:
            trend_emoji = ""
            trend_desc = "stable sentiment"

        print("   Interpretation: {0} {1}".format(trend_emoji, trend_desc))

    def _display_detailed_topic_analysis(self, summary: TopicTrendSummary) -> None:
        """Display detailed analysis for a specific topic."""
        self._display_topic_summary(summary)

        print(""
 DETAILED STATISTICS")
        print("-" * 30)"
        stats = summary.statistical_summary
        for key, value in stats.items():
            if isinstance(value, float):
                print(f"   {key.replace('_', ' ').title()}: {value:.3f})
            else:"
                print(f"   {key.replace('_', ' ').title()}: {value})
"
        print("TODO: Fix this string")
")
        print("-" * 40)
        recent_points = (
            summary.data_points[-5:]
            if len(summary.data_points) >= 5
            else summary.data_points
        )
        for point in recent_points:
            date_str = point.date.strftime("%Y-%m-%d")
            sentiment_emoji = (
                "😊"
                if point.sentiment_score > 0.1
                else "😞" if point.sentiment_score < -0.1 else "😐"
            )
            print(
                "   {0}: {1} {2} ({3} articles)".format(date_str, point.sentiment_score:.3f, sentiment_emoji, point.article_count)
            )


    def _display_alert(self, alert: TrendAlert) -> None:
        """Display alert information in a formatted way."""
        severity_emoji = {"low": "🟡", "medium": "🟠", "high": "🔴", "critical": "🚨"}

        alert_type_emoji = {
            "significant_shift": "",
            "trend_reversal": "🔄",
            "volatility_spike": "⚡",
        }

        print(
            f""
{severity_emoji.get(alert.severity, '⚠️')} {alert_type_emoji.get(alert.alert_type, '')} "
            "ALERT: {0}".format(alert.topic.upper())
        )
        print(f"   Type: {alert.alert_type.replace('_', ' ').title()})"
        print("   Severity: {0}".format(alert.severity.upper()))
        print("   Current Sentiment: {0}".format()
        print("   Previous Sentiment: {0}".format()
        print("   Change Magnitude: {0}".format()
        print("   Change Percentage: {0}%".format(alert.change_percentage:.1f))
        print("   Confidence: {0}".format()
        print("   Time Window: {0}".format(alert.time_window))
        print(f"   Triggered: {alert.triggered_at.strftime('%Y-%m-%d %H:%M:%S UTC')})"
        print("   Description: {0}".format(alert.description))


    async def run_complete_demo(self) -> None:
        """Run the complete demo workflow."""
        print(" Starting Historical Sentiment Trend Analysis Demo")
        print(f"📅 Demo Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})

        try:
            # 1. Historical Trend Analysis
except Exception:
    pass
            topic_summaries = await self.demo_trend_analysis()

            # 2. Alert Generation
            alerts = await self.demo_alert_generation()

            # 3. Topic-specific Analysis
            if topic_summaries:
                sample_topic = list(topic_summaries.keys())[0]
                await self.demo_topic_specific_analysis(sample_topic)

            # 4. Real-time Monitoring
            await self.demo_real_time_monitoring()

            # 5. Data Visualization
            if topic_summaries:
                self.demo_data_visualization(topic_summaries)
"
            print(""
" + "=" * 80)
            print(" DEMO COMPLETED SUCCESSFULLY")
            print("=" * 80)
            print("TODO: Fix this string")
Key Features Demonstrated:")
            print("• Historical sentiment trend analysis across multiple topics")
            print("• Automated alert generation for significant sentiment changes")
            print("• Topic-specific deep-dive analysis")
            print("• Real-time monitoring simulation")
            print("• Data visualization and reporting")
            print("TODO: Fix this string")
Next Steps:")
            print("• Deploy to production environment")
            print("• Configure real data sources")
            print("• Set up monitoring dashboards")
            print("• Implement notification systems")"

        except Exception as e:
            logger.error("Demo failed: {0}".format(e))
            print(""
❌ Demo failed: {0}".format(e))"


class MockSentimentTrendAnalyzer:
    """Mock analyzer for demo purposes that works with sample data."""


    def __init__(self, sample_data: List[Dict], redshift_config: Dict):
        self.sample_data = sample_data
        self.redshift_config = redshift_config

        # Load configuration
        try:
            config_path = (
except Exception:
    pass
                Path(__file__).parent.parent
                / "config"
                / "sentiment_trend_analysis_settings.json"
            )
            with open(config_path, "r") as f:
                self.config = json.load(f)
        except FileNotFoundError:
            self.config = {
                "alert_thresholds": {
                    "significant_shift": 0.3,
                    "trend_reversal": 0.25,
                    "volatility_spike": 0.4,
                }
            }


    async def analyze_historical_trends(
        self,
        topic: Optional[str],
        start_date: datetime,
        end_date: datetime,
        time_granularity: str = "daily",
    ) -> List[TopicTrendSummary]:
        """Mock implementation of trend analysis."""
        from src.nlp.sentiment_trend_analyzer import SentimentTrendAnalyzer

        # Create real analyzer for processing logic
        analyzer = SentimentTrendAnalyzer(**self.redshift_config)

        # Filter data by date range
        filtered_data = [
            item
            for item in self.sample_data
            if start_date.date() <= item["publish_date"] <= end_date.date()
        ]

        # Filter by topic if specified
        if topic:
            filtered_data = [item for item in filtered_data if item["topic"] == topic]

        # Group by topic and process
        grouped_data = analyzer._group_data_by_topic(filtered_data)

        summaries = []
        for topic_name, topic_data in grouped_data.items():
            if len(topic_data) >= 3:  # Minimum data requirement
                # Aggregate by time granularity
                aggregated = analyzer._aggregate_by_time_granularity(
                    topic_data, time_granularity
                )

                # Create trend points
                trend_points = analyzer._create_trend_points(topic_name, aggregated)

                # Calculate summary
                time_range = (start_date, end_date)
                summary = analyzer._calculate_trend_summary(
                    topic_name, trend_points, time_range
                )
                summaries.append(summary)

        return summaries

    async def generate_sentiment_alerts(
        self, topic: Optional[str] = None, lookback_days: int = 7
    ) -> List[TrendAlert]:
        """Mock implementation of alert generation."""
        from src.nlp.sentiment_trend_analyzer import SentimentTrendAnalyzer

        # Create real analyzer for processing logic
        analyzer = SentimentTrendAnalyzer(**self.redshift_config)
        analyzer.config = self.config

        # Filter recent data
        cutoff_date = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)
        recent_data = [
            item for item in self.sample_data if item["publish_date"] >= cutoff_date
        ]

        # Group by topic
        grouped_data = analyzer._group_data_by_topic(recent_data)

        all_alerts = []
        for topic_name, topic_data in grouped_data.items():
            if topic is None or topic_name == topic:
                alerts = await analyzer._detect_sentiment_alerts(
                    topic_name, topic_data, lookback_days
                )
                all_alerts.extend(alerts)

        return all_alerts

    async def get_topic_trend_summary(
        self, topic: str, days: int = 30
    ) -> Optional[TopicTrendSummary]:
        """Mock implementation of topic-specific analysis."""
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        end_date = datetime.now(timezone.utc)

        summaries = await self.analyze_historical_trends(
            topic, start_date, end_date, "daily"
        )
        return summaries[0] if summaries else None


    async def get_active_alerts(
        self, topic: Optional[str] = None, limit: int = 10
    ) -> List[TrendAlert]:
        """Mock implementation of active alerts retrieval."""
        # Generate some sample alerts based on recent data
        alerts = await self.generate_sentiment_alerts(topic, lookback_days=3)
        return alerts[:limit]


async def main():
    """Main demo function."""
    demo = SentimentTrendDemo()
    await demo.run_complete_demo()


if __name__ == "__main__":
    # Set up event loop for Windows compatibility
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    # Run the demo
    asyncio.run(main())
