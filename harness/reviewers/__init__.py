"""
Reviewers module - Pluggable review provider implementations.

Available providers:
- ManualReviewer: Clipboard-based manual review (default)
- GeminiReviewer: Automatic review via Gemini API

Future providers (planned):
- ClaudeReviewer: Claude API for reverse review
"""

from .base import ReviewProvider, ReviewResult, get_reviewer
from .manual import ManualReviewer
from .gemini import GeminiReviewer

__all__ = [
    "ReviewProvider",
    "ReviewResult",
    "get_reviewer",
    "ManualReviewer",
    "GeminiReviewer",
]
