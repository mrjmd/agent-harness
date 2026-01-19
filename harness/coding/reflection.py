#!/usr/bin/env python3
"""
Reflection Module

After a feature passes all checks, extract lessons learned for future agents.

This is knowledge transfer between context windows - what the agent learned
during this feature gets persisted so future features don't make the same mistakes.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

# Shared CLI module (add parent to path for import)
sys.path.insert(0, str(Path(__file__).parent.parent))
from cli import call_reflection as _call_claude_cli


LEARNINGS_PATH = Path("specs/learnings.json")


REFLECTION_PROMPT = """
You just completed feature: {feature_id}
Description: {description}

Review the iteration history below and extract lessons that would help future agents.

## What to look for:

1. **API Gotchas**: Did any APIs behave unexpectedly? Wrong parameters? Deprecated methods?
2. **Package Issues**: Any npm packages that didn't exist, had breaking changes, or surprising behavior?
3. **Pattern Discoveries**: What patterns worked well? What approaches failed initially?
4. **Environment Issues**: Config, paths, or environment problems worth noting?
5. **Test Insights**: Any tricks for making tests more reliable or easier to write?

## Output Format:

Return a JSON array of lessons. Each lesson should be:
- Specific and actionable
- Something that would save a future agent time
- Not obvious or trivial

Example:
```json
[
  {{
    "category": "api_gotcha",
    "lesson": "Playwright page.goto() requires full URL with protocol",
    "context": "Was passing '/login' but needed 'http://localhost:3000/login'"
  }},
  {{
    "category": "package_issue",
    "lesson": "@anthropic-ai/mcp-server-playwright package doesn't exist yet",
    "context": "Use community fork @anthropic/mcp-playwright instead"
  }}
]
```

Categories: api_gotcha, package_issue, pattern, environment, test_insight, blocker, other

If no significant lessons were learned (straightforward implementation), return:
```json
[]
```

## Iteration History:

{iteration_history}
"""


FAILURE_REFLECTION_PROMPT = """
Feature FAILED after multiple attempts: {feature_id}
Description: {description}

The feature could not be completed. Review the iteration history below and extract lessons about WHY it failed.

## What to look for:

1. **Root Cause**: What was the fundamental issue that prevented completion?
2. **Blockers**: Any missing dependencies, configurations, or infrastructure issues?
3. **Approach Issues**: Were there flawed assumptions or wrong approaches tried?
4. **Environment Issues**: Missing tools, services, or incorrect setup?
5. **Knowledge Gaps**: What information was missing that would have helped?

## Output Format:

Return a JSON array of lessons. Each lesson should be:
- Focused on what went wrong and why
- Actionable for future attempts
- Clear about the blocker or issue

Example:
```json
[
  {{
    "category": "blocker",
    "lesson": "Feature requires Redis but it's not running locally",
    "context": "Add Redis to docker-compose or document as prerequisite"
  }},
  {{
    "category": "approach_issue",
    "lesson": "Tried to mock database when integration test was needed",
    "context": "The test requires real database to validate constraints"
  }}
]
```

Categories: blocker, approach_issue, missing_info, environment, api_gotcha, package_issue, other

## Iteration History:

