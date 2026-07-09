"""Tests for src/ml/validation/input_validator.py.

Dependency-light unit coverage in the gated ``tests/unit/ml`` tree (matches the
``tests/unit`` convention). The validator is stdlib-only, so it runs in the
curated CI test environment without the ML stack.
"""

import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest  # noqa: E402

from ml.validation.input_validator import (  # noqa: E402
    InputValidator,
    ValidatedArticle,
    ValidationError,
)


def test_validate_accepts_and_normalizes():
    v = InputValidator()
    article = v.validate("  Headline  ", "  Some body content here.  ")
    assert isinstance(article, ValidatedArticle)
    assert article.title == "Headline"          # edges stripped
    assert article.content == "Some body content here."
    assert article.combined() == "Headline. Some body content here."


def test_none_inputs_are_rejected():
    v = InputValidator()
    with pytest.raises(ValidationError):
        v.validate(None, "content")
    with pytest.raises(ValidationError):
        v.validate("title", None)


def test_too_short_title_and_content_rejected():
    v = InputValidator(min_title_len=3, min_content_len=5)
    with pytest.raises(ValidationError):
        v.validate("ab", "long enough content")
    with pytest.raises(ValidationError):
        v.validate("title", "abcd")


def test_oversized_content_is_truncated_preserving_title():
    v = InputValidator(min_title_len=1, min_content_len=1, max_total_len=20)
    article = v.validate("title", "x" * 100)
    # title (5) preserved; content truncated to fit max_total_len (20)
    assert article.title == "title"
    assert len(article.title) + len(article.content) <= 20


def test_safe_captures_errors_instead_of_raising():
    v = InputValidator()
    article, errors = v.safe("ab", "")   # both too short
    assert errors                        # at least one error captured
    assert isinstance(article, ValidatedArticle)  # partial article, no raise


def test_safe_returns_no_errors_on_valid_input():
    v = InputValidator()
    article, errors = v.safe("Headline", "Some body content.")
    assert errors == []
    assert article.title == "Headline"
