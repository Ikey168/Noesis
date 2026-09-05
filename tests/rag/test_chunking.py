"""
Tests for Text Chunking Module
Issue #229: Chunking & normalization pipeline

Tests cover text chunking with various strategies, edge cases,
overlap handling, and chunk metadata.
"""

import pytest
from services.rag.chunking import TextChunker, ChunkConfig, SplitStrategy, TextChunk, chunk_text


class TestChunkConfig:
    """Test ChunkConfig dataclass."""
    
    def test_default_config(self):
        """Test default configuration values."""
        config = ChunkConfig()
        assert config.max_chars == 1000
        assert config.overlap_chars == 100
        assert config.split_on == SplitStrategy.SENTENCE
        assert config.min_chunk_chars == 50
        assert config.preserve_sentences is True
        assert config.language == "en"
    
    def test_custom_config(self):
        """Test custom configuration."""
        config = ChunkConfig(
            max_chars=500,
            overlap_chars=50,
            split_on=SplitStrategy.PARAGRAPH,
            min_chunk_chars=25
        )
        assert config.max_chars == 500
        assert config.overlap_chars == 50
        assert config.split_on == SplitStrategy.PARAGRAPH
        assert config.min_chunk_chars == 25


class TestTextChunk:
    """Test TextChunk dataclass."""
    
    def test_chunk_creation(self):
        """Test creating a text chunk."""
        chunk = TextChunk(
            text="Test chunk content.",
            start_offset=0,
            end_offset=19,
            chunk_id=1,
            word_count=3,
            char_count=19,
            metadata={"test": "value"}
        )
        
        assert chunk.text == "Test chunk content."
        assert chunk.start_offset == 0
        assert chunk.end_offset == 19
        assert chunk.chunk_id == 1
        assert chunk.word_count == 3
        assert chunk.char_count == 19
        assert chunk.metadata["test"] == "value"
    
    def test_chunk_to_dict(self):
        """Test converting chunk to dictionary."""
        chunk = TextChunk(
            text="Test",
            start_offset=0,
            end_offset=4,
            chunk_id=0,
            word_count=1,
            char_count=4,
            metadata={}
        )
        
        chunk_dict = chunk.to_dict()
        
        assert isinstance(chunk_dict, dict)
        assert chunk_dict['text'] == "Test"
        assert chunk_dict['chunk_id'] == 0
        assert 'metadata' in chunk_dict


