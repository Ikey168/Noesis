"""Personal daily briefs: compose the warehouse into one readable digest.

The brief is the "front door" over the analytics warehouse: an interest
profile (topics + keywords) drives per-topic sections that pull together
recent news/blog coverage, mined claims and fact-check verdicts,
contradictions, day-bucketed timelines, and deeper reading (papers, books,
transcripts, notes) from the same corpus.
"""

from src.briefs.profile import InterestProfile, InterestTopic, load_profile
from src.briefs.daily import generate_daily_brief, render_markdown

__all__ = [
    "InterestProfile",
    "InterestTopic",
    "load_profile",
    "generate_daily_brief",
    "render_markdown",
]
