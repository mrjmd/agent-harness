#!/usr/bin/env python3
"""
Checkpoint module for cadence-based holistic reviews.

Provides periodic "health check" checkpoints during the coding loop that:
- Run backlog reviews via the review board
- Support interactive (pause and prompt) or autonomous (auto-fix) modes
- Persist checkpoint history for audit trail
- Generate fix features from review feedback
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import json
import sys

# Import from parent for review board access
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from review_board import request_review, ReviewResult, is_enabled as review_board_enabled
    REVIEW_BOARD_AVAILABLE = True
except ImportError:
    REVIEW_BOARD_AVAILABLE = False

    @dataclass
    class ReviewResult:
        """Fallback when review board not available."""
        approved: bool
        feedback: str
        reviewer: str = "none"
        cycle: int = 1


# Paths
CHECKPOINT_HISTORY_PATH = Path("specs/checkpoint_history.json")
SPEC_PATHS = {
    "product_spec": Path("specs/product_spec.md"),
    "tech_plan": Path("specs/tech_plan.md"),
    "features": Path("specs/features.json"),
    "deferred_scope": Path("specs/deferred_scope.md"),
    "gate1": Path("specs/gates/gate1_problem.md"),
    "gate2": Path("specs/gates/gate2_solution.md"),
    "gate3": Path("specs/gates/gate3_technical.md"),
    "gate4": Path("specs/gates/gate4_edge_cases.md"),
}


@dataclass
class CheckpointRecord:
    """Record of a single checkpoint."""
    number: int
    timestamp: str
    features_passing: int
    features_total: int
    review_approved: bool
    issues_found: list = field(default_factory=list)
    fixes_generated: list = field(default_factory=list)
    was_interrupt: bool = False


def load_checkpoint_history() -> list[dict]:
    """Load checkpoint history from file."""
    if CHECKPOINT_HISTORY_PATH.exists():
        try:
            return json.loads(CHECKPOINT_HISTORY_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return []


def save_checkpoint_history(record: CheckpointRecord) -> None:
    """Append checkpoint record to history file."""
    history = load_checkpoint_history()

    history.append({
        "number": record.number,
        "timestamp": record.timestamp,
        "features_passing": record.features_passing,
        "features_total": record.features_total,
        "review_approved": record.review_approved,
        "issues_found": record.issues_found,
        "fixes_generated": record.fixes_generated,
        "was_interrupt": record.was_interrupt,
    })

    # Ensure directory exists
    CHECKPOINT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_HISTORY_PATH.write_text(json.dumps(history, indent=2))


def collect_spec_context() -> str:
    """
    Collect all spec documents for review context.

    Reads gate docs, deferred scope, product spec, tech plan, etc.
    """
    parts = []

    for name, path in SPEC_PATHS.items():
        if path.exists():
            try:
                if path.suffix == ".json":
                    content = json.dumps(json.loads(path.read_text()), indent=2)
                else:
                    content = path.read_text()

                # Truncate very large files
                if len(content) > 5000:
                    content = content[:5000] + "\n... (truncated)"

                parts.append(f"=== {name.upper()} ===\n{content}\n")
            except Exception as e:
                parts.append(f"=== {name.upper()} ===\n(Error reading: {e})\n")

    return "\n".join(parts) if parts else "(No spec documents found)"


def format_checkpoint_output(features: list[dict]) -> str:
    """Format features list for review."""
    # Include full feature list
    output_parts = ["=== FEATURES BACKLOG ===\n"]

    for f in features:
        output_parts.append(f"Feature: {f.get('id', 'unknown')}")
        output_parts.append(f"  Status: {f.get('status', 'unknown')}")
        output_parts.append(f"  Priority: {f.get('priority', 'N/A')}")
        output_parts.append(f"  Description: {f.get('description', 'N/A')}")

        if f.get("blocked"):
            output_parts.append(f"  BLOCKED: {f.get('block_reason', 'unknown reason')}")

        if f.get("retries", 0) > 0:
            output_parts.append(f"  Retries: {f.get('retries')}")

        output_parts.append("")

    return "\n".join(output_parts)


def run_checkpoint_review(features: list[dict]) -> ReviewResult:
    """Run the backlog review and return result."""
    if not REVIEW_BOARD_AVAILABLE or not review_board_enabled():
        return ReviewResult(
            approved=True,
            feedback="Review board not available - skipping review",
            reviewer="none"
        )

    # Collect spec context
    context_str = collect_spec_context()
    output = format_checkpoint_output(features)

    return request_review(
        stage="backlog",
        context={"spec_documents": "See output section"},
        output=f"{context_str}\n\n{output}"
    )


def extract_critical_issues(feedback: str) -> list[str]:
    """
    Extract critical (blocking) issues from feedback.

    Looks for patterns like "CRITICAL:", "BLOCKING:", severity markers.
    """
    critical_keywords = [
        "CRITICAL",
        "BLOCKING",
        "MUST FIX",
        "REQUIRED",
        "BREAKING",
        "SECURITY",
        "MISSING REQUIRED",
    ]

    issues = []
    lines = feedback.split("\n")

    for line in lines:
        line_upper = line.upper()
        for keyword in critical_keywords:
            if keyword in line_upper:
                issues.append(line.strip())
                break

    return issues


def generate_fix_features(feedback: str, next_priority: int) -> list[dict]:
    """
    Parse review feedback and generate fix features.

    Creates new features for identified issues with appropriate priority.
    """
    features = []

    # Look for ISSUE patterns in feedback
    lines = feedback.split("\n")
    issue_buffer = []
    in_issue = False
    issue_count = 0

    for line in lines:
        if line.strip().startswith("ISSUE") or line.strip().startswith("Issue"):
            if issue_buffer:
                # Process previous issue
                issue_text = "\n".join(issue_buffer)
                feature = _create_fix_feature(issue_text, issue_count, next_priority + issue_count)
                if feature:
                    features.append(feature)

            issue_buffer = [line]
            in_issue = True
            issue_count += 1
        elif in_issue:
            if line.strip() == "" and len(issue_buffer) > 3:
                # End of issue block
                issue_text = "\n".join(issue_buffer)
                feature = _create_fix_feature(issue_text, issue_count, next_priority + issue_count - 1)
                if feature:
                    features.append(feature)
                issue_buffer = []
                in_issue = False
            else:
                issue_buffer.append(line)

    # Process final issue if any
    if issue_buffer:
        issue_text = "\n".join(issue_buffer)
        feature = _create_fix_feature(issue_text, issue_count, next_priority + issue_count - 1)
        if feature:
            features.append(feature)

    return features


def _create_fix_feature(issue_text: str, issue_num: int, priority: int) -> Optional[dict]:
    """Create a fix feature from issue text."""
    # Extract key parts from issue
    lines = issue_text.strip().split("\n")
    if not lines:
        return None

    # First line is the issue header
    header = lines[0]

    # Try to extract problem and recommendation
    problem = ""
    recommendation = ""

    for line in lines[1:]:
        lower = line.lower()
        if "problem:" in lower:
            problem = line.split(":", 1)[1].strip() if ":" in line else line
        elif "recommendation:" in lower:
            recommendation = line.split(":", 1)[1].strip() if ":" in line else line

    # Create description from available parts
    if recommendation:
        description = recommendation
    elif problem:
        description = f"Fix: {problem}"
    else:
        description = header.split(":", 1)[1].strip() if ":" in header else header

    # Generate ID
    feature_id = f"fix-checkpoint-{issue_num:03d}"

    return {
        "id": feature_id,
        "description": description[:200],
        "status": "todo",
        "priority": priority,
        "acceptance_criteria": f"Address the issue: {problem[:100]}..." if problem else "Address the checkpoint review issue",
        "edge_cases": [
            {"id": "ec1", "scenario": "Original issue scenario", "expected_behavior": "Issue resolved"},
            {"id": "ec2", "scenario": "Related edge case", "expected_behavior": "No regression"},
            {"id": "ec3", "scenario": "Null/empty input", "expected_behavior": "Graceful handling"},
        ],
        "source": "checkpoint_review",
        "original_issue": issue_text[:500],
    }


def handle_interrupt_options() -> Optional[str]:
    """
    Handle interrupt options in interactive mode.

    Returns:
        "review" - Run checkpoint review
        "continue" - Continue to next feature
        None - Stop the loop
    """
    print("\nInterrupt received. Options:")
    print("[1] Run checkpoint review now")
    print("[2] Continue to next feature")
    print("[3] Stop (save progress)")

    try:
        choice = input("\nChoice [1/2/3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if choice == "1":
        return "review"
    elif choice == "2":
        return "continue"
    else:
        return None
