"""
Reviewers module - Pluggable review provider implementations.

This module provides the abstraction layer for different review providers:
- ManualReviewer: Clipboard-based manual review (Phase 1)
- GeminiReviewer: Gemini API integration (Phase 3)
- ClaudeReviewer: Claude API for reverse review (Phase 3)
"""

from .base import ReviewProvider, ReviewResult
from .manual import ManualReviewer

__all__ = [
    "ReviewProvider",
    "ReviewResult",
    "ManualReviewer",
]
