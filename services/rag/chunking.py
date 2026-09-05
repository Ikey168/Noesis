"""
Text Chunking Module
Issue #229: Chunking & normalization pipeline

This module handles the chunking of normalized articles into appropriately sized chunks
with configurable parameters like max_chars, overlap, and language-aware splitting.
"""

import logging
import re
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

try:
    import spacy
    from spacy.lang.en import English
except ImportError:
    spacy = None
    English = None

logger = logging.getLogger(__name__)


class SplitStrategy(Enum):
    """Strategies for text splitting."""
    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"
    WORD = "word"
    CHARACTER = "character"
    # Hierarchical, structure-aware splitting over a section/chapter tree. Each
    # resulting chunk carries its section path so retrieval can cite a location
    # (e.g. a paper section or a book chapter). Used via chunk_document().
    STRUCTURED = "structured"


@dataclass
class ChunkConfig:
    """Configuration for text chunking."""
    max_chars: int = 1000
    overlap_chars: int = 100
    split_on: SplitStrategy = SplitStrategy.SENTENCE
    min_chunk_chars: int = 50
    preserve_sentences: bool = True
    language: str = "en"
    # When split_on == STRUCTURED, each section's body is chunked with this flat
    # leaf strategy. Lets long documents (papers, books) stay section-aware while
    # reusing the existing sentence/paragraph/word/character splitters.
    leaf_split_on: SplitStrategy = SplitStrategy.SENTENCE


@dataclass
class DocumentSection:
    """A node in a document's section/chapter tree for structured chunking.

    ``title`` is the section/chapter heading, ``text`` its body (may be empty
    for container nodes like a book Part), and ``children`` its subsections.
    """
    title: str = ""
    text: str = ""
    children: List["DocumentSection"] = field(default_factory=list)

    @classmethod
    def from_pairs(cls, pairs: List[Tuple[str, str]]) -> List["DocumentSection"]:
        """Build a flat list of sections from (title, text) pairs."""
        return [cls(title=title, text=text) for title, text in pairs]


