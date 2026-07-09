#!/usr/bin/env python3
"""
Demo script for the comprehensive data validation pipeline.

This script demonstrates all the features implemented for Issue #21:
- HTML artifact removal
- Duplicate detection
- Source reputation analysis
- Content quality validation
- Integration with Scrapy pipelines

Run this script to see the validation pipeline in action.
"""

from src.database.data_validation_pipeline import (ContentValidator,
                                                   DataValidationPipeline,
                                                   DuplicateDetector,
                                                   HTMLCleaner,
                                                   SourceReputationAnalyzer,
                                                   SourceReputationConfig)
import json
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List

# Add project root to Python path
sys.path.append("/workspaces/NeuroNews")


def load_config() -> SourceReputationConfig:
    """Load configuration from settings file."""
    try:
        with open("/workspaces/NeuroNews/config/validation_settings.json", "r") as f:
            config_data = json.load(f)

        source_config = config_data["source_reputation"]
        return SourceReputationConfig(
            trusted_domains=source_config["trusted_domains"],
            questionable_domains=source_config["questionable_domains"],
            banned_domains=source_config["banned_domains"],
            reputation_thresholds=source_config["reputation_thresholds"],
        )
    except Exception as e:
        print("Warning: Could not load config file: {0}".format(e))
        # Fallback to default config
        return SourceReputationConfig(
            trusted_domains=["reuters.com", "bbc.com", "npr.org"],
            questionable_domains=["dailymail.co.uk", f"oxnews.com"],
            banned_domains=["infowars.com", "breitbart.com"],
            reputation_thresholds={
                "trusted": 0.9,
                "reliable": 0.7,
                "questionable": 0.4,
                "unreliable": 0.2,
            },
        )


