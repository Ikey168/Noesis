"""
AsyncIO-based news scraper for NeuroNews.
This module provides high-performance async scraping with Playwright and ThreadPoolExecutor.
"""

import asyncio
import json
import logging
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiofiles
import aiohttp

# Performance monitoring
import psutil

# Playwright imports
from playwright.async_api import BrowserContext, async_playwright


@dataclass
class Article:
    """Article data structure."""

    title: str
    url: str
    content: str
    author: str
    published_date: str
    source: str
    scraped_date: str
    language: str = "en"
    content_length: int = 0
    word_count: int = 0
    reading_time: int = 0
    category: str = "News"
    validation_score: int = 0
    content_quality: str = "unknown"
    duplicate_check: str = "unique"


@dataclass
class NewsSource:
    """News source configuration."""

    name: str
    base_url: str
    article_selectors: Dict[str, str]
    link_patterns: List[str]
    requires_js: bool = False
    rate_limit: float = 1.0
    timeout: int = 30


class PerformanceMonitor:
    """Real-time performance monitoring for async scraper."""

    def __init__(self):
        self.start_time = time.time()
        self.articles_scraped = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.source_stats = defaultdict(lambda: {"articles": 0, "errors": 0})
        self.response_times = deque(maxlen=100)
        self.memory_usage = deque(maxlen=50)
        self.cpu_usage = deque(maxlen=50)
        self.lock = threading.Lock()

        # Start monitoring thread
        self.monitoring = True
        self.monitor_thread = threading.Thread(target=self._monitor_system, daemon=True)
        self.monitor_thread.start()

    def _monitor_system(self):
        """Monitor system resources."""
        while self.monitoring:
            try:
                process = psutil.Process()
                with self.lock:
                    self.memory_usage.append(
                        process.memory_info().rss / 1024 / 1024
                    )  # MB
                    self.cpu_usage.append(process.cpu_percent())
                time.sleep(1)
            except BaseException:
                pass

    def record_article(self, source: str):
        """Record a successfully scraped article."""
        with self.lock:
            self.articles_scraped += 1
            self.source_stats[source]["articles"] += 1

    def record_success(self, response_time: float):
        """Record a successful request."""
        with self.lock:
            self.successful_requests += 1
            self.response_times.append(response_time)

    def record_error(self, source: str):
        """Record a failed request."""
        with self.lock:
            self.failed_requests += 1
            self.source_stats[source]["errors"] += 1

    def get_stats(self) -> Dict[str, Any]:
        """Get current performance statistics."""
        with self.lock:
            elapsed_time = time.time() - self.start_time

            return {
                "elapsed_time": elapsed_time,
                "articles_per_second": self.articles_scraped / max(elapsed_time, 1),
                "total_articles": self.articles_scraped,
                "successful_requests": self.successful_requests,
                "failed_requests": self.failed_requests,
                "success_rate": self.successful_requests
                / max(self.successful_requests + self.failed_requests, 1)
                * 100,
                "avg_response_time": sum(self.response_times)
                / max(len(self.response_times), 1),
                "avg_memory_mb": sum(self.memory_usage)
                / max(len(self.memory_usage), 1),
                "avg_cpu_percent": sum(self.cpu_usage) / max(len(self.cpu_usage), 1),
                "source_stats": dict(self.source_stats),
            }

    def stop(self):
        """Stop monitoring."""
        self.monitoring = False