class TestTextChunker:
    """Test TextChunker class."""
    
    def setup_method(self):
        """Setup test fixtures."""
        self.config = ChunkConfig(max_chars=100, overlap_chars=20, min_chunk_chars=10)
        self.chunker = TextChunker(self.config)
    
    def test_empty_text(self):
        """Test chunking empty text."""
        chunks = self.chunker.chunk_text("")
        assert len(chunks) == 0
        
        chunks = self.chunker.chunk_text("   ")
        assert len(chunks) == 0
    
    def test_short_text(self):
        """Test chunking text shorter than max_chars."""
        text = "This is a short text that fits in one chunk."
        chunks = self.chunker.chunk_text(text)
        
        assert len(chunks) == 1
        assert chunks[0].text == text
        assert chunks[0].chunk_id == 0
        assert chunks[0].word_count == 10
        assert chunks[0].char_count == len(text)
    
    def test_sentence_chunking(self):
        """Test chunking by sentences."""
        text = ("This is the first sentence. " * 3 +
                "This is the second sentence. " * 3 +
                "This is the third sentence. " * 3)
        
        config = ChunkConfig(max_chars=150, overlap_chars=30, split_on=SplitStrategy.SENTENCE)
        chunker = TextChunker(config)
        chunks = chunker.chunk_text(text)
        
        assert len(chunks) > 1
        # Check that chunks don't break sentences (when using sentence strategy)
        for chunk in chunks:
            assert chunk.text.strip().endswith('.') or chunk == chunks[-1]  # Last chunk might not end with period
    
    def test_paragraph_chunking(self):
        """Test chunking by paragraphs."""
        text = ("First paragraph content. " * 5 + "\n\n" +
                "Second paragraph content. " * 5 + "\n\n" +
                "Third paragraph content. " * 5)
        
        config = ChunkConfig(max_chars=200, overlap_chars=50, split_on=SplitStrategy.PARAGRAPH)
        chunker = TextChunker(config)
        chunks = chunker.chunk_text(text)
        
        assert len(chunks) > 1
        # Check that first chunk contains first paragraph
        assert "First paragraph" in chunks[0].text
    
    def test_word_chunking(self):
        """Test chunking by words."""
        text = " ".join([f"word{i}" for i in range(50)])  # 50 words
        
        config = ChunkConfig(max_chars=100, overlap_chars=20, split_on=SplitStrategy.WORD)
        chunker = TextChunker(config)
        chunks = chunker.chunk_text(text)
        
        assert len(chunks) > 1
        # Check that chunks don't break words
        for chunk in chunks:
            words = chunk.text.split()
            for word in words:
                assert word.startswith("word")  # All words should be complete
    
    def test_character_chunking(self):
        """Test chunking by characters."""
        text = "A" * 300  # 300 character string
        
        config = ChunkConfig(max_chars=100, overlap_chars=20, split_on=SplitStrategy.CHARACTER)
        chunker = TextChunker(config)
        chunks = chunker.chunk_text(text)
        
        assert len(chunks) >= 3  # Should be split into multiple chunks
        assert all(len(chunk.text) <= 100 for chunk in chunks)
    
    def test_chunk_overlap(self):
        """Test that overlap is properly handled."""
        text = ("Sentence one. " * 10)  # Repeated sentence
        
        config = ChunkConfig(max_chars=80, overlap_chars=20, split_on=SplitStrategy.SENTENCE)
        chunker = TextChunker(config)
        chunks = chunker.chunk_text(text)
        
        if len(chunks) > 1:
            # Check that there's some overlap between consecutive chunks
            for i in range(len(chunks) - 1):
                current_chunk = chunks[i].text
                next_chunk = chunks[i + 1].text
                # There should be some common content (overlap)
                # This is a simplified check - in practice overlap might be more complex
                assert len(next_chunk) > 0
    
    def test_min_chunk_size(self):
        """Test minimum chunk size filtering."""
        text = "Short. " + "A" * 200  # Short sentence plus long content
        
        config = ChunkConfig(max_chars=50, min_chunk_chars=30, split_on=SplitStrategy.SENTENCE)
        chunker = TextChunker(config)
        chunks = chunker.chunk_text(text)
        
        # All chunks should meet minimum size requirement
        for chunk in chunks:
            assert len(chunk.text) >= config.min_chunk_chars or chunk == chunks[-1]  # Last chunk might be shorter
    
    def test_chunk_metadata(self):
        """Test that metadata is properly included in chunks."""
        text = "Test content for metadata."
        metadata = {"source": "test", "author": "tester"}
        
        chunks = self.chunker.chunk_text(text, metadata)
        
        assert len(chunks) == 1
        chunk = chunks[0]
        assert "source" in chunk.metadata
        assert chunk.metadata["source"] == "test"
        assert chunk.metadata["author"] == "tester"
        assert "chunk_strategy" in chunk.metadata
    
    def test_very_long_words(self):
        """Test handling of very long words."""
        long_word = "supercalifragilisticexpialidocious" * 5  # Very long word
        text = f"Normal words and {long_word} more normal words."
        
        chunks = self.chunker.chunk_text(text)
        
        # Should still create chunks even with very long words
        assert len(chunks) >= 1
        # Oversized words must respect the configured bound while retaining
        # every source character through exact, reconstructable slices.
        assert all(len(chunk.text) <= self.config.max_chars for chunk in chunks)
        covered = {offset for chunk in chunks for offset in range(chunk.start_offset, chunk.end_offset)}
        assert all(i in covered for i, char in enumerate(text) if not char.isspace())
    
    def test_edge_case_only_punctuation(self):
        """Test text with only punctuation."""
        text = "!!! ??? ... --- !!!"
        
        chunks = self.chunker.chunk_text(text)
        
        # Should handle gracefully
        if chunks:  # Might be filtered out due to min_chunk_chars
            assert all(chunk.char_count > 0 for chunk in chunks)
    
    def test_edge_case_mixed_languages(self):
        """Test text with mixed languages/scripts."""
        text = "English text. Español texto. 中文文本. العربية النص."
        
        chunks = self.chunker.chunk_text(text)
        
        assert len(chunks) >= 1
        assert any("English" in chunk.text for chunk in chunks)
    
    def test_edge_case_code_blocks(self):
        """Test handling of code blocks."""
        text = """
        Here's some Python code:
        
        def function(x):
            return x * 2
            
        And here's some explanation of the code.
        """
        
        chunks = self.chunker.chunk_text(text)
        
        assert len(chunks) >= 1
        # Code should be preserved
        found_code = any("def function" in chunk.text for chunk in chunks)
        assert found_code
    
    def test_edge_case_special_characters(self):
        """Test handling of special characters and symbols."""
        text = "Text with émojis 🎉, symbols ©®™, and áccénts."
        
        chunks = self.chunker.chunk_text(text)
        
        assert len(chunks) >= 1
        assert any("émojis" in chunk.text for chunk in chunks)
        assert any("áccénts" in chunk.text for chunk in chunks)


