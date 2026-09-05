"""
News aggregator connectors for various news services.
"""

import aiohttp
import asyncio
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, timedelta
import feedparser
from .base import BaseConnector, ConnectionError, DataFormatError, AuthenticationError


class NewsAggregatorConnector(BaseConnector):
    """
    News aggregator connector for fetching news from various sources.
    """

    def __init__(self, config: Dict[str, Any], auth_config: Optional[Dict[str, Any]] = None):
        """
        Initialize news aggregator connector.
        
        Args:
            config: News aggregator configuration
            auth_config: Authentication configuration (if required)
        """
        super().__init__(config, auth_config)
        self._session = None
        self._headers = {}
        self.service = config.get('service', '').lower()

    def validate_config(self) -> bool:
        """Validate news aggregator connector configuration."""
        required_fields = ['service']
        
        for field in required_fields:
            if field not in self.config:
                return False
                
        # Service-specific validation
        if self.service == 'newsapi':
            return 'api_key' in self.auth_config
        elif self.service == 'guardian':
            return 'api_key' in self.auth_config
        elif self.service == 'nytimes':
            return 'api_key' in self.auth_config
        elif self.service == 'reuters':
            return True  # No API key required for RSS
        elif self.service == 'google_news':
            return True  # No API key required for RSS
        elif self.service == 'bing_news':
            return 'subscription_key' in self.auth_config
                
        return True

    async def connect(self) -> bool:
        """Establish connection to news aggregator service."""
        try:
            if self._session and not self._session.closed:
                self._connected = True
                return True
                
            timeout = aiohttp.ClientTimeout(total=self.config.get('timeout', 30))
            self._session = aiohttp.ClientSession(timeout=timeout)
            
            # Set up headers
            self._headers = {
                'User-Agent': self.config.get('user_agent', 'NeuroNews-Scraper/1.0'),
                'Accept': 'application/json',
            }
            
            # Service-specific setup
            if not await self._setup_service():
                return False
            
            self._connected = True
            return True
                    
        except Exception as e:
            self._last_error = ConnectionError(f"Failed to connect to {self.service}: {e}")
            return False

    async def _setup_service(self) -> bool:
        """Setup service-specific authentication and headers."""
        try:
            if self.service == 'newsapi':
                self._headers['X-API-Key'] = self.auth_config['api_key']
            elif self.service == 'guardian':
                self._headers['Accept'] = 'application/json'
            elif self.service == 'nytimes':
                # NYTimes uses API key in query parameters
                pass
            elif self.service == 'bing_news':
                self._headers['Ocp-Apim-Subscription-Key'] = self.auth_config['subscription_key']
            elif self.service in ['reuters', 'google_news']:
                # RSS-based services
                self._headers['Accept'] = 'application/rss+xml, application/xml, text/xml'
            
            return True
                
        except Exception as e:
            self._last_error = AuthenticationError(f"Service setup failed: {e}")
            return False

    async def disconnect(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._connected = False

    async def fetch_data(self, query: str = "", category: str = "", 
                        country: str = "us", language: str = "en", 
                        limit: int = 100, **kwargs) -> List[Dict[str, Any]]:
        """
        Fetch news data from aggregator service.
        
        Args:
            query: Search query
            category: News category
            country: Country code
            language: Language code
            limit: Maximum number of articles
            
        Returns:
            List of news articles
        """
        if not self.is_connected:
            raise ConnectionError(f"Not connected to {self.service}")

        try:
            if self.service == 'newsapi':
                return await self._fetch_newsapi_data(query, category, country, language, limit, **kwargs)
            elif self.service == 'guardian':
                return await self._fetch_guardian_data(query, category, limit, **kwargs)
            elif self.service == 'nytimes':
                return await self._fetch_nytimes_data(query, limit, **kwargs)
            elif self.service == 'reuters':
                return await self._fetch_reuters_data(category, limit, **kwargs)
            elif self.service == 'google_news':
                return await self._fetch_google_news_data(query, country, language, limit, **kwargs)
            elif self.service == 'bing_news':
                return await self._fetch_bing_news_data(query, category, country, limit, **kwargs)
            else:
                raise ConnectionError(f"Unsupported service: {self.service}")
                
        except AuthenticationError as e:
            self._last_error = e
            raise
        except Exception as e:
            self._last_error = e
            raise ConnectionError(f"Failed to fetch {self.service} data: {e}")

    async def _fetch_newsapi_data(self, query: str, category: str, country: str, 
                                 language: str, limit: int, **kwargs) -> List[Dict[str, Any]]:
        """Fetch data from NewsAPI."""
        url = "https://newsapi.org/v2/top-headlines"
        params = {
            'country': country,
            'language': language,
            'pageSize': min(limit, 100),
        }
        
        if query:
            url = "https://newsapi.org/v2/everything"
            params = {'q': query, 'language': language, 'pageSize': min(limit, 100)}
        
        if category:
            params['category'] = category
            
        # Add date filters if provided
        if 'from_date' in kwargs:
            params['from'] = kwargs['from_date']
        if 'to_date' in kwargs:
            params['to'] = kwargs['to_date']
        
        async with self._session.get(url, headers=self._headers, params=params) as response:
            if response.status == 401:
                raise AuthenticationError("NewsAPI authentication failed")
            elif response.status == 429:
                raise ConnectionError("NewsAPI rate limit exceeded")
            elif response.status != 200:
                raise ConnectionError(f"NewsAPI error: {response.status}")
                
            data = await response.json()
            
            articles = []
            for article_data in data.get('articles', []):
                article = {
                    'title': article_data.get('title'),
                    'description': article_data.get('description'),
                    'content': article_data.get('content'),
                    'url': article_data.get('url'),
                    'published_at': article_data.get('publishedAt'),
                    'author': article_data.get('author'),
                    'source': article_data.get('source', {}).get('name'),
                    'url_to_image': article_data.get('urlToImage'),
                    'service': 'newsapi'
                }
                articles.append(article)
            
            return articles

    async def _fetch_guardian_data(self, query: str, category: str, limit: int, **kwargs) -> List[Dict[str, Any]]:
        """Fetch data from The Guardian API."""
        from src.ingestion.guardian_api import parameters, records
        url = "https://content.guardianapis.com/search"
        cursor = str(kwargs.get('cursor') or '1')
        result=[]
        limit=max(1,min(1000,int(limit)))
        for _ in range(max(1,min(20,int(kwargs.get('max_pages',5))))):
            params=parameters({'query':query,'section':category,**{k:kwargs[k] for k in ('from_date','to_date') if k in kwargs}},cursor=cursor,limit=limit-len(result),secret=self.auth_config['api_key'])
            async with self._session.get(url,headers=self._headers,params=params) as response:
                if response.status in (401,403):raise AuthenticationError('Guardian API authentication failed')
                if response.status!=200:raise ConnectionError(f'Guardian API HTTP {response.status}')
                mapped,cursor=records(await response.json(),limit=params['page-size'])
            existing={item['id'] for item in result}
            result.extend(item for item in mapped if item['id'] not in existing)
            self.guardian_checkpoint={'next_cursor':cursor,'from_date':kwargs.get('from_date'),'to_date':kwargs.get('to_date')}
            if cursor is None or len(result)>=limit:break
        return result

    async def _fetch_nytimes_data(self, query: str, limit: int, **kwargs) -> List[Dict[str, Any]]:
        """Fetch data from New York Times API."""
        if query:
            url = "https://api.nytimes.com/svc/search/v2/articlesearch.json"
            params = {
                'api-key': self.auth_config['api_key'],
                'q': query,
                'sort': 'newest'
            }
        else:
            url = "https://api.nytimes.com/svc/topstories/v2/home.json"
            params = {'api-key': self.auth_config['api_key']}
        
        async with self._session.get(url, headers=self._headers, params=params) as response:
            if response.status == 401:
                raise AuthenticationError("NYTimes API authentication failed")
            elif response.status == 429:
                raise ConnectionError("NYTimes API rate limit exceeded")
            elif response.status != 200:
                raise ConnectionError(f"NYTimes API error: {response.status}")
                
            data = await response.json()
            
            articles = []
            
            if query:
                # Article Search API format
                docs = data.get('response', {}).get('docs', [])[:limit]
                for doc in docs:
                    article = {
                        'title': doc.get('headline', {}).get('main'),
                        'description': doc.get('abstract'),
                        'content': doc.get('lead_paragraph'),
                        'url': doc.get('web_url'),
                        'published_at': doc.get('pub_date'),
                        'author': ', '.join([person.get('firstname', '') + ' ' + person.get('lastname', '') 
                                           for person in doc.get('byline', {}).get('person', [])]),
                        'source': 'The New York Times',
                        'section': doc.get('section_name'),
                        'service': 'nytimes'
                    }
                    articles.append(article)
            else:
                # Top Stories API format
                results = data.get('results', [])[:limit]
                for result in results:
                    article = {
                        'title': result.get('title'),
                        'description': result.get('abstract'),
                        'content': '',
                        'url': result.get('url'),
                        'published_at': result.get('published_date'),
                        'author': result.get('byline'),
                        'source': 'The New York Times',
                        'section': result.get('section'),
                        'url_to_image': result.get('multimedia', [{}])[0].get('url') if result.get('multimedia') else None,
                        'service': 'nytimes'
                    }
                    articles.append(article)
            
            return articles

    async def _fetch_reuters_data(self, category: str, limit: int, **kwargs) -> List[Dict[str, Any]]:
        """Fetch data from Reuters RSS feeds."""
        rss_urls = {
            'world': 'https://feeds.reuters.com/reuters/worldNews',
            'business': 'https://feeds.reuters.com/reuters/businessNews',
            'technology': 'https://feeds.reuters.com/reuters/technologyNews',
            'sports': 'https://feeds.reuters.com/reuters/sportsNews',
            'entertainment': 'https://feeds.reuters.com/reuters/entertainment',
            'health': 'https://feeds.reuters.com/reuters/healthNews'
        }
        
        url = rss_urls.get(category.lower(), rss_urls['world'])
        
        async with self._session.get(url, headers=self._headers) as response:
            if response.status != 200:
                raise ConnectionError(f"Reuters RSS error: {response.status}")
                
            content = await response.text()
            
        feed = feedparser.parse(content)
        articles = []
        
        for entry in feed.entries[:limit]:
            article = {
                'title': entry.get('title', ''),
                'description': entry.get('summary', ''),
                'content': entry.get('description', ''),
                'url': entry.get('link', ''),
                'published_at': entry.get('published', ''),
                'author': entry.get('author', ''),
                'source': 'Reuters',
                'service': 'reuters'
            }
            articles.append(article)
        
        return articles

    async def _fetch_google_news_data(self, query: str, country: str, language: str, 
                                     limit: int, **kwargs) -> List[Dict[str, Any]]:
        """Fetch data from Google News RSS."""
        base_url = "https://news.google.com/rss"
        
        if query:
            url = f"{base_url}/search?q={query}&hl={language}&gl={country}&ceid={country}:{language}"
        else:
            url = f"{base_url}?hl={language}&gl={country}&ceid={country}:{language}"
        
        async with self._session.get(url, headers=self._headers) as response:
            if response.status != 200:
                raise ConnectionError(f"Google News RSS error: {response.status}")
                
            content = await response.text()
            
        feed = feedparser.parse(content)
        articles = []
        
        for entry in feed.entries[:limit]:
            article = {
                'title': entry.get('title', ''),
                'description': entry.get('summary', ''),
                'content': '',
                'url': entry.get('link', ''),
                'published_at': entry.get('published', ''),
                'author': '',
                'source': entry.get('source', {}).get('title', 'Google News'),
                'service': 'google_news'
            }
            articles.append(article)
        
        return articles

    async def _fetch_bing_news_data(self, query: str, category: str, country: str, 
                                   limit: int, **kwargs) -> List[Dict[str, Any]]:
        """Fetch data from Bing News Search API."""
        url = "https://api.bing.microsoft.com/v7.0/news/search"
        params = {
            'q': query or 'latest news',
            'count': min(limit, 100),
            'mkt': f"{country}-{country.upper()}",
            'textDecorations': False,
            'textFormat': 'Raw'
        }
        
        if category:
            params['category'] = category
        
        async with self._session.get(url, headers=self._headers, params=params) as response:
            if response.status == 401:
                raise AuthenticationError("Bing News API authentication failed")
            elif response.status == 429:
                raise ConnectionError("Bing News API rate limit exceeded")
            elif response.status != 200:
                raise ConnectionError(f"Bing News API error: {response.status}")
                
            data = await response.json()
            
            articles = []
            for article_data in data.get('value', []):
                article = {
                    'title': article_data.get('name'),
                    'description': article_data.get('description'),
                    'content': '',
                    'url': article_data.get('url'),
                    'published_at': article_data.get('datePublished'),
                    'author': '',
                    'source': article_data.get('provider', [{}])[0].get('name', ''),
                    'url_to_image': article_data.get('image', {}).get('thumbnail', {}).get('contentUrl'),
                    'service': 'bing_news'
                }
                articles.append(article)
            
            return articles

    def validate_data_format(self, data: Any) -> bool:
        """
        Validate news article data format.
        
        Args:
            data: News article data to validate
            
        Returns:
            True if data format is valid
        """
        if not isinstance(data, dict):
            return False
            
        # Should have at least title, url, and service
        required_fields = ['title', 'url', 'service']
        for field in required_fields:
            if field not in data or not data[field]:
                return False
                
        return True

    async def get_available_categories(self) -> List[str]:
        """
        Get list of available news categories for the service.
        
        Returns:
            List of category names
        """
        categories = {
            'newsapi': ['business', 'entertainment', 'general', 'health', 'science', 'sports', 'technology'],
            'guardian': ['world', 'uk', 'us', 'politics', 'environment', 'science', 'tech', 'sport'],
            'nytimes': ['arts', 'business', 'movies', 'politics', 'science', 'sports', 'technology', 'world'],
            'reuters': ['world', 'business', 'technology', 'sports', 'entertainment', 'health'],
            'google_news': ['world', 'nation', 'business', 'technology', 'entertainment', 'sports', 'science', 'health'],
            'bing_news': ['business', 'entertainment', 'politics', 'products', 'scienceandtechnology', 'sports', 'world']
        }
        
        return categories.get(self.service, [])

    async def get_service_limits(self) -> Dict[str, Any]:
        """
        Get service-specific rate limits and restrictions.
        
        Returns:
            Dictionary containing service limits
        """
        limits = {
            'newsapi': {
                'requests_per_day': 1000,
                'max_results_per_request': 100,
                'requires_api_key': True
            },
            'guardian': {
                'requests_per_day': 5000,
                'max_results_per_request': 50,
                'requires_api_key': True
            },
            'nytimes': {
                'requests_per_day': 1000,
                'max_results_per_request': 100,
                'requires_api_key': True
            },
            'reuters': {
                'requests_per_day': 'unlimited',
                'max_results_per_request': 'varies',
                'requires_api_key': False
            },
            'google_news': {
                'requests_per_day': 'unlimited',
                'max_results_per_request': 'varies',
                'requires_api_key': False
            },
            'bing_news': {
                'requests_per_second': 3,
                'requests_per_month': 1000,
                'max_results_per_request': 100,
                'requires_api_key': True
            }
        }
        
        return limits.get(self.service, {})
