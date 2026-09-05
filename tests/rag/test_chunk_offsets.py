"""Citation-coordinate regressions for #1415."""

import pytest

from services.rag.chunking import ChunkConfig, DocumentSection, SplitStrategy, TextChunker


@pytest.mark.parametrize("strategy", [SplitStrategy.SENTENCE, SplitStrategy.PARAGRAPH, SplitStrategy.WORD, SplitStrategy.CHARACTER])
@pytest.mark.parametrize("overlap", [0, 15, 1000])
def test_original_unicode_slices_with_repeated_prefixes_and_whitespace(strategy, overlap):
    text = "  Repeated introduction with the same first fifty characters. Résultat α!\n\n\tRepeated introduction with the same first fifty characters. Résultat β?  \nEnd."
    chunker = TextChunker(ChunkConfig(max_chars=80, min_chunk_chars=1, overlap_chars=overlap,
                                     split_on=strategy, preserve_sentences=False))
    chunks = chunker.chunk_text(text)
    assert len(chunks) >= 2
    assert all(text[c.start_offset:c.end_offset] == c.text for c in chunks)
    assert all(c.char_count <= 80 and c.metadata["coordinate_system"] == "input-unicode-codepoints" for c in chunks)
    assert all(a.start_offset < b.start_offset for a, b in zip(chunks, chunks[1:]))
    covered = {i for c in chunks for i in range(c.start_offset, c.end_offset)}
    assert all(i in covered for i, char in enumerate(text) if not char.isspace())


def test_structured_offsets_are_explicitly_section_relative():
    chunker = TextChunker(ChunkConfig(max_chars=30, min_chunk_chars=1, overlap_chars=0,
                                     split_on=SplitStrategy.STRUCTURED, preserve_sentences=False))
    sections = [DocumentSection(title="One", text="  First section!  "),
                DocumentSection(title="Two", text="  A different section? ")]
    chunks = chunker.chunk_document(sections)
    assert [c.path for c in chunks] == [["One"], ["Two"]]
    for chunk, section in zip(chunks, sections):
        assert chunk.text == section.text[chunk.start_offset:chunk.end_offset]
        assert chunk.metadata["coordinate_system"] == "section-unicode-codepoints"


def test_single_oversized_word_terminates_and_preserves_all_characters():
    text = "界" * 301
    chunks = TextChunker(ChunkConfig(max_chars=20, min_chunk_chars=1, overlap_chars=0,
                                   split_on=SplitStrategy.WORD, preserve_sentences=False)).chunk_text(text)
    assert "".join(c.text for c in chunks) == text
    assert all(c.char_count <= 20 for c in chunks)
