"""
Gemini API reviewer implementation.

Provides automatic review using Google's Gemini API, eliminating
the manual clipboard workflow.

Requires:
    - google-generativeai package: pip install google-generativeai
    - API key in GOOGLE_API_KEY or GEMINI_API_KEY environment variable
"""

import os
from typing import Optional

from .base import ReviewResult


class GeminiReviewer:
    """
    Review provider using Gemini API.

    Usage:
        reviewer = GeminiReviewer()
        result = reviewer.review("architect", context, output)

    Configuration:
        Set GOOGLE_API_KEY or GEMINI_API_KEY environment variable.
        Optionally configure model in .claude/config.json:
        {
            "reviewBoard": {
                "provider": "gemini",
                "geminiModel": "gemini-2.0-flash"
            }
        }
    """

    def __init__(self, model: str = "gemini-2.0-flash"):
        """
        Initialize GeminiReviewer.

        Args:
            model: Gemini model name (default: gemini-2.0-flash)
        """
        self.model_name = model
        self._client = None

    @property
    def name(self) -> str:
        """Return provider name."""
        return "gemini"

    def _get_client(self):
        """
        Lazy-load Gemini client.

        Raises:
            ImportError: If google-generativeai package not installed
            ValueError: If API key not configured
        """
        if self._client is None:
            try:
                import google.generativeai as genai
            except ImportError:
                raise ImportError(
                    "google-generativeai package required for Gemini reviewer.\n"
                    "Install with: pip install google-generativeai"
                )

            api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise ValueError(
                    "Gemini API key required.\n"
                    "Set GOOGLE_API_KEY or GEMINI_API_KEY environment variable:\n"
                    "  export GOOGLE_API_KEY='your-api-key'"
                )

            genai.configure(api_key=api_key)
            self._client = genai.GenerativeModel(self.model_name)

        return self._client

    def review(
        self,
        stage: str,
        context: dict,
        output: str,
        cycle: int = 1
    ) -> ReviewResult:
        """
        Send to Gemini API and parse response.

        Args:
            stage: Review stage ("architect", "implementer", "backlog", etc.)
            context: Context dictionary for the review
            output: The output being reviewed
            cycle: Current review cycle (1-based)

        Returns:
            ReviewResult with approval status and feedback
        """
        try:
            client = self._get_client()

            # Build prompt using stage-specific template
            prompt = self._build_prompt(stage, context, output)

            # Call Gemini API
            response = client.generate_content(prompt)
            response_text = response.text

            # Parse response for approval/rejection
            approved, concerns = self._parse_response(response_text)

            return ReviewResult(
                approved=approved,
                feedback=response_text if not approved else "",
                reviewer=self.name,
                cycle=cycle,
                concerns=concerns
            )

        except ImportError as e:
            # Package not installed
            return ReviewResult(
                approved=False,
                feedback=str(e),
                reviewer=self.name,
                cycle=cycle,
                concerns=[{"title": "Setup Required", "details": [str(e)]}]
            )

        except ValueError as e:
            # API key not configured
            return ReviewResult(
                approved=False,
                feedback=str(e),
                reviewer=self.name,
                cycle=cycle,
                concerns=[{"title": "Configuration Required", "details": [str(e)]}]
            )

        except Exception as e:
            # API error or other failure
            return ReviewResult(
                approved=False,
                feedback=f"Gemini API error: {e}",
                reviewer=self.name,
                cycle=cycle,
                concerns=[{"title": "API Error", "details": [str(e)]}]
            )

    def _build_prompt(self, stage: str, context: dict, output: str) -> str:
        """
        Build stage-specific prompt.

        Imports prompts from review_board to avoid duplication.

        Args:
            stage: Review stage name
            context: Context dictionary
            output: Output being reviewed

        Returns:
            Formatted prompt string
        """
        # Import prompts from review_board to stay DRY
        try:
            from ..review_board import REVIEW_PROMPTS, GENERIC_PROMPT
            template = REVIEW_PROMPTS.get(stage, GENERIC_PROMPT)
        except ImportError:
            # Fallback if import fails
            template = """You are reviewing the following output.

OUTPUT:
{output}

CONTEXT:
{context}

Respond with APPROVED if acceptable, or REVISE with specific feedback.
"""

        # Format context as readable string
        if isinstance(context, dict):
            context_str = "\n".join(f"- {k}: {v}" for k, v in context.items())
        else:
            context_str = str(context)

        return template.format(context=context_str, output=output)

    def _parse_response(self, text: str) -> tuple[bool, list[dict]]:
        """
        Parse Gemini response for approval and concerns.

        Looks for keywords like APPROVED, LGTM, REVISE, CHANGES_REQUESTED.
        Extracts structured concerns when present.

        Args:
            text: Raw response text from Gemini

        Returns:
            Tuple of (approved: bool, concerns: list[dict])
        """
        text_upper = text.upper()

        # Check for approval keywords
        approved = any(kw in text_upper for kw in ["LGTM", "APPROVED", "PASS"])

        # Check for rejection keywords (overrides approval)
        if any(kw in text_upper for kw in ["CHANGES_REQUESTED", "REVISE", "REJECT", "FAIL"]):
            approved = False

        # Parse structured concerns
        concerns = self._extract_concerns(text)

        return approved, concerns

    def _extract_concerns(self, text: str) -> list[dict]:
        """
        Extract structured concerns from response text.

        Looks for patterns like:
            CONCERN 1: Title
            - Detail 1
            - Detail 2

            ISSUE 1: [Feature ID]
            - Problem: ...

        Args:
            text: Response text to parse

        Returns:
            List of concern dictionaries with title and details
        """
        concerns = []
        lines = text.split("\n")
        current_concern: Optional[dict] = None

        for line in lines:
            line_upper = line.upper().strip()

            # Match "CONCERN N:" or "ISSUE N:" patterns
            if line_upper.startswith(("CONCERN", "ISSUE")):
                # Save previous concern
                if current_concern:
                    concerns.append(current_concern)

                # Extract title (everything after the colon)
                if ":" in line:
                    title = line.split(":", 1)[1].strip()
                else:
                    title = line.strip()

                current_concern = {"title": title, "details": []}

            elif current_concern and line.strip().startswith("-"):
                # Add detail to current concern
                detail = line.strip()[1:].strip()
                if detail:
                    current_concern["details"].append(detail)

        # Don't forget the last concern
        if current_concern:
            concerns.append(current_concern)

        return concerns
