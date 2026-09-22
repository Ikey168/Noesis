"""
Items for the NeuroNews scrapers.
"""

import scrapy


class NewsItem(scrapy.Item):
    """Item for storing scraped news articles."""

    title = scrapy.Field()
    url = scrapy.Field()
    content = scrapy.Field()
    published_date = scrapy.Field()
    source = scrapy.Field()
    author = scrapy.Field()
    category = scrapy.Field()

    # Additional metadata fields
    scraped_date = scrapy.Field()
    acquisition_provenance_json = scrapy.Field()
    content_length = scrapy.Field()
    language = scrapy.Field()
    tags = scrapy.Field()
    summary = scrapy.Field()
    image_url = scrapy.Field()
    video_url = scrapy.Field()
    reading_time = scrapy.Field()
    word_count = scrapy.Field()

    # Data quality fields
    validation_score = scrapy.Field()
    content_quality = scrapy.Field()
    duplicate_check = scrapy.Field()

    # Multi-language processing fields
    language_info = scrapy.Field()
    translation_info = scrapy.Field()
    original_language = scrapy.Field()
    translation_performed = scrapy.Field()
    detection_confidence = scrapy.Field()
    translation_quality = scrapy.Field()
    translated_title = scrapy.Field()
    translated_content = scrapy.Field()
    target_language = scrapy.Field()
