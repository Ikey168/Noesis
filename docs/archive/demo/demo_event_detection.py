"""
Event Detection and Article Clustering Demo (Issue #31)

This demo showcases the complete event detection system:
1. Article embedding generation using BERT
2. K-means clustering for event detection
3. Event significance scoring and ranking
4. Breaking news API functionality

Run: python demo_event_detection.py
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List

import numpy as np

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class EventDetectionDemo:
    """Comprehensive demo of the event detection system."""

    def __init__(self):
        """Initialize demo with sample data."""
        self.sample_articles = self._create_sample_articles()
        self.demo_results = {}

    def _create_sample_articles(self) -> List[Dict[str, Any]]:
        """Create sample articles for demonstration."""
        base_date = datetime.now() - timedelta(hours=12)

        # Technology cluster - AI Safety Summit
        tech_articles = [
            {
                "id": "tech_001",
                "title": "Global AI Safety Summit 2025 Concludes with New International Standards",
                "content": "The Global AI Safety Summit concluded today with representatives from 50 countries agreeing on new international standards for AI development. The summit, hosted by the UK government, addressed concerns about AI safety, ethical development, and global cooperation. Tech leaders including OpenAI, Google DeepMind, and Anthropic participated in discussions about responsible AI deployment.",
                "source": "TechCrunch",
                "published_date": base_date + timedelta(hours=1),
                "category": "Technology",
                "source_credibility": "trusted",
                "sentiment_score": 0.7,
            },
            {
                "id": "tech_002",
                "title": "OpenAI and DeepMind Announce Joint AI Safety Research Initiative",
                "content": "Following the AI Safety Summit, OpenAI and Google DeepMind announced a groundbreaking joint research initiative focused on AI alignment and safety. The collaboration will share research findings and establish common safety protocols. The announcement came as world leaders called for increased cooperation among AI companies.",
                "source": "Wired",
                "published_date": base_date + timedelta(hours=3),
                "category": "Technology",
                "source_credibility": "trusted",
                "sentiment_score": 0.8,
            },
            {
                "id": "tech_003",
                "title": "UK Government Proposes New AI Regulation Framework After Summit",
                "content": "The UK government unveiled a comprehensive AI regulation framework proposal following the successful conclusion of the Global AI Safety Summit. The framework includes mandatory safety testing, transparency requirements, and international cooperation mechanisms. Industry experts praised the balanced approach to innovation and safety.",
                "source": "BBC",
                "published_date": base_date + timedelta(hours=5),
                "category": "Technology",
                "source_credibility": "trusted",
                "sentiment_score": 0.6,
            },
            {
                "id": "tech_004",
                "title": "Tech Leaders Call for Global AI Safety Standards at London Summit",
                "content": "Leading figures from the AI industry called for unified global safety standards during the AI Safety Summit in London. Representatives from major tech companies emphasized the need for coordinated international response to AI development challenges. The summit has been hailed as a landmark event for AI governance.",
                "source": "The Guardian",
                "published_date": base_date + timedelta(hours=7),
                "category": "Technology",
                "source_credibility": "trusted",
                "sentiment_score": 0.7,
            },
        ]

        # Politics cluster - Climate Policy
        politics_articles = [
            {
                "id": "pol_001",
                "title": "Senate Passes Landmark Climate Legislation with Bipartisan Support",
                "content": "The US Senate passed comprehensive climate legislation with surprising bipartisan support, marking a significant shift in American climate policy. The bill includes major investments in renewable energy, carbon capture technology, and green infrastructure. Environmental groups hailed the legislation as a historic achievement.",
                "source": "CNN",
                "published_date": base_date + timedelta(hours=2),
                "category": "Politics",
                "source_credibility": "reliable",
                "sentiment_score": 0.8,
            },
            {
                "id": "pol_002",
                "title": "Climate Bill Allocates $500 Billion for Clean Energy Transition",
                "content": "The newly passed climate legislation allocates $500 billion over ten years for Americas clean energy transition. The funding will support solar and wind projects, electric vehicle infrastructure, and job retraining programs for fossil fuel workers. Economic analysts project significant job creation in green industries.",
                "source": "Reuters",
                "published_date": base_date + timedelta(hours=4),
                "category": "Politics",
                "source_credibility": "trusted",
                "sentiment_score": 0.7,
            },
            {
                "id": "pol_003",
                "title": "Environmental Groups Celebrate Historic Climate Victory in Congress",
                "content": "Environmental organizations are celebrating what they call a historic victory following the passage of comprehensive climate legislation. The bill represents the largest federal investment in clean energy in US history. Activists who have lobbied for years expressed emotional reactions to the legislative breakthrough.",
                "source": "Associated Press",
                "published_date": base_date + timedelta(hours=6),
                "category": "Politics",
                "source_credibility": "trusted",
                "sentiment_score": 0.9,
            },
        ]

        # Health cluster - Medical Breakthrough
        health_articles = [
            {
                "id": "health_001",
                "title": "Revolutionary Gene Therapy Shows Promise for Alzheimer's Treatment",
                "content": "A breakthrough gene therapy treatment for Alzheimers disease showed remarkable results in Phase II clinical trials. The therapy, developed by researchers at Johns Hopkins, demonstrated significant cognitive improvement in 78% of patients. The FDA has granted fast-track designation for the treatment.",
                "source": "Medical News Today",
                "published_date": base_date + timedelta(hours=8),
                "category": "Health",
                "source_credibility": "reliable",
                "sentiment_score": 0.9, '
            },
            {
                "id": "health_002",
                "title": "Alzheimer's Gene Therapy Receives FDA Fast-Track Approval",
                "content": "The FDA announced fast-track approval for a promising Alzheimers gene therapy following encouraging clinical trial results. The decision accelerates the review process for what could be the first effective treatment for the devastating neurodegenerative disease. Patients and families expressed hope for the breakthrough.",
                "source": "WebMD",
                "published_date": base_date + timedelta(hours=9),
                "category": "Health",
                "source_credibility": "reliable",
                "sentiment_score": 0.8,'
            },
        ]

        # Unrelated articles (should form smaller clusters or be outliers)
        misc_articles = [
            {
                "id": "misc_001",
                "title": "Local Restaurant Chain Expands to Five New Locations",
                "content": "Popular local restaurant chain announced expansion plans to open five new locations across the metropolitan area. The family-owned business has been serving the community for over 20 years and attributes success to fresh ingredients and customer service.",
                "source": "Local News Network",
                "published_date": base_date + timedelta(hours=10),
                "category": "Business",
                "source_credibility": "reliable",
                "sentiment_score": 0.6,
            },
            {
                "id": "misc_002",
                "title": "City Council Approves New Park Development Project",
                "content": "The city council unanimously approved a new park development project that will create 50 acres of green space in the downtown area. The project includes playgrounds, walking trails, and community gardens. Construction is expected to begin next spring.",
                "source": "City Herald",
                "published_date": base_date + timedelta(hours=11),
                "category": "General",
                "source_credibility": "reliable",
                "sentiment_score": 0.7,
            },
        ]

        return tech_articles + politics_articles + health_articles + misc_articles

    async def run_demo(self):
        """Run the complete event detection demo."""
        print(" Starting Event Detection and Article Clustering Demo")
        print(f"📅 Demo Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(""
" + "=" * 60)
        print("🔬 EVENT DETECTION SYSTEM DEMO")
        print("=" * 60)"

        try:
            # 1. Article Embedding Demo
            await self.demo_article_embedding()

            # 2. Event Clustering Demo
            await self.demo_event_clustering()

            # 3. Breaking News API Demo
            await self.demo_breaking_news_api()

            # 4. Event Significance Scoring Demo
            await self.demo_event_significance()

            # 5. Database Integration Demo
            await self.demo_database_integration()

            # 6. Performance Metrics Demo
            await self.demo_performance_metrics()

            # Save demo results
            await self.save_demo_results()

            print(""
" + "=" * 60)
            print(" Event Detection Demo Completed Successfully!")
            print("=" * 60)"

        except Exception as e:
            logger.error("Demo execution failed: {0}".format(e))
            print(""
❌ Demo failed with error: {0}".format(e))"


    async def demo_article_embedding(self):
        """Demonstrate article embedding generation."""
        print(""
 ARTICLE EMBEDDING GENERATION")
        print("-" * 40)"

        try:
            from src.nlp.article_embedder import (
                ArticleEmbedder, get_redshift_connection_params)

            # Initialize embedder
            embedder = ArticleEmbedder(
                model_name="all-MiniLM-L6-v2",
                conn_params=get_redshift_connection_params(),
            )

            print("🤖 Model: {0}".format(embedder.model_name))
            print("📏 Embedding dimension: {0}".format(embedder.embedding_dimension))

            # Generate embeddings for sample articles
            print(
                ""
🔄 Generating embeddings for {0} articles...".format(len(self.sample_articles))"
            )

            embeddings = await embedder.generate_embeddings_batch(self.sample_articles)

            print(" Generated {0} embeddings".format(len(embeddings)))

            # Show quality metrics
            if embeddings:
                avg_quality = np.mean(
                    [emb["embedding_quality_score"] for emb in embeddings]
                )
                avg_time = np.mean([emb["processing_time"] for emb in embeddings])

                print(" Average quality score: {0}".format()
                print("⏱️ Average processing time: {0}s".format()

            # Store results
            self.demo_results["embeddings"] = {
                "count": len(embeddings),
                "model": embedder.model_name,
                "dimension": embedder.embedding_dimension,
                "avg_quality": avg_quality if embeddings else 0,
                "avg_processing_time": avg_time if embeddings else 0,
                "statistics": embedder.get_statistics(),
            }

            # Store embeddings for next steps
            self.embeddings_data = []
            for i, embedding in enumerate(embeddings):
                article = self.sample_articles[i]
                self.embeddings_data.append(
                    {
                        "article_id": article["id"],
                        "title": article["title"],
                        "source": article["source"],
                        "published_date": article["published_date"],
                        "category": article["category"],
                        "sentiment_score": article["sentiment_score"],
                        "source_credibility": article["source_credibility"],
                        "embedding_vector": np.array(embedding["embedding_vector"]),
                        "embedding_quality_score": embedding["embedding_quality_score"],
                    }
                )

        except Exception as e:
            logger.error("Error in embedding demo: {0}".format(e))
            print("❌ Embedding demo failed: {0}".format(e))
            self.embeddings_data = []


    async def demo_event_clustering(self):
        """Demonstrate event clustering and detection."""
        print(""
 EVENT CLUSTERING AND DETECTION")
        print("-" * 40)"

        try:
            from src.nlp.event_clusterer import (
                EventClusterer, get_redshift_connection_params)

            if not hasattr(self, "embeddings_data") or not self.embeddings_data:
                print("❌ No embeddings available for clustering")
                return

            # Initialize clusterer
            clusterer = EventClusterer(
                conn_params=get_redshift_connection_params(),
                min_cluster_size=2,  # Lower for demo
                max_clusters=10,
                clustering_method="kmeans",
            )

            print("🔬 Clustering method: {0}".format(clusterer.clustering_method))
            print(" Min cluster size: {0}".format(clusterer.min_cluster_size))

            # Detect events
            print(""
🔄 Detecting events from {0} articles...".format(len(self.embeddings_data)))"

            events = await clusterer.detect_events(self.embeddings_data)

            print(" Detected {0} events".format(len(events)))

            # Display event details
            if events:
                print(""
📰 Detected Events:")"
                for i, event in enumerate(events[:5], 1):  # Show top 5
                    print(f"
{i}. {event['cluster_name']}")
                    print(f"   📂 Category: {event['category']}")
                    print(f"   🔥 Type: {event['event_type']}")
                    print(f"    Articles: {event['cluster_size']}")
                    print(f"   ⭐ Trending Score: {event['trending_score']:.2f}")
                    print(f"   💥 Impact Score: {event['impact_score']:.1f}")
                    print(f"   ⚡ Velocity Score: {event['velocity_score']:.2f}")
                    print(
                        f"   🌍 Geographic Focus: {', '.join(event['geographic_focus'][:3])}"
                    )
                    print(f"   👥 Key Entities: {', '.join(event['key_entities'][:3])}")

                    # Show sample articles
                    print("   📄 Sample Articles:")
                    for article in event["articles"][:2]:
                        print(
                            f"     • {article['title'][:60]}... ({article['source']})"
                        )

            # Store results
            self.demo_results["clustering"] = {
                "events_detected": len(events),
                "clustering_method": clusterer.clustering_method,
                "statistics": clusterer.get_statistics(),
                "events": events,
            }

            self.detected_events = events

        except Exception as e:
            logger.error("Error in clustering demo: {0}".format(e))
            print("❌ Clustering demo failed: {0}".format(e))
            self.detected_events = []


    async def demo_breaking_news_api(self):
        """Demonstrate breaking news API functionality."""
        print(""
📺 BREAKING NEWS API DEMO")
        print("-" * 40)"

        try:
            if not hasattr(self, "detected_events") or not self.detected_events:
                print("❌ No events available for API demo")
                return

            # Simulate API responses
            print("🌐 Simulating API endpoints...")

            # Filter events by criteria
            breaking_events = [
                event
                for event in self.detected_events
                if event["trending_score"] >= 2.0 and event["impact_score"] >= 50.0
            ]

            print(""
 API Response Simulation:")
            print("🔥 Breaking events: {0}".format(len(breaking_events)))"

            # Group by category
            category_stats = {}
            for event in self.detected_events:
                category = event["category"]
                if category not in category_stats:
                    category_stats[category] = {"count": 0, "avg_score": 0}
                category_stats[category]["count"] += 1
                category_stats[category]["avg_score"] += event["trending_score"]

            # Calculate averages
            for category in category_stats:
                count = category_stats[category]["count"]
                category_stats[category]["avg_score"] /= count

            print(""
 Events by Category:")"
            for category, stats in sorted(
                category_stats.items(), key=lambda x: x[1]["avg_score"], reverse=True
            ):
                print(
                    f"   {category}: {stats['count']} events (avg score: {stats['avg_score']:.2f})"
                )

            # Simulate API endpoint responses
            api_responses = {
                "/breaking_news": {
                    "events": len(breaking_events),
                    "response_time": "0.15s",
                    "cached": False,
                },
                "/breaking_news?category=Technology": {
                    "events": len(
                        [e for e in breaking_events if e["category"] == "Technology"]
                    ),
                    "response_time": "0.12s",
                    "cached": False,
                },
                "/events/clusters": {
                    "clusters": len(self.detected_events),
                    "response_time": "0.08s",
                    "cached": True,
                },
            }

            print(""
🌐 API Endpoint Performance:")"
            for endpoint, metrics in api_responses.items():
                print("   {0}".format(endpoint))
                print(
                    f"      Results: {metrics['events'] if 'events' in metrics else metrics['clusters']}"
                )
                print(f"     ⏱️ Response time: {metrics['response_time']}")
                print(f"     💾 Cached: {metrics['cached']}")

            # Store results
            self.demo_results["api"] = {
                "breaking_events": len(breaking_events),
                "total_events": len(self.detected_events),
                "category_distribution": category_stats,
                "endpoint_performance": api_responses,
            }

        except Exception as e:
            logger.error("Error in API demo: {0}".format(e))
            print("❌ API demo failed: {0}".format(e))


    async def demo_event_significance(self):
        """Demonstrate event significance scoring."""
        print(""
⭐ EVENT SIGNIFICANCE SCORING")
        print("-" * 40)"

        try:
            if not hasattr(self, "detected_events") or not self.detected_events:
                print("❌ No events available for significance demo")
                return

            print(" Event Significance Analysis:")

            # Calculate significance scores
            significance_scores = []
            for event in self.detected_events:
                significance = (
                    event["trending_score"] * 0.3
                    + event["impact_score"] * 0.4
                    + event["velocity_score"] * 0.2
                    + (event["cluster_size"] / 20) * 0.1
                )
                significance_scores.append(significance)
                event["significance_score"] = significance

            # Sort by significance
            sorted_events = sorted(
                self.detected_events,
                key=lambda x: x["significance_score"],
                reverse=True,
            )

            print(""
🏆 Top Events by Significance:")"
            for i, event in enumerate(sorted_events[:3], 1):
                print(f"
{i}. {event['cluster_name']}")
                print(f"    Significance: {event['significance_score']:.2f}")
                print(f"    Trending: {event['trending_score']:.2f}")
                print(f"   💥 Impact: {event['impact_score']:.1f}")
                print(f"   ⚡ Velocity: {event['velocity_score']:.2f}")
                print(f"    Cluster Size: {event['cluster_size']}")
                print(f"   ⏱️ Duration: {event['event_duration_hours']:.1f} hours")

            # Significance distribution
            high_significance = len([s for s in significance_scores if s >= 50])
            medium_significance = len([s for s in significance_scores if 20 <= s < 50])
            low_significance = len([s for s in significance_scores if s < 20])

            print(""
 Significance Distribution:")
            print("   🔥 High (≥50): {0} events".format(high_significance))
            print("   🔶 Medium (20-49): {0} events".format(medium_significance))
            print("   🔵 Low (<20): {0} events".format(low_significance))"

            # Store results
            self.demo_results["significance"] = {
                "avg_significance": np.mean(significance_scores),
                "max_significance": (
                    max(significance_scores) if significance_scores else 0
                ),
                "distribution": {
                    "high": high_significance,
                    "medium": medium_significance,
                    "low": low_significance,
                },
                "top_events": [
                    {
                        "name": event["cluster_name"],
                        "significance": event["significance_score"],
                        "category": event["category"],
                    }
                    for event in sorted_events[:3]
                ],
            }

        except Exception as e:
            logger.error("Error in significance demo: {0}".format(e))
            print("❌ Significance demo failed: {0}".format(e))


    async def demo_database_integration(self):
        """Demonstrate database integration capabilities."""
        print(""
🗄️ DATABASE INTEGRATION DEMO")
        print("-" * 40)"

        try:
            print(" Database Integration Features:")
            print("    Event clusters storage")
            print("    Article-cluster assignments")
            print("    Embedding vectors caching")
            print("    Performance metrics tracking")
            print("    Breaking news views")

            # Simulate database operations
            print(""
💾 Simulated Database Operations:")
            print(
                f"   📝 Events to store: {len(self.detected_events) if hasattr(self, 'detected_events') else 0}""
            )
            print(
                f"   🔗 Article assignments: {sum(len(e['articles']) for e in self.detected_events) if hasattr(self, 'detected_events') else 0}"
            )
            print(
                f"   🧠 Embeddings cached: {len(self.embeddings_data) if hasattr(self, 'embeddings_data') else 0}"
            )

            # Schema information
            print(""
🏗️ Database Schema:")
            print("    event_clusters table")
            print("   🔗 article_cluster_assignments table")
            print("   🧠 article_embeddings table")
            print("    Views: active_breaking_news, trending_events_by_category")"

            # Performance estimates
            print(""
⚡ Performance Estimates:")
            print("   💾 Storage per event: ~2KB")
            print("   🧠 Embedding storage: ~1.5KB per article")
            print("    Query response time: <100ms")
            print("    Indexing: Optimized for time-based queries")"

            # Store results
            self.demo_results["database"] = {
                "events_to_store": (
                    len(self.detected_events) if hasattr(self, "detected_events") else 0
                ),
                "embeddings_cached": (
                    len(self.embeddings_data) if hasattr(self, "embeddings_data") else 0
                ),
                "schema_tables": [
                    "event_clusters",
                    "article_cluster_assignments",
                    "article_embeddings",
                ],
                "performance_optimized": True,
            }

        except Exception as e:
            logger.error("Error in database demo: {0}".format(e))
            print("❌ Database demo failed: {0}".format(e))


    async def demo_performance_metrics(self):
        """Demonstrate performance metrics and monitoring."""
        print(""
 PERFORMANCE METRICS DEMO")
        print("-" * 40)"

        try:
            # Calculate overall metrics
            total_articles = len(self.sample_articles)
            total_events = (
                len(self.detected_events) if hasattr(self, "detected_events") else 0
            )
            total_embeddings = (
                len(self.embeddings_data) if hasattr(self, "embeddings_data") else 0
            )

            # Processing efficiency
            if hasattr(self, "demo_results") and "embeddings" in self.demo_results:
                embedding_stats = self.demo_results["embeddings"]["statistics"]
                avg_embedding_time = embedding_stats.get("avg_processing_time", 0)
            else:
                avg_embedding_time = 0

            if hasattr(self, "demo_results") and "clustering" in self.demo_results:
                clustering_stats = self.demo_results["clustering"]["statistics"]
                clustering_time = clustering_stats.get("processing_time", 0)
            else:
                clustering_time = 0

            print(" Overall Performance Metrics:")
            print("   📰 Articles processed: {0}".format(total_articles))
            print("   🧠 Embeddings generated: {0}".format(total_embeddings))
            print("    Events detected: {0}".format(total_events))
            print("   ⏱️ Avg embedding time: {0}s per article".format()
            print("    Clustering time: {0}s total".format()

            # Quality metrics
            if hasattr(self, "detected_events") and self.detected_events:
                avg_cluster_size = np.mean(
                    [e["cluster_size"] for e in self.detected_events]
                )
                avg_silhouette = np.mean(
                    [e["silhouette_score"] for e in self.detected_events]
                )
                avg_cohesion = np.mean(
                    [e["cohesion_score"] for e in self.detected_events]
                )

                print(""
 Quality Metrics:")
                print("    Average cluster size: {0}".format(avg_cluster_size:.1f))
                print("    Average silhouette score: {0}".format()
                print("   🎪 Average cohesion score: {0}".format()"

            # System efficiency
            total_processing_time = (
                avg_embedding_time * total_articles + clustering_time
            )
            throughput = (
                total_articles / total_processing_time
                if total_processing_time > 0
                else 0
            )

            print(""
⚡ System Efficiency:")
            print("    Total processing time: {0}s".format(total_processing_time:.2f))
            print("    Throughput: {0} articles/second".format(throughput:.1f))
            print("   💾 Memory efficient: Batch processing enabled")
            print("   🔄 Scalable: Async processing architecture")"

            # Store metrics
            self.demo_results["performance"] = {
                "total_articles": total_articles,
                "total_events": total_events,
                "total_embeddings": total_embeddings,
                "avg_embedding_time": avg_embedding_time,
                "clustering_time": clustering_time,
                "total_processing_time": total_processing_time,
                "throughput": throughput,
                "quality_metrics": {
                    "avg_cluster_size": (
                        avg_cluster_size
                        if hasattr(self, "detected_events") and self.detected_events
                        else 0
                    ),
                    "avg_silhouette_score": (
                        avg_silhouette
                        if hasattr(self, "detected_events") and self.detected_events
                        else 0
                    ),
                    "avg_cohesion_score": (
                        avg_cohesion
                        if hasattr(self, "detected_events") and self.detected_events
                        else 0
                    ),
                },
            }

        except Exception as e:
            logger.error("Error in performance demo: {0}".format(e))
            print("❌ Performance demo failed: {0}".format(e))


    async def save_demo_results(self):
        """Save demo results to JSON file."""
        try:
            # Add metadata
            self.demo_results["metadata"] = {
                "demo_date": datetime.now().isoformat(),
                "version": "1.0",
                "sample_articles_count": len(self.sample_articles),
                "demo_duration": time.time(),  # Will be updated
            }

            # Convert numpy arrays to lists for JSON serialization

            def convert_numpy(obj):
                if isinstance(obj, np.ndarray):
                    return obj.tolist()
                elif isinstance(obj, np.integer):
                    return int(obj)
                elif isinstance(obj, np.floating):
                    return float(obj)
                elif isinstance(obj, dict):
                    return {key: convert_numpy(value) for key, value in obj.items()}
                elif isinstance(obj, list):
                    return [convert_numpy(item) for item in obj]
                else:
                    return obj

            # Clean results for JSON
            clean_results = convert_numpy(self.demo_results)

            # Save to file
            output_file = "event_detection_demo_results.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(clean_results, f, indent=2, ensure_ascii=False, default=str)

            print(""
💾 Demo results saved to: {0}".format(output_file))"

        except Exception as e:
            logger.error("Error saving demo results: {0}".format(e))
            print("❌ Failed to save results: {0}".format(e))


async def main():
    """Run the event detection demo."""
    demo = EventDetectionDemo()
    await demo.run_demo()


if __name__ == "__main__":
    asyncio.run(main())
