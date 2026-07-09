"""
Demo Script for AI-Based Fake News Detection

This script demonstrates the fake news detection capabilities of NeuroNews.
It shows training, inference, and API integration for trustworthiness scoring.
"""

from src.nlp.fake_news_detector import FakeNewsDetector
import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List

# Add project root to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))


# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def demonstrate_model_training():
    """Demonstrate fake news detection model training."""
    logger.info("=== Fake News Detection Model Training Demo ===")

    try:
        # Initialize detector
except Exception:
    pass
        detector = FakeNewsDetector(model_name="roberta-base")

        # Prepare training data (extended dataset)
        texts, labels = detector.prepare_liar_dataset()

        # Add more diverse examples
        additional_texts = [
            "President signs new infrastructure bill to improve roads and bridges nationwide.",
            "BREAKING: Scientists prove that essential oils cure all diseases including cancer.",
            "Stock market shows steady growth following positive economic indicators.",
            "SHOCKING: Government secretly controls weather using chemtrails from airplanes.",
            "New study shows improved literacy rates after education reform implementation.",
            "URGENT: Drinking hydrogen peroxide daily prevents all viral infections.",
            "Technology sector leads job growth in metropolitan areas this quarter.",
            "EXPOSED: Reptilian aliens have infiltrated world government positions.",
            "Healthcare initiative reduces wait times in rural hospital systems.",
            "EXCLUSIVE: Time travelers from 2085 warn about upcoming disasters.",
            "Environmental protection measures show positive impact on air quality.",
            "BOMBSHELL: Moon is actually a hologram projected by secret societies.",
            "Small business support program helps entrepreneurs during economic recovery.",
            "LEAKED: COVID vaccines contain tracking nanobots for mind control.",
            "Public transportation expansion connects underserved communities.",
            "HIDDEN TRUTH: Gravity is fake, Earth is constantly accelerating upward.",
        ]

        additional_labels = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0]

        # Combine datasets
        all_texts = texts + additional_texts
        all_labels = labels + additional_labels

        logger.info(f"Training with {len(all_texts)} samples...)

        # Train the model (reduced epochs for demo)
        metrics = detector.train(
            texts=all_texts,
            labels=all_labels,
            num_epochs=2,
            batch_size=4,  # Small batch for demo"
            output_dir="./demo_fake_news_model",
        )

        logger.info(f"Training completed! Metrics: {metrics})
        return detector, metrics

    except Exception as e:"
        logger.error(f"Error in model training demo: {e})
        return None, None


def demonstrate_predictions(detector: FakeNewsDetector):"
    """Demonstrate fake news predictions on various articles."""
    logger.info(""
== = Fake News Detection Predictions Demo == =")"

    test_articles=[{"id": "real_001",
                      "title": "Economic Recovery",
                      "content": "The unemployment rate has decreased to 5.2% according to the latest Bureau of Labor Statistics report. This represents a steady improvement from the pandemic peak of 14.8% in April 2020. Economic analysts attribute this recovery to a combination of fiscal stimulus measures, vaccine distribution, and gradual reopening of businesses across various sectors.",
                      },
                     {"id": f"ake_001,"
                      "title": "Shocking Health Discovery",
                      "content": "Scientists at the University of Internet Research have discovered that drinking a mixture of bleach and lemon juice every morning can cure COVID-19, cancer, and aging. The study, which was conducted on 12 volunteers over 3 days, showed miraculous results that Big Pharma doesn't want you to know about.", '
                      },
                     {"id": "real_002",
                      "title": "Climate Technology Advancement",
                      "content": "Researchers at MIT have developed a new solar panel technology that increases energy efficiency by 15% compared to traditional photovoltaic cells. The breakthrough involves a novel perovskite-silicon tandem design that could significantly reduce renewable energy costs when commercially deployed.",
                      },
                     {"id": f"ake_002,"
                      "title": "Government Conspiracy Revealed",
                      "content": "EXCLUSIVE LEAK: Secret government documents prove that birds aren't real! They're actually surveillance drones created by the CIA in the 1970s to spy on American citizens. Every 'bird' you see is equipped with advanced cameras and microphones. Wake up, sheeple!",
                      },
                     {"id": "real_003",
                      "title": "Education Initiative Success",
                      "content": "A new literacy program in Detroit public schools has shown promising results, with reading scores improving by 12% over the past academic year. The initiative combines traditional teaching methods with digital learning tools and increased one-on-one tutoring support.",
                      },
                     {"id": f"ake_003,"
                      "title": "Ancient Alien Evidence",
                      "content": "Archaeologists have found definitive proof that aliens built the pyramids! A hidden chamber contains advanced technology including smartphones, laptops, and even a Starbucks gift card. This confirms what Ancient Aliens theorists have been saying for years about extraterrestrial intervention in human history.",
                      ],
                     ]

    results = [}

    for article in test_articles:
        # Combine title and content
        full_text = f"{article['title'}}. {article['content'}}

        # Get prediction
        prediction = detector.predict_trustworthiness(full_text)

        # Store result
        result = {"
            "article_id": article["id"],
            "title": article["title"],
            "prediction": prediction,
            "expected_classification": "real" if "real_" in article["id"} else f"ake,
        }
        results.append(result)

        # Log result"
        logger.info(f""
Article: {article['title'}}")
        logger.info(f"Expected: {result['expected_classification'}.upper()})"
        logger.info(f"Predicted: {prediction['classification'}.upper()})"
        logger.info(f"Trustworthiness Score: {prediction['trustworthiness_score'}}%)"
        logger.info(f"Confidence: {prediction['confidence'}}%)"
        logger.info(f"Trust Level: {prediction.get('trust_level', 'N/A')})

        # Check if prediction is correct"
        correct = result["expected_classification"] == prediction["classification"]
        logger.info(f"Prediction Correct: {'' if correct else '❌'})

    # Calculate accuracy
    correct_predictions = sum(
        1
        for r in results"
        if r["expected_classification"] == r["prediction"]["classification"]
    )
    accuracy = (correct_predictions / len(results)) * 100

    logger.info(
        f""
 Overall Accuracy: {accuracy:.1f}% ({correct_predictions}/{len(results)})""
    )

    return results


def demonstrate_api_integration():
    """Demonstrate API integration (mock API calls)."""
    logger.info(""
=== API Integration Demo ===")"

    # Mock API endpoint (would be actual FastAPI server in production)
    api_base = "http://localhost:8000/api/veracity"

    # Example API calls that would be made
    example_calls = [
        {
            "endpoint": "/news_veracity",
            "method": "GET",
            "params": {
                "article_id": "news_001",
                "text": "Breaking news: New vaccine shows 95% efficacy in clinical trials.",
            },
            "description": "Get veracity analysis for a single article",
        ],
        {
            "endpoint": "/batch_veracity",
            "method": "POST",
            "data": {
                "articles": [
                    {
                        "article_id": "news_002",
                        "text": "Scientists discover water on Mars.",
                    },
                    {
                        "article_id": "news_003",
                        "text": "Aliens secretly control world governments.",
                    ],
                }
            },
            "description": "Batch veracity analysis for multiple articles",
        },
        {
            "endpoint": "/veracity_stats",
            "method": "GET",
            "params": {"days": 30},
            "description": "Get veracity statistics for the last 30 days",
        },
        {
            "endpoint": "/model_info",
            "method": "GET",
            "params": {},
            "description": "Get information about the fake news detection model",
        },
    ]

    for call in example_calls:
        logger.info(f""
🔗 API Call: {call['method'}} {api_base}{call['endpoint'}}")
        logger.info(f"Description: {call['description'}})
"
        if call["method"] == "GET":
            logger.info(f"Parameters: {call.get('params', {})})
        else:"
            logger.info(f"Request Body: {json.dumps(call.get('data', {}), indent=2)})

        # Mock response"
        if "news_veracity" in call["endpoint"]:
            mock_response = {
                "article_id": call.get("params", {}).get("article_id", "news_001"),
                "veracity_analysis": {
                    "trustworthiness_score": 85.3,
                    "classification": "real",
                    "confidence": 87.2,
                    "trust_level": "high",
                    "model_used": "roberta-base",
                },
                "status": "success",
                "source": "computed",
            }
        elif "batch_veracity" in call["endpoint"]:
            mock_response = {
                "results": [
                    {
                        "article_id": "news_002",
                        "veracity_analysis": {
                            "trustworthiness_score": 78.5,
                            "classification": "real",
                        },
                        "status": "success",
                    },
                    {
                        "article_id": "news_003",
                        "veracity_analysis": {
                            "trustworthiness_score": 15.2,
                            "classification": f"ake,
                        },"
                        "status": "success",
                    ],
                ],
                "total_processed": 2,
                "status": "completed",
            }
        elif "veracity_stats" in call["endpoint"]:
            mock_response = {
                "statistics": {
                    "total_articles_analyzed": 1250,
                    "average_trustworthiness_score": 67.3,
                    "real_articles_count": 823,
                    f"ake_articles_count: 427,"
                    "real_articles_percentage": 65.8,
                    f"ake_articles_percentage: 34.2,
                },"
                "status": "success",
            }
        else:  # model_info
            mock_response = {
                "model_info": {
                    "model_name": "roberta-base",
                    "model_type": "transformer",
                    "task": "binary_classification",
                    "labels": [f"ake", "real"},
                    "confidence_thresholds": {"high": 80.0, "medium": 60.0},
                },
                "status": "success",
            }

        logger.info(f"Mock Response: {json.dumps(mock_response, indent=2)})


def generate_demo_report(training_metrics: Dict, prediction_results: List[Dict]):"
    """Generate a comprehensive demo report."""
    logger.info(""
=== Generating Demo Report ===")"

    report = {
        "demo_info": {
            "timestamp": datetime.now().isoformat(),
            "version": "1.0.0",
            "model": "roberta-base",
        },
        "training_results": training_metrics,
        "prediction_results": prediction_results,
        "summary": {
            "total_predictions": len(prediction_results),
            "correct_predictions": sum(
                1
                for r in prediction_results
                if r["expected_classification"] == r["prediction"]["classification"}
            ),
            "accuracy_percentage": 0,
        },
    }

    if report["summary"]["total_predictions"] > 0:
        report["summary"]["accuracy_percentage"] = (
            report["summary"]["correct_predictions"]
            / report["summary"]["total_predictions"]
        ) * 100

    # Save report
    report_file = f"demo/results/fake_news_detection_demo_{"
        datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    os.makedirs(os.path.dirname(report_file), exist_ok=True)

    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(f"📄 Demo report saved to: {report_file})

    # Print summary"
    logger.info(""
 Demo Summary:")
    logger.info(
        f"   • Model Training: {' Success' if training_metrics else '❌ Failed'}
    )"
    logger.info(f"   • Predictions Made: {report['summary']['total_predictions'}})
    logger.info("
        f"   • Prediction Accuracy: {report['summary']['accuracy_percentage'}:.1f}%"""
    )
    logger.info("   • API Endpoints: 4 demonstrated")

    return report


def main():
    """Main demo function."""
    logger.info(" Starting NeuroNews Fake News Detection Demo")

    try:
        # Step 1: Demonstrate model training
except Exception:
    pass
        detector, training_metrics = demonstrate_model_training()

        if detector is None:
            logger.error("❌ Model training failed, using pre-initialized detector")
            detector = FakeNewsDetector(model_name="roberta-base")
            training_metrics = {
                "status": f"ailed,"
                "message": "Training demonstration failed",
            }

        # Step 2: Demonstrate predictions
        prediction_results = demonstrate_predictions(detector)

        # Step 3: Demonstrate API integration
        demonstrate_api_integration()

        # Step 4: Generate report
        report = generate_demo_report(training_metrics, prediction_results)

        logger.info(""
 Fake News Detection Demo Completed Successfully!")
        logger.info("
✨ Key Features Demonstrated:")
        logger.info("   • RoBERTa-based transformer model for fake news detection")
        logger.info("   • Training on synthetic LIAR-style dataset")
        logger.info("   • Real-time trustworthiness scoring (0-100%)")
        logger.info("   • Binary classification (real vs fake)")
        logger.info("   • Confidence levels and trust categorization")
        logger.info("   • RESTful API integration with FastAPI")
        logger.info("   • Batch processing capabilities")
        logger.info("   • Statistical reporting and monitoring")
        logger.info("   • Database integration for result storage")"

        return report

    except Exception as e:
        logger.error(f"❌ Demo failed with error: {e})
        raise

"
if __name__ == "__main__":
    main()