{iteration_history}
"""


@dataclass
class Learning:
    """A single lesson learned."""
    id: str
    timestamp: str
    feature_id: str
    category: str
    lesson: str
    context: str = ""


def call_claude_cli(prompt_text: str) -> str:
    """
    Call claude CLI with streaming output and read-only tool access.

    Uses the shared CLI module which provides:
    - Streaming output for real-time feedback
    - Read-only tools (Read, Glob, Grep)
    - 5 minute timeout for reflection
    """
    return _call_claude_cli(prompt_text)


def run_reflection(
    feature: dict,
    iteration_history: list[dict],
    failed: bool = False
) -> list[Learning]:
    """
    Extract lessons learned after feature completion or failure.

    Args:
        feature: The feature dict
        iteration_history: List of iteration records with prompts/responses/errors
        failed: If True, use failure-focused reflection prompt to extract
                lessons from what went wrong

    Returns:
        List of Learning objects extracted from reflection
    """
    # Format iteration history
    history_text = format_iteration_history(iteration_history)

    # Build prompt - use failure prompt if feature failed
    prompt_template = FAILURE_REFLECTION_PROMPT if failed else REFLECTION_PROMPT
    prompt = prompt_template.format(
        feature_id=feature.get("id", "unknown"),
        description=feature.get("description", ""),
        iteration_history=history_text
    )

    try:
        response_text = call_claude_cli(prompt)
        learnings = parse_learnings(response_text, feature, failed=failed)
        return learnings

    except Exception as e:
        print(f"Warning: Reflection failed: {e}")
        return []


def format_iteration_history(history: list[dict]) -> str:
    """Format iteration history for the reflection prompt."""
    if not history:
        return "(No iteration history available)"

    lines = []
    for i, iteration in enumerate(history, 1):
        lines.append(f"### Iteration {i}")

        if iteration.get("error"):
            lines.append(f"Error: {iteration['error'][:500]}")

        if iteration.get("response_summary"):
            lines.append(f"Response: {iteration['response_summary'][:500]}")

        if iteration.get("verification_result"):
            lines.append(f"Verification: {iteration['verification_result']}")

        lines.append("")

    return "\n".join(lines)


def parse_learnings(response_text: str, feature: dict, failed: bool = False) -> list[Learning]:
    """Parse the JSON array of learnings from the response."""

    # Try to extract JSON from the response
    json_match = re.search(r"\[[\s\S]*\]", response_text)
    if not json_match:
        return []

    try:
        raw_learnings = json.loads(json_match.group())
    except json.JSONDecodeError:
        print("Warning: Could not parse learnings JSON")
        return []

    if not isinstance(raw_learnings, list):
        return []

    learnings = []
    timestamp = datetime.now(timezone.utc).isoformat()

    for i, raw in enumerate(raw_learnings):
        if not isinstance(raw, dict):
            continue

        lesson_text = raw.get("lesson", "")
        if not lesson_text:
            continue

        # Add failure marker to ID if from failed feature
        id_suffix = f"-fail-{i+1}" if failed else f"-{i+1}"
        category = raw.get("category", "other")

        # Add context about this being from a failed attempt
        context = raw.get("context", "")
        if failed and context:
            context = f"[FROM FAILED ATTEMPT] {context}"
        elif failed:
            context = "[FROM FAILED ATTEMPT]"

        learning = Learning(
            id=f"learn-{feature.get('id', 'unknown')}{id_suffix}",
            timestamp=timestamp,
            feature_id=feature.get("id", "unknown"),
            category=category,
            lesson=lesson_text,
            context=context
        )
        learnings.append(learning)

    return learnings


def save_learnings(learnings: list[Learning], path: Path = None) -> None:
    """Append new learnings to the learnings file."""
    if path is None:
        path = LEARNINGS_PATH

    # Load existing learnings
    existing = []
    if path.exists():
        try:
            data = json.loads(path.read_text())
            existing = data.get("learnings", [])
        except (json.JSONDecodeError, KeyError):
            existing = []

    # Add new learnings
    for learning in learnings:
        existing.append(asdict(learning))

    # Save back
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"learnings": existing}, indent=2))

    print(f"Saved {len(learnings)} new learnings to {path}")


def load_learnings(path: Path = None) -> list[dict]:
    """Load all learnings from the learnings file."""
    if path is None:
        path = LEARNINGS_PATH

    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text())
        return data.get("learnings", [])
    except (json.JSONDecodeError, KeyError):
        return []


def get_relevant_learnings(feature: dict, max_items: int = 10, path: Path = None) -> str:
    """
    Get learnings relevant to the current feature.

    Returns formatted string for injection into prompts.
    """
    learnings = load_learnings(path)

    if not learnings:
        return ""

    # For now, just get the most recent learnings
    # Future: could filter by category relevance, keywords, etc.
    recent = learnings[-max_items:]

    lines = []
    for learning in recent:
        lesson = learning.get("lesson", "")
        if lesson:
            lines.append(f"- {lesson}")
            context = learning.get("context", "")
            if context:
                lines.append(f"  ({context})")

    return "\n".join(lines) if lines else ""


def clear_learnings(path: Path = None) -> None:
    """Clear all learnings (useful for testing)."""
    if path is None:
        path = LEARNINGS_PATH

    path.write_text(json.dumps({"learnings": []}, indent=2))


# Test the module
if __name__ == "__main__":
    print("Testing reflection module...")

    # Test parsing
    test_response = '''
    Here are the lessons learned:

    ```json
    [
      {
        "category": "api_gotcha",
        "lesson": "Test lesson 1",
        "context": "Some context"
      },
      {
        "category": "pattern",
        "lesson": "Test lesson 2"
      }
    ]
    ```
    '''

    feature = {"id": "test-feature", "description": "Test feature"}
    learnings = parse_learnings(test_response, feature)

    print(f"Parsed {len(learnings)} learnings:")
    for l in learnings:
        print(f"  - [{l.category}] {l.lesson}")