class AsyncNewsScraperEngine:
    """High-performance async news scraper with Playwright and ThreadPoolExecutor."""

    def __init__(
        self,
        max_concurrent: int = 10,
        max_threads: int = 4,
        headless: bool = True,
        proxy_config_path: Optional[str] = None,
        proxy_config: Optional[dict] = None,
        use_tor: bool = False,
        captcha_api_key: Optional[str] = None,
        cloudwatch_region: str = "us-east-1",
        cloudwatch_namespace: str = "NeuroNews/Scraper",
        dynamodb_table: str = "neuronews-failed-urls",
        sns_topic_arn: Optional[str] = None,
        enable_monitoring: bool = True,
    ):
        self.max_concurrent = max_concurrent
        self.max_threads = max_threads
        self.headless = headless
        self.proxy_config = proxy_config
        self.use_tor = use_tor
        self.captcha_api_key = captcha_api_key
        self.enable_monitoring = enable_monitoring

        # Performance monitoring
        self.monitor = PerformanceMonitor()

        # Rate limiting
        self.semaphores = {}

        # Monitoring and error handling components
        self.cloudwatch_logger = None
        self.failure_manager = None
        self.alert_manager = None
        self.retry_manager = None

        if enable_monitoring:
            # Initialize monitoring components
            from .cloudwatch_logger import CloudWatchLogger
            from .dynamodb_failure_manager import DynamoDBFailureManager
            from .enhanced_retry_manager import EnhancedRetryManager, RetryConfig
            from .sns_alert_manager import SNSAlertManager

            self.cloudwatch_logger = CloudWatchLogger(
                region_name=cloudwatch_region, namespace=cloudwatch_namespace
            )

            self.failure_manager = DynamoDBFailureManager(
                table_name=dynamodb_table, region_name=cloudwatch_region
            )

            if sns_topic_arn:
                self.alert_manager = SNSAlertManager(
                    topic_arn=sns_topic_arn, region_name=cloudwatch_region
                )

            # Enhanced retry manager with monitoring integration
            self.retry_manager = EnhancedRetryManager(
                cloudwatch_logger=self.cloudwatch_logger,
                failure_manager=self.failure_manager,
                alert_manager=self.alert_manager,
                retry_config=RetryConfig(max_retries=3, base_delay=2.0),
            )

        # Proxy manager
        self.proxy_manager = None
        if proxy_config_path:
            from .proxy_manager import ProxyRotationManager

            self.proxy_manager = ProxyRotationManager(config_path=proxy_config_path)
        elif proxy_config:
            from .proxy_manager import ProxyConfig

            self.proxy = ProxyConfig(**proxy_config)
        else:
            self.proxy = None

        self.tor_manager = None
        if use_tor:
            from .tor_manager import TorManager

            self.tor_manager = TorManager()

        # User agent rotator
        from .user_agent_rotator import UserAgentRotator

        self.user_agent_rotator = UserAgentRotator()

        # CAPTCHA solver
        self.captcha_solver = None
        if captcha_api_key:
            from .captcha_solver import CaptchaSolver

            self.captcha_solver = CaptchaSolver(captcha_api_key)

        # Browser management
        self.playwright = None
        self.browser = None
        self.browser_contexts = []

        # Thread pool for CPU-intensive tasks
        self.thread_pool = ThreadPoolExecutor(max_workers=max_threads)

        # Session management
        self.session = None

        # Results storage
        self.articles: List[Article] = []
        self.articles_lock = asyncio.Lock()

        # Deduplication
        self.seen_urls: Set[str] = set()

        # Setup logging
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)

        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    async def __aenter__(self):
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    async def start(self):
        """Initialize async components."""
        # Initialize monitoring components if enabled
        if self.enable_monitoring:
            if self.cloudwatch_logger:
                self.logger.info("Initializing CloudWatch logging...")
            if self.failure_manager:
                self.logger.info("Initializing DynamoDB failure tracking...")
            if self.alert_manager:
                self.logger.info("Initializing SNS alerting...")
                # Test alert system
                try:
                    await self.alert_manager.test_alert_system()
                    self.logger.info("SNS alert system test successful")
                except Exception as e:
                    self.logger.warning("SNS alert system test failed: {0}".format(e))

        # Proxy setup
        connector = None
        proxy = None

        # Get proxy configuration
        if self.proxy_manager:
            # Use proxy rotation manager
            proxy_config = await self.proxy_manager.get_proxy()
            if proxy_config:
                proxy = "{0}://{1}:{2}".format(
                    proxy_config.proxy_type, proxy_config.host, proxy_config.port
                )
                if proxy_config.username:
                    proxy = "{0}://{1}:{2}@{3}:{4}".format(
                        proxy_config.proxy_type,
                        proxy_config.username,
                        proxy_config.password,
                        proxy_config.host,
                        proxy_config.port,
                    )
        elif self.use_tor and self.tor_manager:
            proxy = self.tor_manager.get_proxy_url()
        elif self.proxy:
            proxy = "{0}://{1}:{2}".format(
                self.proxy.proxy_type, self.proxy.host, self.proxy.port
            )

        # Setup connector
        connector = aiohttp.TCPConnector(
            limit=self.max_concurrent * 2, ssl=False if proxy else None
        )

        timeout = aiohttp.ClientTimeout(total=30)

        # Get initial headers from user agent rotator
        initial_headers = self.user_agent_rotator.get_random_headers()

        # Create session with proxy if available
        session_kwargs = {
            "connector": connector,
            "timeout": timeout,
            "headers": initial_headers,
        }

        if proxy:
            session_kwargs["trust_env"] = True

        self.session = aiohttp.ClientSession(**session_kwargs)

        # Initialize Playwright for JS-heavy sites
        self.playwright = await async_playwright().start()
        browser_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        if proxy:
            browser_args.append("--proxy-server={0}".format(proxy))
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless, args=browser_args
        )

        # Create browser contexts for parallel browsing with random user agents
        for i in range(min(self.max_concurrent // 2, 5)):
            headers = self.user_agent_rotator.get_random_headers()
            user_agent = headers.get(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            )
            context = await self.browser.new_context(
                user_agent=user_agent, extra_http_headers=headers
            )
            self.browser_contexts.append(context)

        # Start proxy health monitoring if using proxy manager
        if self.proxy_manager and hasattr(self.proxy_manager, "start_health_monitor"):
            asyncio.create_task(self.proxy_manager.start_health_monitor())

        self.logger.info(
            "AsyncNewsScraperEngine started with {0} concurrent connections".format(
                self.max_concurrent
            )
        )
        self.logger.info("Monitoring enabled: {0}".format(self.enable_monitoring))
        if proxy:
            self.logger.info(
                f"Using proxy: {proxy.split('@')[-1] if '@' in proxy else proxy}"
            )  # Hide credentials

    async def close(self):
        """Clean up resources."""
        # Flush any remaining monitoring data
        if self.enable_monitoring:
            if self.cloudwatch_logger:
                try:
                    await self.cloudwatch_logger.flush_metrics()
                    self.logger.info("Flushed remaining CloudWatch metrics")
                except Exception as e:
                    self.logger.error(
                        "Error flushing CloudWatch metrics: {0}".format(e)
                    )

            if self.failure_manager:
                try:
                    # Clean up old failures (optional, can be done periodically
                    # instead)
                    await self.failure_manager.cleanup_old_failures(days=30)
                    self.logger.info("Cleaned up old failure records")
                except Exception as e:
                    self.logger.error("Error cleaning up failures: {0}".format(e))

        if self.session:
            await self.session.close()

        for context in self.browser_contexts:
            await context.close()

        if self.browser:
            await self.browser.close()

        if self.playwright:
            await self.playwright.stop()

        self.thread_pool.shutdown(wait=True)
        self.monitor.stop()

        self.logger.info("AsyncNewsScraperEngine closed")

    def get_rate_limiter(self, source: NewsSource) -> asyncio.Semaphore:
        """Get or create rate limiter for source."""
        if source.name not in self.semaphores:
            # Convert rate limit to semaphore value
            max_concurrent = max(1, int(source.rate_limit * 2))
            self.semaphores[source.name] = asyncio.Semaphore(max_concurrent)
        return self.semaphores[source.name]

    async def scrape_sources_async(self, sources: List[NewsSource]) -> List[Article]:
        """Scrape multiple sources concurrently."""
        self.logger.info("Starting async scraping of {0} sources".format(len(sources)))

        # Create tasks for each source
        tasks = []
        for source in sources:
            task = asyncio.create_task(self.scrape_source_async(source))
            tasks.append(task)

        # Wait for all sources to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect all articles
        all_articles = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self.logger.error(
                    "Error scraping {0}: {1}".format(sources[i].name, str(result))
                )
                self.monitor.record_error(sources[i].name)
            else:
                all_articles.extend(result)

        self.logger.info(
            "Async scraping completed: {0} articles collected".format(len(all_articles))
        )
        return all_articles

    async def scrape_source_async(self, source: NewsSource) -> List[Article]:
        """Scrape a single source asynchronously."""
        rate_limiter = self.get_rate_limiter(source)

        async with rate_limiter:
            try:
                self.logger.info(
                    f"Scraping {source.name} ({'JS' if source.requires_js else 'HTTP'})"
                )

                if source.requires_js:
                    return await self.scrape_js_source(source)
                else:
                    return await self.scrape_http_source(source)

            except Exception as e:
                self.logger.error("Error scraping {0}: {1}".format(source.name, e))
                self.monitor.record_error(source.name)
                return []

    async def scrape_http_source(self, source: NewsSource) -> List[Article]:
        """Scrape source using async HTTP requests."""
        start_time = time.time()

        try:
            # Get article links
            links = await self.get_article_links_http(source)
            self.logger.info("Found {0} links for {1}".format(len(links), source.name))

            # Scrape articles concurrently
            semaphore = asyncio.Semaphore(self.max_concurrent)
            tasks = []

            for link in links:
                if link not in self.seen_urls:
                    self.seen_urls.add(link)
                    task = asyncio.create_task(
                        self.scrape_article_http(semaphore, source, link)
                    )
                    tasks.append(task)

            # Wait for all articles
            articles = await asyncio.gather(*tasks, return_exceptions=True)

            # Filter successful results
            valid_articles = [
                article for article in articles if isinstance(article, Article)
            ]

            # Record performance
            response_time = time.time() - start_time
            self.monitor.record_success(response_time)

            for article in valid_articles:
                self.monitor.record_article(source.name)

            return valid_articles

        except Exception as e:
            self.logger.error("HTTP scraping error for {0}: {1}".format(source.name, e))
            self.monitor.record_error(source.name)
            return []

    async def get_article_links_http(self, source: NewsSource) -> List[str]:
        """Get article links using HTTP request with proxy and user agent rotation."""
        try:
            # Rotate user agent
            headers = (
                self.user_agent_rotator.get_random_headers()
                if hasattr(self.user_agent_rotator, "get_random_headers")
                else {}
            )
            # Rotate proxy
            proxy = (
                self.tor_manager.get_proxy()
                if self.use_tor
                else (self.proxy.url if self.proxy else None)
            )
            async with self.session.get(
                source.base_url, headers=headers, proxy=proxy
            ) as response:
                if response.status == 200:
                    html = await response.text()
                    # CAPTCHA detection (simple placeholder)
                    if "captcha" in html.lower() and self.captcha_solver:
                        self.logger.warning(
                            "CAPTCHA detected on {0}, attempting to solve...".format(
                                source.base_url
                            )
                        )
                        # Here you would extract sitekey and call self.captcha_solver.solve_recaptcha_v2(...)
                        # For now, just log and skip
                        return []
                    # Use thread pool for CPU-intensive parsing
                    loop = asyncio.get_event_loop()
                    links = await loop.run_in_executor(
                        self.thread_pool, self.extract_links_from_html, html, source
                    )
                    return links
                else:
                    self.logger.warning(
                        "HTTP {0} for {1}".format(response.status, source.base_url)
                    )
                    return []
        except Exception as e:
            self.logger.error(
                "Error getting links from {0}: {1}".format(source.base_url, e)
            )
            return []

    async def scrape_article_http(
        self, semaphore: asyncio.Semaphore, source: NewsSource, url: str
    ) -> Optional[Article]:
        """Scrape individual article using HTTP."""
        async with semaphore:
            try:
                # Rotate user agent
                headers = (
                    self.user_agent_rotator.get_random_headers()
                    if hasattr(self.user_agent_rotator, "get_random_headers")
                    else {}
                )
                # Rotate proxy
                proxy = (
                    self.tor_manager.get_proxy()
                    if self.use_tor
                    else (self.proxy.url if self.proxy else None)
                )
                async with self.session.get(
                    url, headers=headers, proxy=proxy
                ) as response:
                    if response.status == 200:
                        html = await response.text()
                        # CAPTCHA detection (simple placeholder)
                        if "captcha" in html.lower() and self.captcha_solver:
                            self.logger.warning(
                                "CAPTCHA detected on {0}, attempting to solve...".format(
                                    url
                                )
                            )
                            # Here you would extract sitekey and call self.captcha_solver.solve_recaptcha_v2(...)
                            # For now, just log and skip
                            return None
                        # Use thread pool for parsing
                        loop = asyncio.get_event_loop()
                        article = await loop.run_in_executor(
                            self.thread_pool, self.parse_article_html, html, url, source
                        )
                        return article
                    else:
                        self.logger.warning(
                            "HTTP {0} for {1}".format(response.status, url)
                        )
                        return None
            except Exception as e:
                self.logger.error("Error scraping article {0}: {1}".format(url, e))
                return None

    def parse_article_html(
        self, html: str, url: str, source: NewsSource
    ) -> Optional[Article]:
        """Parse article from HTML (runs in thread pool)."""
        from bs4 import BeautifulSoup

        try:
            soup = BeautifulSoup(html, "html.parser")

            # Extract article data
            title = self.extract_text_by_selector(
                soup, source.article_selectors.get("title", "h1")
            )
            content = self.extract_content_by_selector(
                soup, source.article_selectors.get("content", "p")
            )
            author = self.extract_text_by_selector(
                soup, source.article_selectors.get("author", ".author")
            )
            date = self.extract_text_by_selector(
                soup, source.article_selectors.get("date", "time")
            )

            if not title or not content:
                return None

            # Create article
            article = Article(
                title=title.strip(),
                url=url,
                content=content.strip(),
                author=author.strip() if author else "{0} Sta".format(source.name),
                published_date=date if date else datetime.now().isoformat(),
                source=source.name,
                scraped_date=datetime.now().isoformat(),
                content_length=len(content),
                word_count=len(content.split()),
                category=self.extract_category_from_url(url),
            )

            # Calculate reading time
            article.reading_time = max(1, article.word_count // 200)

            # Validate article
            article.validation_score = self.validate_article(article)
            article.content_quality = self.get_quality_label(article.validation_score)

            return article if article.validation_score >= 50 else None

        except Exception as e:
            self.logger.debug("Error parsing article {0}: {1}".format(url, e))
            return None

    def extract_text_by_selector(self, soup, selector: str) -> str:
        """Extract text using CSS selector."""
        try:
            element = soup.select_one(selector)
            return element.get_text(strip=True) if element else ""
        except BaseException:
            return ""

    def extract_content_by_selector(self, soup, selector: str) -> str:
        """Extract content from multiple elements."""
        try:
            elements = soup.select(selector)
            return " ".join(elem.get_text(strip=True) for elem in elements)
        except BaseException:
            return ""

    async def scrape_js_source(self, source: NewsSource) -> List[Article]:
        """Scrape JavaScript-heavy source using Playwright."""
        if not self.browser_contexts:
            self.logger.warning(
                "No browser contexts available for {0}".format(source.name)
            )
            return []

        # Use available browser context
        context = self.browser_contexts[0]

        try:
            self.logger.info("Using Playwright for {0}".format(source.name))

            # Get article links
            links = await self.get_article_links_js(context, source)
            self.logger.info(
                "Found {0} JS links for {1}".format(len(links), source.name)
            )

            # Scrape articles with controlled concurrency
            semaphore = asyncio.Semaphore(2)  # Limit browser concurrency

            tasks = []
            for link in links[:20]:  # Limit for JS scraping
                if link not in self.seen_urls:
                    self.seen_urls.add(link)
                    task = asyncio.create_task(
                        self.scrape_article_js(semaphore, context, source, link)
                    )
                    tasks.append(task)

            # Wait for all articles
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Filter successful results
            valid_articles = [
                article for article in results if isinstance(article, Article)
            ]

            for article in valid_articles:
                self.monitor.record_article(source.name)

            return valid_articles

        except Exception as e:
            self.logger.error("JS scraping error for {0}: {1}".format(source.name, e))
            self.monitor.record_error(source.name)
            return []

    async def get_article_links_js(
        self, context: BrowserContext, source: NewsSource
    ) -> List[str]:
        """Get article links using Playwright."""
        page = await context.new_page()

        try:
            # Navigate to source
            await page.goto(source.base_url, wait_until="networkidle", timeout=30000)

            # Wait for content to load
            await page.wait_for_timeout(3000)

            # Extract links
            links = await page.evaluate(
                """
                () => {{
                    const patterns = {json.dumps(source.link_patterns)};
                    const links = Array.from(document.querySelectorAll('a[href]'))
                        .map(a => a.href)
                        .filter(href => patterns.some(pattern => new RegExp(pattern).test(href)));
                    return [...new Set(links)];
                }}
            """
            )

            return links

        except Exception as e:
            self.logger.error(
                "Error getting JS links from {0}: {1}".format(source.base_url, e)
            )
            return []
        finally:
            await page.close()

    async def scrape_article_js(
        self,
        semaphore: asyncio.Semaphore,
        context: BrowserContext,
        source: NewsSource,
        url: str,
    ) -> Optional[Article]:
        """Scrape individual article using Playwright."""
        async with semaphore:
            page = await context.new_page()

            try:
                # Navigate to article
                await page.goto(url, wait_until="networkidle", timeout=30000)

                # Wait for content
                await page.wait_for_timeout(2000)

                # Extract article data using JavaScript
                article_data = await page.evaluate(
                    """
                    () => {{
                        const selectors = {json.dumps(source.article_selectors)};

                        const getTextBySelector = (selector) => {{
                            const element = document.querySelector(selector);
                            return element ? element.textContent.trim(): '';
                        }};

                        const getContentBySelector = (selector) => {{
                            const elements = document.querySelectorAll(selector);
                            return Array.from(elements).map(el => el.textContent.trim()).join(' ');
                        }};

                        return {{
                            title: getTextBySelector(selectors.title || 'h1'),
                            content: getContentBySelector(selectors.content || 'p'),
                            author: getTextBySelector(selectors.author || '.author'),
                            date: getTextBySelector(selectors.date || 'time')
                        }};
                    }}
                """
                )

                if not article_data["title"] or not article_data["content"]:
                    return None

                # Create article
                article = Article(
                    title=article_data["title"],
                    url=url,
                    content=article_data["content"],
                    author=article_data["author"] or "{0} Sta".format(source.name),
                    published_date=article_data["date"] or datetime.now().isoformat(),
                    source=source.name,
                    scraped_date=datetime.now().isoformat(),
                    content_length=len(article_data["content"]),
                    word_count=len(article_data["content"].split()),
                    category=self.extract_category_from_url(url),
                )

                # Calculate reading time
                article.reading_time = max(1, article.word_count // 200)

                # Validate article
                article.validation_score = self.validate_article(article)
                article.content_quality = self.get_quality_label(
                    article.validation_score
                )

                return article if article.validation_score >= 50 else None

            except Exception as e:
                self.logger.debug("Error scraping JS article {0}: {1}".format(url, e))
                return None
            finally:
                await page.close()

    def extract_category_from_url(self, url: str) -> str:
        """Extract category from URL."""
        url_lower = url.lower()

        categories = {
            "Technology": ["tech", "technology", "gadgets", "ai", "software"],
            "Politics": ["politics", "political", "government", "election"],
            "Business": ["business", "finance", "economy", "market"],
            "Sports": ["sports", "sport", "football", "basketball"],
            "Health": ["health", "medical", "medicine", "covid"],
            "Science": ["science", "research", "study", "discovery"],
        }

        for category, keywords in categories.items():
            if any(keyword in url_lower for keyword in keywords):
                return category

        return "News"

    def validate_article(self, article: Article) -> int:
        """Validate article quality (same logic as Python ValidationPipeline)."""
        score = 100

        # Check required fields
        if not article.title:
            score -= 25
        if not article.content:
            score -= 30
        if not article.author:
            score -= 10

        # Content length validation
        if article.content_length < 100:
            score -= 20
        elif article.content_length > 10000:
            score -= 5

        # Title length validation
        if len(article.title) < 10:
            score -= 10
        elif len(article.title) > 200:
            score -= 5

        return max(0, score)

    def get_quality_label(self, score: int) -> str:
        """Get quality label from score."""
        if score >= 80:
            return "high"
        elif score >= 60:
            return "medium"
        else:
            return "low"

    async def save_articles(self, articles: List[Article], output_path: str):
        """Save articles to JSON file asynchronously."""
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Convert articles to dictionaries
        articles_data = [asdict(article) for article in articles]

        # Save asynchronously
        async with aiofiles.open(output_file, "w") as f:
            await f.write(json.dumps(articles_data, indent=2, ensure_ascii=False))

        self.logger.info("Saved {0} articles to {1}".format(len(articles), output_file))

    def get_performance_stats(self) -> Dict[str, Any]:
        """Get current performance statistics."""
        return self.monitor.get_stats()


# Pre-configured news sources — built from config_async.json, the single
# source of truth (#881). The former in-code literal duplicated the JSON and
# the two drifted; a minimal fallback remains only for a missing/broken file.
def load_async_sources(path=None) -> List[NewsSource]:
    """Build NewsSource objects from the canonical sources config (#881)."""
    from .sources_config import enabled_sources

    return [
        NewsSource(
            name=entry["name"],
            base_url=entry["base_url"],
            article_selectors=dict(entry["article_selectors"]),
            link_patterns=list(entry.get("link_patterns", [])),
            requires_js=bool(entry.get("requires_js", False)),
            rate_limit=float(entry.get("rate_limit", 1.0)),
            timeout=int(entry.get("timeout", 30)),
        )
        for entry in enabled_sources(path)
    ]


_FALLBACK_SOURCES = [
    NewsSource(
        name="BBC",
        base_url="https://www.bbc.com/news",
        article_selectors={
            "title": "h1[data-testid='headline'], h1",
            "content": "div[data-component='text-block'] p",
            "author": "span[data-testid='byline']",
            "date": "time[datetime]",
        },
        link_patterns=[r"/news/.*"],
        requires_js=False,
        rate_limit=1.0,
    ),
]

try:
    ASYNC_NEWS_SOURCES = load_async_sources()
except Exception as _exc:  # config missing/broken: stay importable, log loudly
    logging.getLogger(__name__).error(
        "sources config unusable (%s); using minimal fallback source list", _exc
    )
    ASYNC_NEWS_SOURCES = _FALLBACK_SOURCES
