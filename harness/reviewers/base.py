"""
Base protocol for review providers.

This module defines the interface that all review providers must implement.
Using Protocol allows for structural subtyping - any class with the right
methods will work, without requiring explicit inheritance.
"""

from dataclasses import dataclass
from typing import Protocol, Optional


@dataclass
class ReviewResult:
    """
    Result of a review request.

    Attributes:
        approved: True if the review passed
        feedback: Reviewer's feedback (empty if approved)
        reviewer: Identifier for the reviewer ("manual", "gemini", "claude", etc.)
        cycle: Which review cycle this result is from (1-based)
        concerns: Optional list of specific concerns raised
    """
    approved: bool
    feedback: str
    reviewer: str = "unknown"
    cycle: int = 1
    concerns: Optional[list[dict]] = None

    def __post_init__(self):
        if self.concerns is None:
            self.concerns = []


class ReviewProvider(Protocol):
    """
    Protocol for review providers.

    Implementations must provide a `review` method that takes stage information,
    context, and output, and returns a ReviewResult.

    Example implementations:
        - ManualReviewer: Clipboard-based manual review
        - GeminiReviewer: Gemini API integration
        - ClaudeReviewer: Claude API integration
        - MockReviewer: For testing
    """

    def review(
        self,
        stage: str,
        context: dict,
        output: str,
        cycle: int = 1
    ) -> ReviewResult:
        """
        Request a review.

        Args:
            stage: The review stage ("architect", "implementer", etc.)
            context: Dictionary of context for the review
            output: The output being reviewed
            cycle: Current review cycle (1-based)

        Returns:
            ReviewResult with approval status and feedback
        """
        ...

    @property
    def name(self) -> str:
        """Return the provider name."""
        ...


def get_reviewer(provider: str = "manual") -> ReviewProvider:
    """
    Factory function to get a reviewer by name.

    Args:
        provider: Provider name ("manual", "gemini", "claude")

    Returns:
        A ReviewProvider instance

    Raises:
        ValueError: If provider is not recognized
    """
    if provider == "manual":
        from .manual import ManualReviewer
        return ManualReviewer()

    # Future providers will be added here
    # elif provider == "gemini":
    #     from .gemini import GeminiReviewer
    #     return GeminiReviewer()

    raise ValueError(f"Unknown review provider: {provider}")