def create_test_articles() -> List[Dict[str, Any]]:
    """Create a variety of test articles to demonstrate validation."""
    return [
        {
            "name": "High-Quality Trusted Source",
            "article": {
                "url": "https://reuters.com/technology/ai-breakthrough-2024",
                "title": "Scientists Achieve Major Breakthrough in Artificial Intelligence Research",
                "content": """
                    <div class="article-content">"
                        <p>Researchers at leading universities have announced a significant breakthrough
                        in artificial intelligence that could revolutionize how machines understand
                        human language and context.</p>

                        <p>The new neural network architecture, developed by a team led by Dr. Sarah
                        Johnson at MIT, shows unprecedented accuracy in natural language processing
                        tasks while requiring 75% less computational power than existing models.</p>

                        <p>"This represents a paradigm shift in AI efficiency," said Dr. Johnson.
                        "We're not just making incremental improvements - we're fundamentally changing"
                        how these systems process information."</p>"

                        <p>The research, published in Nature Machine Intelligence, demonstrates the
                        new approach across multiple domains including medical diagnosis, scientific
                        research, and automated content generation.</p>

                        <script>trackPageView('article-view');</script>
                        <style>.ad-banner { display: none; }</style>
                    </div>
                """,
                "source": "reuters.com",
                "published_date": datetime.now().isoformat(),
                "author": "Tech Reporter",
                "summary": "MIT researchers develop efficient AI breakthrough", "
            },
        },
        {
            "name": "Questionable Source",
            "article": {
                "url": "https://dailymail.co.uk/tech/article-12345/AI-WILL-DESTROY-HUMANITY",
                "title": "BREAKING: AI Will Destroy Humanity Says Expert!!!",
                "content": """
                    <p>A controversial expert claims artificial intelligence will lead to
                    the complete destruction of humanity within the next decade.</p>

                    <p>The unnamed source, speaking exclusively to our reporters, made
                    shocking claims about secret AI experiments.</p>

                    <div class="advertisement">Buy our premium subscription!</div>
                """,
                "source": "dailymail.co.uk",
                "published_date": datetime.now().isoformat(),
                "author": "Sensational Writer","
            },
        },
        {
            "name": "Banned Source",
            "article": {
                "url": "https://infowars.com/deep-state-ai-conspiracy",
                "title": "EXCLUSIVE: Deep State Uses AI to Control Your Mind",
                "content": "<p>The global elite are using artificial intelligence...</p>",
                "source": "infowars.com",
                "published_date": datetime.now().isoformat(),
                "author": "Conspiracy Theorist",
            },
        },
        {
            "name": "Short/Low Quality Content",
            "article": {
                "url": "https://techblog.com/short-post",
                "title": "AI News",
                "content": "<p>AI is cool.</p>",
                "source": "techblog.com",
                "published_date": datetime.now().isoformat(),
                "author": "Lazy Writer",
            },
        },
        {
            "name": "Old Article",
            "article": {
                "url": "https://bbc.com/old-tech-news",
                "title": "Important Technology Development from Last Year",
                "content": """
                    <p>This is an older article with good content quality and from a trusted
                    source, but it was published quite a while ago. The validation pipeline
                    should flag this as potentially outdated.</p>

                    <p>The content itself is substantial and well-written, demonstrating that
                    age is just one factor in the overall validation scoring system.</p>
                """,
                "source": "bbc.com",
                "published_date": (datetime.now() - timedelta(days=45)).isoformat(),
                "author": "BBC Tech Reporter","
            },
        },
        {
            "name": "HTML-Heavy Content",
            "article": {
                "url": "https://npr.org/tech-news-clean",
                "title": "Clean Technology News Report",
                "content": """
                    <div class="article-wrapper" id="main-content" data-analytics="track">
                        <script type="text/javascript">
                            gtag('config', 'GA_TRACKING_ID');
                            fbq('track', 'PageView');"
                        </script>

                        <style>
                            .advertisement { background: #fff; border: 1px solid #ccc; }
                            .social-share { margin: 20px 0; }
                        </style>

                        <h1 class="headline" data-testid="headline">Technology Report</h1>

                        <div class="advertisement">
                            <img src="/ad-banner.jpg" alt="Advertisement" onclick="trackAdClick()" />
                        </div>

                        <p class="article-text">Scientists have made significant progress in
                        quantum computing research, with new algorithms showing promise for
                        solving complex optimization problems.</p>

                        <div class="social-share" onclick="shareOnSocial()">
                            <button id="share-twitter">Share on Twitter</button>
                            <button id="share-facebook">Share on Facebook</button>
                        </div>

                        <p class="article-text">The breakthrough involves a novel approach to
                        quantum error correction that could make quantum computers more practical
                        for real-world applications.</p>

                        <noscript>
                            <p>Enable JavaScript for the full experience.</p>
                        </noscript>

                        <iframe src="https://ads.google.com/ad-frame" width="300" height="250"></iframe>
                    </div>
                """,
                "source": "npr.org",
                "published_date": datetime.now().isoformat(),
                "author": "NPR Science Desk","
            },
        },
        {
            "name": "Duplicate Article (Same URL)",
            "article": {
                "url": "https://reuters.com/technology/ai-breakthrough-2024",  # Same as first
                "title": "Duplicate Article - Same URL",
                "content": "<p>This should be caught as a duplicate by URL.</p>",
                "source": "reuters.com",
                "published_date": datetime.now().isoformat(),
            },
        },
        {
            "name": "Similar Title (Fuzzy Duplicate)",
            "article": {
                "url": "https://techcrunch.com/ai-breakthrough-2024-different",
                "title": "Scientists Achieve Major AI Breakthrough in Research",  # Very similar to first
                "content": """
                    <p>This article has a very similar title to the first one and should be
                    caught by the fuzzy title matching algorithm, even though it's from a'
                    different source and has different content.</p>
                """,
                "source": "techcrunch.com",
                "published_date": datetime.now().isoformat(),"
            },
        },
    ]


