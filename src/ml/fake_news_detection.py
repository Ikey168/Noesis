"""
AI-based fake news detection module using transformer models.

This module provides functionality to detect fake news using pre-trained
transformer models like RoBERTa or DeBERTa, with high accuracy and
confidence scoring.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

# torch / transformers are imported lazily inside the methods that need them, so
# importing this module (e.g. to reference the class or for collection) does not
# pull the heavy ML stack. Model construction still loads a model on demand.

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class FakeNewsDetector:
    """
    AI-powered fake news detection using transformer models.

    This class provides methods to analyze news articles and determine
    their veracity using state-of-the-art NLP models.
    """

    def __init__(
        self,
        model_name: str = "hamzelou/fake-news-bert",
        config_path: Optional[str] = None,
    ):
        """
        Initialize the fake news detector.

        Args:
            model_name: Name or path of the pre-trained model
            config_path: Path to configuration file
        """
        self.model_name = model_name
        self.device = None  # resolved lazily in _initialize_model (needs torch)

        # Load configuration
        self.config = self._load_config(config_path)

        # Initialize model and tokenizer
        self.tokenizer = None
        self.model = None
        self.classifier = None

        # Model metadata
        self.model_version = "roberta-fake-news-v1.0"
        self.confidence_threshold = self.config.get("confidence_threshold", 0.7)
        self.max_length = self.config.get("max_length", 512)

        # Initialize the model
        self._initialize_model()

        logger.info("FakeNewsDetector initialized with model: {0}".format(model_name))

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        """Load configuration from file or use defaults."""
        default_config = {
            "confidence_threshold": 0.7,
            "max_length": 512,
            "batch_size": 16,
        }

        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    config = json.load(f)
                    return config.get("fake_news_detection", default_config)
            except Exception as e:
                logger.warning(
                    "Could not load config from {0}: {1}".format(config_path, e)
                )

        return default_config

    def _initialize_model(self):
        """Initialize the transformer model and tokenizer.

        torch / transformers are imported here (not at module load) so importing
        the module stays dependency-light; they are required only to build a
        detector. If they are unavailable, fall through to the rule-based path.
        """
        try:
            import torch
            from transformers import (
                AutoModelForSequenceClassification,
                AutoTokenizer,
                pipeline,
            )
        except Exception as exc:  # torch/transformers not installed
            logger.warning("ML deps unavailable (%s); using rule-based fallback", exc)
            self.classifier = None
            self.model = None
            return

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        try:
            # Try to use a pipeline first for simplicity
            self.classifier = pipeline(
                "text-classification",
                model=self.model_name,
                tokenizer=self.model_name,
                device=0 if torch.cuda.is_available() else -1,
                return_all_scores=True,
            )
            logger.info("Model initialized successfully with pipeline")

        except Exception as e:
            logger.warning("Pipeline initialization failed: {0}".format(e))

            # Fallback to manual model loading
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
                self.model = AutoModelForSequenceClassification.from_pretrained(
                    self.model_name
                )
                self.model.to(self.device)
                self.model.eval()
                logger.info("Model initialized successfully with manual loading")

            except Exception as e2:
                logger.error("Manual model loading also failed: {0}".format(e2))
                # Use a simple rule-based fallback
                self.classifier = None
                self.model = None
                logger.warning("Using rule-based fallback for fake news detection")

    def predict_veracity(
        self, title: str, content: str, include_explanation: bool = True
    ) -> Dict[str, Any]:
        """
        Predict whether a news article is real or fake.

        Args:
            title: Article title
            content: Article content
            include_explanation: Whether to include explanation

        Returns:
            Dictionary containing prediction results
        """
        try:
            # Combine title and content
            text = "{0}. {1}".format(title, content)

            # Truncate if too long
            if len(text) > self.max_length * 4:  # Rough character estimate
                text = text[: self.max_length * 4]

            if self.classifier:
                # Use transformer model
                result = self._predict_with_transformer(text)
            else:
                # Use rule-based fallback
                result = self._predict_with_rules(title, content)

            # Add metadata
            result.update(
                {
                    "model_version": self.model_version,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
            )

            if include_explanation:
                result["explanation"] = self._generate_explanation(
                    title, content, result
                )

            return result

        except Exception as e:
            logger.error("Error in predict_veracity: {0}".format(e))
            return self._get_fallback_result()

    def _predict_with_transformer(self, text: str) -> Dict[str, Any]:
        """Predict using transformer model."""
        try:
            # Get predictions
            predictions = self.classifier(text)

            # Process results
            if isinstance(predictions[0], list):
                # Multiple scores returned
                scores = {pred["label"]: pred["score"] for pred in predictions[0]}

                # Determine if it's real or fake
                # This depends on the model's label mapping
                fake_score = scores.get(
                    "FAKE", scores.get("fake", scores.get("1", 0.5))
                )
                real_score = scores.get(
                    "REAL", scores.get("real", scores.get("0", 0.5))
                )

                is_real = real_score > fake_score
                confidence = max(real_score, fake_score)

            else:
                # Single prediction
                pred = predictions[0]
                label = pred["label"].upper()
                is_real = label in ["REAL", "TRUE", "0"]
                confidence = pred["score"]

            return {
                "is_real": is_real,
                "confidence": float(confidence),
                "raw_scores": (
                    predictions[0] if isinstance(predictions[0], list) else predictions
                ),
            }

        except Exception as e:
            logger.error("Transformer prediction error: {0}".format(e))
            return self._get_fallback_result()

    def _predict_with_rules(self, title: str, content: str) -> Dict[str, Any]:
        """Rule-based fallback prediction."""

        # Simple heuristic rules
        fake_indicators = [
            "aliens",
            "miracle cure",
            "doctors hate",
            "secret trick",
            "shocking truth",
            "they don't want you to know",
            "click here",
            "you won't believe",
            "instant cure",
            "free money",
            "get rich quick",
            "amazing discovery",
        ]

        real_indicators = [
            "study shows",
            "research",
            "university",
            "published",
            "according to",
            "expert",
            "analysis",
            "data",
            "statistics",
            "peer-reviewed",
            "journal",
            "scientist",
        ]

        text = (title + " " + content).lower()

        fake_score = sum(1 for indicator in fake_indicators if indicator in text)
        real_score = sum(1 for indicator in real_indicators if indicator in text)

        # Simple scoring
        if fake_score > real_score:
            is_real = False
            confidence = min(0.6 + (fake_score * 0.1), 0.9)
        elif real_score > fake_score:
            is_real = True
            confidence = min(0.6 + (real_score * 0.1), 0.9)
        else:
            # Neutral - lean towards real
            is_real = True
            confidence = 0.5

        return {
            "is_real": is_real,
            "confidence": float(confidence),
            "method": "rule_based",
        }

    def _generate_explanation(
        self, title: str, content: str, result: Dict[str, Any]
    ) -> str:
        """Generate human-readable explanation for the prediction."""

        is_real = result["is_real"]
        confidence = result["confidence"]

        if confidence > 0.8:
            certainty = "high confidence"
        elif confidence > 0.6:
            certainty = "moderate confidence"
        else:
            certainty = "low confidence"

        if is_real:
            base_explanation = "This article appears to be REAL with {0}".format(
                certainty
            )

            if "study" in content.lower() or "research" in content.lower():
                base_explanation += ". Content references research or studies"
            elif "expert" in content.lower() or "university" in content.lower():
                base_explanation += ". Content cites experts or academic sources"
            else:
                base_explanation += ". Content appears factual and credible"
        else:
            base_explanation = "This article appears to be FAKE with {0}".format(
                certainty
            )

            if any(
                word in (title + content).lower()
                for word in ["miracle", "shocking", "secret"]
            ):
                base_explanation += ". Content contains sensational language"
            elif "instant" in content.lower() or "immediate" in content.lower():
                base_explanation += ". Content makes unrealistic claims"
            else:
                base_explanation += (
                    ". Content lacks credible sources or contains questionable claims"
                )

        return base_explanation

    def _get_fallback_result(self) -> Dict[str, Any]:
        """Get fallback result when prediction fails."""
        return {
            "is_real": True,  # Conservative approach
            "confidence": 0.5,
            "error": "Prediction failed, using conservative fallback",
            "model_version": self.model_version,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

    def batch_predict(self, articles: list) -> list:
        """
        Predict veracity for multiple articles.

        Args:
            articles: List of dicts with 'title' and 'content' keys

        Returns:
            List of prediction results
        """
        results = []

        for article in articles:
            try:
                result = self.predict_veracity(
                    title=article.get("title", ""), content=article.get("content", "")
                )
                result["article_id"] = article.get("id")
                results.append(result)

            except Exception as e:
                logger.error(
                    f"Error processing article {article.get('id')}: {e}"
                )
                results.append(self._get_fallback_result())

        return results

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model."""
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "device": str(self.device),
            "confidence_threshold": self.confidence_threshold,
            "max_length": self.max_length,
            "available": self.classifier is not None or self.model is not None,
        }
