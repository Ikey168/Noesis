"""
Article Normalization Module
Issue #229: Chunking & normalization pipeline

This module handles the normalization of articles from various formats (HTML, markdown, etc.)
into clean text while preserving important metadata like title, byline, timestamp, URL, and language.
"""

import logging
import re
from typing import Dict, Optional, Any
from datetime import datetime
from urllib.parse import urlparse

try:
    from bs4 import BeautifulSoup
    from bs4.element import Comment
except ImportError:
    raise ImportError(
        "beautifulsoup4 is required for HTML normalization. "
        "Install it with: pip install beautifulsoup4"
    )

try:
    import langdetect
except ImportError:
    langdetect = None
    
logger = logging.getLogger(__name__)


class ArticleNormalizer:
    """
    Article normalizer that converts various formats to clean text with metadata.
    
    Features:
    - HTML to clean text conversion
    - Metadata extraction (title, byline, timestamp, URL, language)
    - Language detection
    - Text cleaning and normalization
    - Configurable extraction rules
    """
    
    def __init__(
        self,
        preserve_paragraphs: bool = True,
        remove_extra_whitespace: bool = True,
        detect_language: bool = True,
        min_paragraph_length: int = 10,
        language_backend: str = "langdetect",
        language_candidates: tuple[str, ...] = ("de", "en"),
    ):
        """
        Initialize article normalizer.
        
        Args:
            preserve_paragraphs: Whether to preserve paragraph structure
            remove_extra_whitespace: Whether to normalize whitespace
            detect_language: Whether to auto-detect language
            min_paragraph_length: Minimum length for valid paragraphs
        """
        self.preserve_paragraphs = preserve_paragraphs
        self.remove_extra_whitespace = remove_extra_whitespace
        self.detect_language = detect_language
        self.min_paragraph_length = min_paragraph_length
        if language_backend not in {"langdetect", "lingua"}:
            raise ValueError("unknown language backend")
        self.language_backend = language_backend
        self.language_candidates = language_candidates
        
        # Common patterns for cleaning
        self.url_pattern = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')
        self.email_pattern = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
        self.multiple_spaces = re.compile(r'\s+')
        self.multiple_newlines = re.compile(r'\n\s*\n')
        
    def normalize_article(self, raw_content: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Normalize an article from raw content to clean text with metadata.
        
        Args:
            raw_content: Raw article content (HTML, markdown, or plain text)
            metadata: Optional metadata dictionary to augment
            
        Returns:
            Dictionary containing normalized text and extracted metadata
        """
        if not raw_content or not raw_content.strip():
            return self._empty_result(metadata)
        
        # Initialize result with provided metadata
        result = {
            'title': '',
            'byline': '',
            'timestamp': None,
            'url': '',
            'language': 'unknown',
            'content': '',
            'word_count': 0,
            'char_count': 0,
            'metadata': metadata or {}
        }
        
        # Detect content type and normalize
        if self._is_html(raw_content):
            result.update(self._normalize_html(raw_content))
        else:
            result.update(self._normalize_text(raw_content))
        
        # Override with provided metadata after normalization
        if metadata:
            for key in ['title', 'byline', 'timestamp', 'url', 'language']:
                if key in metadata and metadata[key]:
                    result[key] = metadata[key]
        
        # Post-processing
        result = self._post_process(result)
        
        logger.debug(
            f"Normalized article: {result['word_count']} words, "
            f"{result['char_count']} characters, language: {result['language']}"
        )
        
        return result
    
    def _is_html(self, content: str) -> bool:
        """Check if content appears to be HTML."""
        return bool(re.search(r'<[^>]+>', content))
    
    def _normalize_html(self, html_content: str) -> Dict[str, Any]:
        """Normalize HTML content to clean text with metadata extraction."""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Remove unwanted elements
            for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                element.decompose()
            
            # Remove comments
            for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
                comment.extract()
            
            result = {}
            
            # Extract title
            title_elem = soup.find('title') or soup.find('h1') or soup.find(['h2', 'h3'])
            result['title'] = self._clean_text(title_elem.get_text()) if title_elem else ''
            
            # Extract byline/author
            byline_elem = (
                soup.find(attrs={'class': re.compile(r'author|byline', re.I)}) or
                soup.find(attrs={'name': 'author'}) or
                soup.find('meta', attrs={'name': 'author'})
            )
            if byline_elem:
                if byline_elem.name == 'meta':
                    result['byline'] = byline_elem.get('content', '')
                else:
                    result['byline'] = self._clean_text(byline_elem.get_text())
            else:
                result['byline'] = ''
            
            # Extract timestamp
            time_elem = (
                soup.find('time') or
                soup.find(attrs={'class': re.compile(r'date|time|published', re.I)}) or
                soup.find('meta', attrs={'name': re.compile(r'date|published', re.I)})
            )
            result['timestamp'] = self._extract_timestamp(time_elem)
            
            # Extract URL (from canonical link or base)
            url_elem = (
                soup.find('link', attrs={'rel': 'canonical'}) or
                soup.find('meta', attrs={'property': 'og:url'})
            )
            result['url'] = url_elem.get('href', '') if url_elem else ''
            
            # Extract main content
            content_elem = (
                soup.find('article') or
                soup.find(attrs={'class': re.compile(r'content|article|main', re.I)}) or
                soup.find('main') or
                soup.find('body')
            )
            
            if content_elem:
                # Get text while preserving paragraph structure
                if self.preserve_paragraphs:
                    paragraphs = content_elem.find_all(['p', 'div', 'section', 'pre', 'code'])
                    content_text = '\n\n'.join([
                        self._clean_text(p.get_text()) 
                        for p in paragraphs 
                        if self._clean_text(p.get_text()) and len(self._clean_text(p.get_text())) >= self.min_paragraph_length
                    ])
                else:
                    content_text = self._clean_text(content_elem.get_text())
            else:
                content_text = self._clean_text(soup.get_text())
            
            result['content'] = content_text
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to parse HTML content: {e}")
            return self._normalize_text(html_content)
    
    def _normalize_text(self, text_content: str) -> Dict[str, Any]:
        """Normalize plain text content."""
        cleaned_text = self._clean_text(text_content)
        
        # Try to extract title from first line if it looks like a title
        lines = cleaned_text.split('\n')
        title = ''
        content = cleaned_text
        
        if lines and len(lines[0]) < 200 and not lines[0].endswith('.'):
            title = lines[0].strip()
            content = '\n'.join(lines[1:]).strip()
        
        return {
            'title': title,
            'byline': '',
            'timestamp': None,
            'url': '',
            'content': content,
        }
    
    def _clean_text(self, text: str) -> str:
        """Clean and normalize text content."""
        if not text:
            return ''
        
        # Remove URLs and emails
        text = self.url_pattern.sub('', text)
        text = self.email_pattern.sub('', text)
        
        # Normalize whitespace
        if self.remove_extra_whitespace:
            text = self.multiple_spaces.sub(' ', text)
            text = self.multiple_newlines.sub('\n\n', text)
        
        # Remove extra whitespace at line beginnings/ends
        lines = [line.strip() for line in text.split('\n')]
        text = '\n'.join(line for line in lines if line)
        
        return text.strip()
    
    def _extract_timestamp(self, time_elem) -> Optional[datetime]:
        """Extract timestamp from HTML element."""
        if not time_elem:
            return None
        
        # Try datetime attribute first
        datetime_str = time_elem.get('datetime') if hasattr(time_elem, 'get') else None
        
        # Try content attribute for meta tags
        if not datetime_str and hasattr(time_elem, 'get'):
            datetime_str = time_elem.get('content')
        
        # Try text content
        if not datetime_str:
            datetime_str = time_elem.get_text() if hasattr(time_elem, 'get_text') else str(time_elem)
        
        # Parse datetime string
        if datetime_str:
            return self._parse_datetime(datetime_str)
        
        return None
    
    def _parse_datetime(self, datetime_str: str) -> Optional[datetime]:
        """Parse datetime string into datetime object."""
        # Common datetime formats
        formats = [
            '%Y-%m-%dT%H:%M:%S%z',  # ISO format with timezone
            '%Y-%m-%dT%H:%M:%S',     # ISO format without timezone
            '%Y-%m-%d %H:%M:%S',     # Standard format
            '%Y-%m-%d',              # Date only
            '%B %d, %Y',             # Month Day, Year
            '%b %d, %Y',             # Abbreviated month
            '%d/%m/%Y',              # Day/Month/Year
            '%m/%d/%Y',              # Month/Day/Year
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(datetime_str.strip(), fmt)
            except ValueError:
                continue
        
        logger.warning(f"Could not parse datetime: {datetime_str}")
        return None
    
    def _post_process(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Post-process the normalized result."""
        content = result.get('content', '')
        
        # Calculate statistics
        result['word_count'] = len(content.split()) if content else 0
        result['char_count'] = len(content) if content else 0
        
        # Detect language if enabled
        if self.detect_language and content and self.language_backend == "lingua":
            from src.integrations.text import detect_language
            detection = detect_language(content, languages=self.language_candidates)
            result['language'] = detection['language']
            result['language_detection'] = detection
        elif self.detect_language and content and langdetect:
            try:
                result['language'] = langdetect.detect(content)
            except Exception as e:
                logger.debug(f"Language detection failed: {e}")
                result['language'] = 'unknown'
        elif not self.detect_language:
            result['language'] = 'unknown'
        
        # Validate URL
        if result.get('url'):
            try:
                parsed = urlparse(result['url'])
                if not parsed.scheme or not parsed.netloc:
                    result['url'] = ''
            except Exception:
                result['url'] = ''
        
        return result
    
    def _empty_result(self, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return empty result structure."""
        return {
            'title': '',
            'byline': '',
            'timestamp': None,
            'url': '',
            'language': 'unknown',
            'content': '',
            'word_count': 0,
            'char_count': 0,
            'metadata': metadata or {}
        }


def normalize_article(
    raw_content: str,
    metadata: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Convenience function to normalize an article.
    
    Args:
        raw_content: Raw article content
        metadata: Optional metadata dictionary
        **kwargs: Additional arguments for ArticleNormalizer
        
    Returns:
        Normalized article with metadata
    """
    normalizer = ArticleNormalizer(**kwargs)
    return normalizer.normalize_article(raw_content, metadata)