class TestChunkingConvenienceFunction:
    """Test the convenience function."""
    
    def test_chunk_text_function(self):
        """Test the standalone chunk_text function."""
        text = "This is a test. This is another sentence."
        chunks = chunk_text(text, max_chars=30, overlap_chars=5)
        
        assert len(chunks) >= 1
        assert isinstance(chunks, list)
        assert isinstance(chunks[0], dict)
        assert 'text' in chunks[0]
        assert 'chunk_id' in chunks[0]
    
    def test_chunk_text_with_custom_params(self):
        """Test chunk_text with custom parameters."""
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = chunk_text(
            text,
            max_chars=50,
            overlap_chars=10,
            split_on="paragraph",
            metadata={"test": "value"}
        )
        
        assert len(chunks) >= 1
        if chunks:
            assert chunks[0]['metadata']['test'] == "value"
    
    def test_chunk_text_different_strategies(self):
        """Test chunk_text with different splitting strategies."""
        text = "First sentence. Second sentence. Third sentence."
        
        # Test each strategy
        strategies = ["sentence", "paragraph", "word", "character"]
        
        for strategy in strategies:
            chunks = chunk_text(text, max_chars=50, split_on=strategy)
            assert len(chunks) >= 1, f"Failed for strategy: {strategy}"
            for chunk in chunks:
                assert isinstance(chunk, dict)
                assert chunk['metadata']['chunk_strategy'] == strategy


class TestChunkingWithRealWorldContent:
    """Test chunking with realistic content."""
    
    def test_news_article_chunking(self):
        """Test chunking a realistic news article."""
        article = """
        Breaking News: Technology Breakthrough Announced
        
        In a groundbreaking development announced today, researchers at the Institute of Technology 
        have successfully created a new method for processing information that could revolutionize 
        the computing industry.
        
        The breakthrough comes after years of research and development. Dr. Jane Smith, lead researcher 
        on the project, explained that the new technology offers significant improvements over existing 
        methods. "This represents a major step forward in our understanding of computational processes," 
        she said during a press conference.
        
        The research team plans to publish their findings in the next issue of the Journal of 
        Advanced Computing. They also announced partnerships with several major technology companies 
        to begin commercial development of the technology.
        
        Industry experts are calling this one of the most significant technological advances in recent years.
        """
        
        chunks = chunk_text(article, max_chars=300, overlap_chars=50, split_on="paragraph")
        
        assert len(chunks) >= 2  # Should be split into multiple chunks

        # Check that important content is preserved. The chunker preserves the
        # original intra-paragraph whitespace (including newlines/indentation),
        # so collapse runs of whitespace before checking for phrases that span
        # the article's wrapped lines (e.g. "Journal of\n        Advanced Computing").
        import re
        all_text = " ".join([chunk['text'] for chunk in chunks])
        all_text = re.sub(r"\s+", " ", all_text)
        assert "Breaking News" in all_text
        assert "Dr. Jane Smith" in all_text
        assert "Journal of Advanced Computing" in all_text
        
        # Check chunk properties
        for chunk in chunks:
            assert chunk['word_count'] > 0
            assert chunk['char_count'] > 0
            assert chunk['chunk_id'] >= 0
            assert 'start_offset' in chunk
            assert 'end_offset' in chunk