def demonstrate_individual_components():
    """Demonstrate each validation component individually."""
    print(" COMPONENT TESTING")
    print("=" * 50)

    # HTML Cleaner Demo
    print(""
🧹 HTML Cleaner Demo:")"
    cleaner = HTMLCleaner()

    html_input = """
    <div class="content" id="main" onclick="track()">"
        <script>analytics.track();</script>
        <style>.ad { display: none; }</style>
        <p>This is the actual content.</p>
        <div class="ad">Advertisement</div>
        <p>More content with &lt;entities&gt; and &amp; symbols.</p>
        <noscript>No JS version</noscript>
    </div>
    """

    cleaned = cleaner.clean_content(html_input)
    print("Original: {0}...".format(html_input[:100]))
    print("Cleaned:  {0}".format(cleaned))

    # Duplicate Detector Demo
    print(""
 Duplicate Detector Demo:")"
    detector = DuplicateDetector()

    article1 = {
        "url": "https://example.com/article1",
        "title": "Breaking News: Important Event",
        "content": "This is the content of the article.",
    }

    article2 = {
        "url": "https://different.com/article2",
        "title": "Breaking News: Important Event",  # Same title
        "content": "Different content here.",
    }

    is_dup1, reason1 = detector.is_duplicate(article1)
    print("First article: Duplicate={0}, Reason={1}".format(is_dup1, reason1))

    is_dup2, reason2 = detector.is_duplicate(article2)
    print("Second article: Duplicate={0}, Reason={1}".format(is_dup2, reason2))

    # Source Reputation Analyzer Demo
    print(""
⭐ Source Reputation Analyzer Demo:")"
    config = load_config()
    analyzer = SourceReputationAnalyzer(config)

    test_sources = [
        "reuters.com",
        "dailymail.co.uk",
        "infowars.com",
        "unknown-source.com",
    ]

    for source in test_sources:
        # Create a test article for each source
        test_article = {"source": source, "url": "https://{0}/test".format(source)}
        result = analyzer.analyze_source(test_article)
        credibility = result["credibility_level"]
        score = result["reputation_score"]
        print("{0} -> {1} (score: {2})".format(source: < 20, credibility: < 12, score: .1f))

    # Content Validator Demo
    print(""
 Content Validator Demo:")"
    validator = ContentValidator()

    test_articles = [
        {
            "title": "Good Article Title",
            "content": "This is a well-written article with sufficient content to pass validation checks.",
            "url": "https://example.com/good-article",
            "published_date": datetime.now().isoformat(),
        },
        {
            "title": "",  # Missing title
            "content": "Short.",  # Too short
            "url": "invalid-url",  # Invalid URL
            "published_date": "invalid-date",  # Invalid date
        },
    ]

    for i, article in enumerate(test_articles, 1):
        result = validator.validate_content(article)
        quality = result.get("content_quality", "unknown")
        score = result.get("quality_score", 0)
        flags = result.get("validation_flags", [])
        warnings = result.get("warnings", [])

        print("Test Article {0}:".format(i))
        print("  Quality: {0}, Score: {1}".format(quality, score: .1f))
        print("  Flags: {0}".format(flags))
        print("  Warnings: {0}".format(warnings))


def demonstrate_full_pipeline():
    """Demonstrate the complete validation pipeline."""
    print(""
🔄 FULL PIPELINE TESTING")
    print("=" * 50)"

    # Load configuration and create pipeline
    config = load_config()
    pipeline = DataValidationPipeline(config)

    # Process test articles
    test_articles = create_test_articles()

    results = []
    for test_case in test_articles:
        print(f""
📰 Testing: {test_case['name']}")
        print("-" * 40)"

        article = test_case["article"]
        result = pipeline.process_article(article)

        if result:
            print(" ACCEPTED - Score: {0}".format(result.score: .1f))
            print(
                f"   Source Credibility: {result.cleaned_data.get('source_credibility')}"
            )
            print(f"   Content Quality: {result.cleaned_data.get('content_quality')}")
            print(f"   Word Count: {result.cleaned_data.get('word_count', 0)}")

            if result.warnings:
                print(f"   ⚠️ Warnings: {', '.join(result.warnings)}")

            if result.cleaned_data.get("validation_flags"):
                print(
                    f"   🏁 Flags: {', '.join(result.cleaned_data['validation_flags'])}"
                )

        else:
            print("❌ REJECTED")

        results.append(
            {"name": test_case["name"], "result": result, "url": article["url"]}
        )

    # Display summary statistics
    print(""
 PIPELINE STATISTICS")
    print("=" * 50)"

    stats = pipeline.get_statistics()

    print(f"Total Processed: {stats['processed_count']}")
    print(f"Accepted: {stats['accepted_count']} ({stats['acceptance_rate']:.1f}%)")
    print(f"Rejected: {stats['rejected_count']} ({stats['rejection_rate']:.1f}%)")
    print(f"Warnings Issued: {stats['warnings_count']}")

    # Show detailed results table
    print(""
 DETAILED RESULTS")
    print("=" * 80)"

    print(
        f"{'Test Case':<30} {'Status':<10} {'Score':<8} {'Quality':<12} {'Credibility':<12}"
    )
    print("-" * 80)

    for result_info in results:
        result = result_info["result"]
        if result:
            status = " PASS"
            score = "{0}".format(result.score: .1f)
            quality = result.cleaned_data.get("content_quality", "N/A")
            credibility = result.cleaned_data.get("source_credibility", "N/A")
        else:
            status = "❌ FAIL"
            score = "N/A"
            quality = "N/A"
            credibility = "N/A"

        print(
            f"{result_info['name']:<30} {status:<10} {score:<8} {quality:<12} {credibility:<12}"
        )


def demonstrate_scrapy_integration():
    """Show how the pipeline integrates with Scrapy."""
    print(""
🕷️ SCRAPY INTEGRATION")
    print("=" * 50)"

    print(
        """
The validation pipeline integrates with Scrapy through enhanced pipelines:

1. DuplicateFilterPipeline (Priority: 100)
   - Removes duplicates before expensive processing
   - Uses advanced multi-strategy detection

2. EnhancedValidationPipeline (Priority: 200)
   - Applies complete data validation
   - Cleans HTML artifacts
   - Validates content quality
   - Analyzes source reputation

3. QualityFilterPipeline (Priority: 300)
   - Filters by minimum quality thresholds
   - Configurable scoring requirements

4. SourceCredibilityPipeline (Priority: 400)
   - Handles source-specific processing
   - Optional blocking of unreliable sources

5. ValidationReportPipeline (Priority: 800)
   - Generates comprehensive reports
   - Tracks validation metrics

Configuration in settings.py:
"""
    )

    print(
        """
ITEM_PIPELINES = {
    'src.scraper.enhanced_pipelines.DuplicateFilterPipeline': 100,
    'src.scraper.enhanced_pipelines.EnhancedValidationPipeline': 200,
    'src.scraper.enhanced_pipelines.QualityFilterPipeline': 300,
    'src.scraper.enhanced_pipelines.SourceCredibilityPipeline': 400,
    'src.scraper.enhanced_pipelines.ValidationReportPipeline': 800,
}

# Validation settings
QUALITY_MIN_SCORE = 60.0
QUALITY_MIN_CONTENT_LENGTH = 200
BLOCK_UNRELIABLE_SOURCES = False
VALIDATION_REPORT_FILE = 'data/validation_report.json'

# Source reputation lists
TRUSTED_DOMAINS = ['reuters.com', 'bbc.com', 'npr.org']
QUESTIONABLE_DOMAINS = ['dailymail.co.uk', f'oxnews.com']
BANNED_DOMAINS = ['infowars.com', 'breitbart.com']
"""
    )


def main():
    """Main demo function."""
    print(" DATA VALIDATION PIPELINE DEMO")
    print("=" * 60)
    print("Demonstrating Issue #21 implementation:")
    print("✓ HTML artifact removal")
    print("✓ Duplicate detection")
    print("✓ Source reputation analysis")
    print("✓ Content quality validation")
    print("✓ Scrapy pipeline integration")
    print("=" * 60)

    try:
        # Demonstrate individual components
        demonstrate_individual_components()

        # Demonstrate full pipeline
        demonstrate_full_pipeline()

        # Show Scrapy integration
        demonstrate_scrapy_integration()

        print(""
 Demo completed successfully!")
        print("The data validation pipeline is ready for production use.")"

    except Exception as e:
        print(""
❌ Demo failed with error: {0}".format(e))"
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