@dataclass
class TextChunk:
    """Represents a chunk of text with metadata."""
    text: str
    start_offset: int
    end_offset: int
    chunk_id: int
    word_count: int
    char_count: int
    metadata: Dict[str, Any]
    # Section breadcrumb for structure-aware chunks, e.g. ["4", "4.2 Methods"]
    # or ["Part II", "Chapter 5"]. Empty for flat (non-structured) chunks.
    path: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert chunk to dictionary representation."""
        return {
            'text': self.text,
            'start_offset': self.start_offset,
            'end_offset': self.end_offset,
            'chunk_id': self.chunk_id,
            'word_count': self.word_count,
            'char_count': self.char_count,
            'metadata': self.metadata,
            'path': list(self.path),
        }


class TextChunker:
    """
    Language-aware text chunker that splits text into appropriately sized chunks.
    
    Features:
    - Configurable chunk size and overlap
    - Language-aware sentence splitting
    - Multiple splitting strategies
    - Metadata preservation
    - Offset tracking for reconstruction
    """
    
    def __init__(self, config: Optional[ChunkConfig] = None, *, sentence_segmenter=None):
        """
        Initialize text chunker.
        
        Args:
            config: Chunking configuration
        """
        self.config = config or ChunkConfig()
        self.sentence_segmenter = sentence_segmenter
        if self.config.max_chars <= 0 or self.config.overlap_chars < 0 or self.config.min_chunk_chars < 0:
            raise ValueError("chunk bounds must be positive/nonnegative")
        
        # Initialize language model for sentence splitting
        self.nlp = None
        if spacy and self.config.preserve_sentences:
            try:
                # Try to load language-specific model
                if self.config.language == "en":
                    self.nlp = English()
                    self.nlp.add_pipe("sentencizer")
                else:
                    from spacy.util import get_lang_class
                    self.nlp = get_lang_class(self.config.language)()
                    self.nlp.add_pipe("sentencizer")

            except Exception as e:
                logger.warning(f"Failed to load spacy model: {e}, using regex fallback")
                self.nlp = None
        
        # Fallback regex patterns for sentence splitting
        self.sentence_endings = re.compile(r'[.!?]+\s+')
        self.paragraph_endings = re.compile(r'\n\s*\n')
        
    def chunk_text(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> List[TextChunk]:
        """
        Chunk text into appropriately sized pieces.
        
        Args:
            text: Text to chunk
            metadata: Optional metadata to include with chunks
            
        Returns:
            List of TextChunk objects
        """
        if not text or not text.strip():
            return []
        
        metadata = metadata or {}
        
        # Choose splitting strategy
        if self.config.split_on == SplitStrategy.SENTENCE:
            return self._chunk_by_sentences(text, metadata)
        elif self.config.split_on == SplitStrategy.PARAGRAPH:
            return self._chunk_by_paragraphs(text, metadata)
        elif self.config.split_on == SplitStrategy.WORD:
            return self._chunk_by_words(text, metadata)
        elif self.config.split_on == SplitStrategy.STRUCTURED:
            # No section tree available for a plain string: treat the whole text
            # as a single untitled section so callers still get chunks.
            return self.chunk_document([DocumentSection(text=text)], metadata)
        else:  # CHARACTER
            return self._chunk_by_characters(text, metadata)

    def chunk_document(
        self,
        sections: List[DocumentSection],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[TextChunk]:
        """Chunk a section/chapter tree, tagging each chunk with its path.

        Each section's body is split with the configured leaf strategy; every
        resulting chunk carries the breadcrumb of titles from the root to that
        section (e.g. ["4", "4.2 Methods"]) so retrieval can cite the location.
        Container sections with no body still contribute their title to the path
        of their descendants.
        """
        metadata = metadata or {}
        chunks: List[TextChunk] = []
        counter = 0

        def walk(node: DocumentSection, ancestors: List[str]) -> None:
            nonlocal counter
            path = ancestors + [node.title] if node.title else list(ancestors)
            if node.text and node.text.strip():
                for chunk in self._chunk_leaf(node.text, metadata):
                    chunk.chunk_id = counter
                    counter += 1
                    chunk.path = list(path)
                    chunk.metadata = {
                        **chunk.metadata,
                        'chunk_strategy': SplitStrategy.STRUCTURED.value,
                        'section_path': list(path),
                        'coordinate_system': 'section-unicode-codepoints',
                    }
                    chunks.append(chunk)
            for child in node.children:
                walk(child, path)

        for section in sections:
            walk(section, [])
        return chunks

    def _chunk_leaf(self, text: str, metadata: Dict[str, Any]) -> List[TextChunk]:
        """Chunk a single section body using the configured flat leaf strategy."""
        strategy = self.config.leaf_split_on
        if strategy == SplitStrategy.PARAGRAPH:
            return self._chunk_by_paragraphs(text, metadata)
        elif strategy == SplitStrategy.WORD:
            return self._chunk_by_words(text, metadata)
        elif strategy == SplitStrategy.CHARACTER:
            return self._chunk_by_characters(text, metadata)
        else:  # SENTENCE (default leaf strategy)
            return self._chunk_by_sentences(text, metadata)
    
    def _chunk_by_sentences(self, text: str, metadata: Dict[str, Any]) -> List[TextChunk]:
        return self._chunk_on_spans(text, self._sentence_spans(text), metadata)
    
    def _chunk_by_paragraphs(self, text: str, metadata: Dict[str, Any]) -> List[TextChunk]:
        spans, start = [], 0
        for separator in self.paragraph_endings.finditer(text):
            spans.append((start, separator.start()))
            start = separator.end()
        spans.append((start, len(text)))
        return self._chunk_on_spans(text, spans, metadata)
    
    def _chunk_by_words(self, text: str, metadata: Dict[str, Any]) -> List[TextChunk]:
        return self._chunk_on_spans(text, [match.span() for match in re.finditer(r"\S+", text)], metadata)
    
    def _chunk_by_characters(self, text: str, metadata: Dict[str, Any]) -> List[TextChunk]:
        return self._chunk_on_spans(text, [], metadata)
    
    def _split_into_sentences(self, text: str) -> List[str]:
        """Return original sentence slices without inventing punctuation."""
        return [text[start:end] for start, end in self._sentence_spans(text)]

    def _sentence_spans(self, text: str) -> List[Tuple[int, int]]:
        if self.sentence_segmenter is not None:
            spans = list(self.sentence_segmenter(text))
            previous = 0
            for start, end in spans:
                if not 0 <= previous <= start < end <= len(text) or text[previous:start].strip():
                    raise ValueError("segmenter returned invalid or incomplete source offsets")
                previous = end
            if text[previous:].strip():
                raise ValueError("segmenter omitted source text")
            return spans
        if self.nlp:
            try:
                return [(sent.start_char, sent.end_char) for sent in self.nlp(text).sents]
            except Exception as exc:
                logger.warning("Sentence splitting failed: %s; using regex", exc)
        spans, start = [], 0
        for separator in re.finditer(r"(?<=[.!?])\s+", text):
            spans.append((start, separator.start()))
            start = separator.end()
        if start < len(text):
            spans.append((start, len(text)))
        return spans

    def _chunk_on_spans(self, text: str, spans: List[Tuple[int, int]], metadata: Dict[str, Any]) -> List[TextChunk]:
        """Choose boundaries in original text; every chunk is an exact slice.

        Oversized units split at the character bound, including a single long
        word. Overlap prefers complete units and always advances. No normalized
        string search is used to reconstruct a citation location.
        """
        from bisect import bisect_left, bisect_right

        units = []
        for start, end in spans:
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if start < end:
                units.append((start, end))
        starts = [span[0] for span in units]
        ends = [span[1] for span in units]
        chunks, start = [], 0
        while start < len(text):
            while start < len(text) and text[start].isspace():
                start += 1
            if start == len(text):
                break
            hard_end = min(len(text), start + self.config.max_chars)
            boundary = bisect_right(ends, hard_end) - 1
            end = ends[boundary] if boundary >= 0 and ends[boundary] > start else hard_end
            trimmed_end = end
            while trimmed_end > start and text[trimmed_end - 1].isspace():
                trimmed_end -= 1
            if (chunks and end == len(text) and 0 < trimmed_end - start < self.config.min_chunk_chars
                    and self.config.min_chunk_chars <= self.config.max_chars):
                # Retain a short final tail by extending its source slice into
                # the previous chunk, rather than dropping the last evidence.
                target = max(chunks[-1].start_offset + 1, trimmed_end - self.config.min_chunk_chars)
                unit = bisect_right(starts, target) - 1
                candidate = starts[unit] if unit >= 0 else target
                if candidate <= chunks[-1].start_offset or trimmed_end - candidate > self.config.max_chars:
                    candidate = target
                start = min(start, candidate)
            if trimmed_end - start >= self.config.min_chunk_chars:
                chunks.append(self._create_chunk(text[start:trimmed_end], start, len(chunks), metadata))
            if end == len(text):
                break
            overlap_start = end - self.config.overlap_chars
            if units:
                index = bisect_left(starts, overlap_start)
                overlap_start = starts[index] if index < len(starts) else end
            start = overlap_start if start < overlap_start < end else end
        return chunks
    
    def _get_overlap_sentences(self, sentences: List[str], current_index: int, current_chunk: str) -> str:
        """Get sentences for overlap based on overlap_chars."""
        if self.config.overlap_chars == 0:
            return ""
        
        # Calculate how many sentences to include for overlap
        overlap_length = 0
        overlap_sentences = []
        
        # Work backwards from current position
        for i in range(current_index - 1, -1, -1):
            sentence = sentences[i]
            if overlap_length + len(sentence) <= self.config.overlap_chars:
                overlap_sentences.insert(0, sentence)
                overlap_length += len(sentence)
            else:
                break
        
        return "".join(overlap_sentences)
    
    def _find_text_position(self, full_text: str, chunk_text: str) -> int:
        """Legacy exact lookup; chunk construction carries its own coordinates."""
        position = full_text.find(chunk_text.strip())
        if position < 0:
            raise ValueError("chunk is not a slice of the source text")
        return position
    
    def _create_chunk(self, text: str, start_offset: int, chunk_id: int, metadata: Dict[str, Any]) -> TextChunk:
        """Create a TextChunk object."""
        word_count = len(text.split())
        char_count = len(text)
        end_offset = start_offset + char_count
        
        chunk_metadata = metadata.copy()
        chunk_metadata.update({
            'chunk_strategy': self.config.split_on.value,
            'coordinate_system': 'input-unicode-codepoints',
            'max_chars': self.config.max_chars,
            'overlap_chars': self.config.overlap_chars,
        })
        
        return TextChunk(
            text=text,
            start_offset=start_offset,
            end_offset=end_offset,
            chunk_id=chunk_id,
            word_count=word_count,
            char_count=char_count,
            metadata=chunk_metadata,
        )


def chunk_text(
    text: str,
    max_chars: int = 1000,
    overlap_chars: int = 100,
    split_on: str = "sentence",
    metadata: Optional[Dict[str, Any]] = None,
    **kwargs
) -> List[Dict[str, Any]]:
    """
    Convenience function to chunk text.
    
    Args:
        text: Text to chunk
        max_chars: Maximum characters per chunk
        overlap_chars: Overlap between chunks
        split_on: Splitting strategy ("sentence", "paragraph", "word", "character")
        metadata: Optional metadata to include
        **kwargs: Additional configuration options
        
    Returns:
        List of chunk dictionaries
    """
    # Set reasonable defaults for convenience function
    config_kwargs = {
        'min_chunk_chars': kwargs.pop('min_chunk_chars', 10),  # Lower default
        **kwargs
    }
    
    config = ChunkConfig(
        max_chars=max_chars,
        overlap_chars=overlap_chars,
        split_on=SplitStrategy(split_on),
        **config_kwargs
    )
    
    chunker = TextChunker(config)
    chunks = chunker.chunk_text(text, metadata)

    return [chunk.to_dict() for chunk in chunks]


def chunk_document(
    sections,
    max_chars: int = 1000,
    overlap_chars: int = 100,
    leaf_split_on: str = "sentence",
    metadata: Optional[Dict[str, Any]] = None,
    **kwargs
) -> List[Dict[str, Any]]:
    """
    Convenience function for structure-aware chunking.

    Args:
        sections: A list of DocumentSection nodes, or a list of (title, text)
            pairs (which are treated as a flat list of sections).
        max_chars: Maximum characters per chunk.
        overlap_chars: Overlap between chunks.
        leaf_split_on: Flat strategy used within each section
            ("sentence", "paragraph", "word", "character").
        metadata: Optional metadata to include.
        **kwargs: Additional ChunkConfig options.

    Returns:
        List of chunk dictionaries, each including a ``path`` breadcrumb.
    """
    normalized: List[DocumentSection] = []
    for section in sections:
        if isinstance(section, DocumentSection):
            normalized.append(section)
        else:  # (title, text) pair
            title, text = section
            normalized.append(DocumentSection(title=title, text=text))

    config_kwargs = {
        'min_chunk_chars': kwargs.pop('min_chunk_chars', 10),
        **kwargs
    }
    config = ChunkConfig(
        max_chars=max_chars,
        overlap_chars=overlap_chars,
        split_on=SplitStrategy.STRUCTURED,
        leaf_split_on=SplitStrategy(leaf_split_on),
        **config_kwargs
    )

    chunker = TextChunker(config)
    chunks = chunker.chunk_document(normalized, metadata)

    return [chunk.to_dict() for chunk in chunks]
