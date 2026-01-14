"""
Reviewers module - Pluggable review provider implementations.

Currently implemented:
- ManualReviewer: Clipboard-based manual review

Future providers (not yet implemented):
- GeminiReviewer: Gemini API integration
- ClaudeReviewer: Claude API for reverse review
"""

from .base import ReviewProvider, ReviewResult
from .manual import ManualReviewer

__all__ = [
    "ReviewProvider",
    "ReviewResult",
    "ManualReviewer",
]
