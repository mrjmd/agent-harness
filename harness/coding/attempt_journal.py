#!/usr/bin/env python3
"""
Attempt Journal - Per-Feature Iteration Tracking

Tracks every attempt within a feature's implementation lifecycle:
- What approaches were tried
- What specific errors occurred
- What the agent claimed vs what actually happened
- Review exchanges with cross-model reviewers

This enables:
1. Loop detection (same error appearing repeatedly)
2. Cross-reviewer memory (persisting feedback history)
3. Adaptive context injection (more history when stuck)
4. Post-feature lesson extraction from attempt patterns
"""

import hashlib
import json
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Storage paths
ATTEMPTS_DIR = Path("specs/memory/attempts")
ARCHIVE_DIR = ATTEMPTS_DIR / "archive"


@dataclass
class VerificationResult:
    """Result of external test verification."""
    passed: bool
    error_signature: str = ""
    error_hash: str = ""
    test_output_hash: str = ""
    failed_tests: list = field(default_factory=list)


@dataclass
class ReviewExchange:
    """A single review exchange between agent and reviewer."""
    cycle: int
    reviewer: str  # "gemini", "manual", etc.
    submission_summary: str
    key_decisions: list = field(default_factory=list)
    approved: bool = False
    concerns: list = field(default_factory=list)
    feedback_summary: str = ""
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class Attempt:
    """A single iteration attempt within a feature."""
    attempt_num: int
    timestamp: str
    approach_summary: str = ""
    files_modified: list = field(default_factory=list)
    claimed_complete: bool = False
    verification: Optional[VerificationResult] = None
    review: Optional[ReviewExchange] = None
    error: str = ""
    approach_hash: str = ""  # For detecting repeated approaches

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class LoopDetectionState:
    """Tracks loop detection signals across attempts."""
    same_error_count: int = 0
    last_error_hash: str = ""
    same_approach_count: int = 0
    last_approach_hash: str = ""
    reviewer_reject_count: int = 0
    escalation_triggered: bool = False


@dataclass
class AttemptJournal:
    """
    Complete attempt journal for a single feature.

    Stored at: specs/memory/attempts/{feature_id}.json
    Archived to: specs/memory/attempts/archive/{feature_id}.json on completion
    """
    feature_id: str
    started_at: str = ""
    attempts: list = field(default_factory=list)
    review_exchanges: list = field(default_factory=list)
    loop_state: LoopDetectionState = field(default_factory=LoopDetectionState)
    final_status: Optional[str] = None  # "passing", "blocked", "exhausted"
    schema_version: str = "v1"

    def __post_init__(self):
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()
        if isinstance(self.loop_state, dict):
            self.loop_state = LoopDetectionState(**self.loop_state)
        # Convert dict attempts to Attempt objects
        if self.attempts and isinstance(self.attempts[0], dict):
            self.attempts = [self._dict_to_attempt(a) for a in self.attempts]
        if self.review_exchanges and isinstance(self.review_exchanges[0], dict):
            self.review_exchanges = [ReviewExchange(**r) for r in self.review_exchanges]

    def _dict_to_attempt(self, d: dict) -> Attempt:
        """Convert a dict to an Attempt object, handling nested objects."""
        if d.get("verification") and isinstance(d["verification"], dict):
            d["verification"] = VerificationResult(**d["verification"])
        if d.get("review") and isinstance(d["review"], dict):
            d["review"] = ReviewExchange(**d["review"])
        return Attempt(**d)

    @property
    def path(self) -> Path:
        """Path to this journal's storage file."""
        return ATTEMPTS_DIR / f"{self.feature_id}.json"

    @property
    def archive_path(self) -> Path:
        """Path to archived journal after feature completion."""
        return ARCHIVE_DIR / f"{self.feature_id}.json"

    def save(self) -> None:
        """Persist journal to disk."""
        ATTEMPTS_DIR.mkdir(parents=True, exist_ok=True)

        # Convert to serializable dict
        data = {
            "schema_version": self.schema_version,
            "feature_id": self.feature_id,
            "started_at": self.started_at,
            "final_status": self.final_status,
            "loop_state": asdict(self.loop_state),
            "attempts": [self._attempt_to_dict(a) for a in self.attempts],
            "review_exchanges": [asdict(r) for r in self.review_exchanges],
        }

        self.path.write_text(json.dumps(data, indent=2))

    def _attempt_to_dict(self, attempt: Attempt) -> dict:
        """Convert an Attempt to a serializable dict."""
        d = asdict(attempt)
        return d

    def archive(self) -> None:
        """Move journal to archive after feature completion."""
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        self.save()  # Ensure latest state is saved
        if self.path.exists():
            shutil.move(str(self.path), str(self.archive_path))


def compute_error_hash(error_output: str) -> str:
    """
    Compute a hash of error output for duplicate detection.

    Normalizes the error to ignore timestamps, paths, and line numbers
    that vary between runs of the same underlying issue.
    """
    if not error_output:
        return ""

    import re

    # Normalize the error
    normalized = error_output.lower()

    # Remove timestamps (various formats)
    normalized = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', '', normalized)
    normalized = re.sub(r'\d{2}:\d{2}:\d{2}', '', normalized)

    # Remove absolute paths but keep filenames
    normalized = re.sub(r'/[^\s:]+/([^/\s:]+)', r'\1', normalized)
    normalized = re.sub(r'[A-Za-z]:\\[^\s:]+\\([^\\:\s]+)', r'\1', normalized)

    # Remove line numbers (common formats)
    normalized = re.sub(r':\d+:\d+', ':LINE', normalized)
    normalized = re.sub(r'line \d+', 'line LINE', normalized)
    normalized = re.sub(r'\(\d+:\d+\)', '(LINE)', normalized)

    # Remove memory addresses
    normalized = re.sub(r'0x[0-9a-f]+', '0xADDR', normalized)

    # Remove UUIDs
    normalized = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', 'UUID', normalized)

    # Collapse whitespace
    normalized = re.sub(r'\s+', ' ', normalized).strip()

    # Take the first 2000 chars to focus on the main error
    normalized = normalized[:2000]

    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def compute_approach_signature(files_modified: list, summary: str) -> str:
    """
    Compute a signature for an approach to detect repetition.

    Combines:
    - List of files being modified
    - Key terms from the approach summary
    """
    import re

    # Sort files for consistency
    files_part = "|".join(sorted(files_modified)) if files_modified else ""

    # Extract key terms from summary (nouns, verbs)
    if summary:
        # Simple keyword extraction - keep alphanumeric words > 3 chars
        words = re.findall(r'\b[a-zA-Z]{4,}\b', summary.lower())
        # Remove common words
        stopwords = {'this', 'that', 'with', 'from', 'have', 'been', 'will', 'would',
                    'could', 'should', 'into', 'some', 'they', 'them', 'their', 'what',
                    'when', 'where', 'which', 'while', 'about', 'after', 'before'}
        keywords = [w for w in words if w not in stopwords]
        # Take top 10 most common
        from collections import Counter
        summary_part = "|".join(w for w, _ in Counter(keywords).most_common(10))
    else:
        summary_part = ""

    combined = f"{files_part}::{summary_part}"
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def load_journal(feature_id: str) -> AttemptJournal:
    """
    Load an existing journal or create a new one.

    Args:
        feature_id: The feature being worked on

    Returns:
        AttemptJournal instance
    """
    path = ATTEMPTS_DIR / f"{feature_id}.json"

    if path.exists():
        try:
            data = json.loads(path.read_text())
            return AttemptJournal(**data)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"Warning: Could not load journal for {feature_id}: {e}")
            # Return new journal but preserve the file (don't overwrite corrupted data)

    return AttemptJournal(feature_id=feature_id)


def record_attempt(
    feature_id: str,
    attempt_num: int,
    approach_summary: str = "",
    files_modified: list = None,
    claimed_complete: bool = False,
    verification_passed: bool = None,
    error_output: str = "",
    failed_tests: list = None
) -> Attempt:
    """
    Record a single iteration attempt.

    Args:
        feature_id: The feature being worked on
        attempt_num: Iteration number (1-based)
        approach_summary: Brief description of what was tried
        files_modified: List of files changed
        claimed_complete: Whether agent claimed completion
        verification_passed: Result of external verification
        error_output: Error message/output if verification failed
        failed_tests: List of failed test names

    Returns:
        The recorded Attempt
    """
    journal = load_journal(feature_id)

    files_modified = files_modified or []
    failed_tests = failed_tests or []

    # Compute hashes for pattern detection
    error_hash = compute_error_hash(error_output) if error_output else ""
    approach_hash = compute_approach_signature(files_modified, approach_summary)

    # Build verification result if we have verification info
    verification = None
    if verification_passed is not None:
        verification = VerificationResult(
            passed=verification_passed,
            error_signature=error_output[:500] if error_output else "",
            error_hash=error_hash,
            test_output_hash=hashlib.sha256(error_output.encode()).hexdigest()[:16] if error_output else "",
            failed_tests=failed_tests
        )

    attempt = Attempt(
        attempt_num=attempt_num,
        timestamp=datetime.now(timezone.utc).isoformat(),
        approach_summary=approach_summary,
        files_modified=files_modified,
        claimed_complete=claimed_complete,
        verification=verification,
        error=error_output[:1000] if error_output else "",
        approach_hash=approach_hash
    )

    journal.attempts.append(attempt)

    # Update loop detection state
    _update_loop_state(journal, attempt)

    journal.save()

    return attempt


def record_review(
    feature_id: str,
    attempt_num: int,
    reviewer: str,
    submission_summary: str,
    approved: bool,
    feedback_summary: str = "",
    concerns: list = None,
    key_decisions: list = None
) -> ReviewExchange:
    """
    Record a review exchange.

    Args:
        feature_id: The feature being worked on
        attempt_num: Which attempt this review is for
        reviewer: Reviewer identifier ("gemini", "manual", etc.)
        submission_summary: What was submitted for review
        approved: Whether review was approved
        feedback_summary: Summary of reviewer feedback
        concerns: List of specific concerns raised
        key_decisions: Key decisions in the submission

    Returns:
        The recorded ReviewExchange
    """
    journal = load_journal(feature_id)

    concerns = concerns or []
    key_decisions = key_decisions or []

    exchange = ReviewExchange(
        cycle=len([r for r in journal.review_exchanges if r.reviewer == reviewer]) + 1,
        reviewer=reviewer,
        submission_summary=submission_summary,
        key_decisions=key_decisions,
        approved=approved,
        concerns=concerns,
        feedback_summary=feedback_summary
    )

    journal.review_exchanges.append(exchange)

    # Also attach to the relevant attempt if possible
    for attempt in reversed(journal.attempts):
        if attempt.attempt_num == attempt_num:
            attempt.review = exchange
            break

    # Update loop state for reviewer ping-pong detection
    if not approved:
        journal.loop_state.reviewer_reject_count += 1
    else:
        journal.loop_state.reviewer_reject_count = 0  # Reset on approval

    journal.save()

    return exchange


def _update_loop_state(journal: AttemptJournal, attempt: Attempt) -> None:
    """Update loop detection state based on new attempt."""
    state = journal.loop_state

    # Check for same error
    if attempt.verification and not attempt.verification.passed:
        error_hash = attempt.verification.error_hash
        if error_hash and error_hash == state.last_error_hash:
            state.same_error_count += 1
        else:
            state.same_error_count = 1
            state.last_error_hash = error_hash

    # Check for same approach
    if attempt.approach_hash:
        if attempt.approach_hash == state.last_approach_hash:
            state.same_approach_count += 1
        else:
            state.same_approach_count = 1
            state.last_approach_hash = attempt.approach_hash


def get_attempt_history(feature_id: str) -> list[Attempt]:
    """
    Get all attempts for a feature.

    Args:
        feature_id: The feature ID

    Returns:
        List of Attempt objects
    """
    journal = load_journal(feature_id)
    return journal.attempts


def get_review_history(feature_id: str) -> list[ReviewExchange]:
    """
    Get all review exchanges for a feature.

    Args:
        feature_id: The feature ID

    Returns:
        List of ReviewExchange objects
    """
    journal = load_journal(feature_id)
    return journal.review_exchanges


def get_loop_state(feature_id: str) -> LoopDetectionState:
    """
    Get current loop detection state for a feature.

    Args:
        feature_id: The feature ID

    Returns:
        LoopDetectionState object
    """
    journal = load_journal(feature_id)
    return journal.loop_state


def finalize_journal(feature_id: str, final_status: str) -> None:
    """
    Mark journal as complete and archive it.

    Args:
        feature_id: The feature ID
        final_status: Final status ("passing", "blocked", "exhausted")
    """
    journal = load_journal(feature_id)
    journal.final_status = final_status
    journal.archive()


def format_attempt_history(
    feature_id: str,
    max_attempts: int = 5,
    include_full_errors: bool = False
) -> str:
    """
    Format attempt history for injection into prompts.

    Args:
        feature_id: The feature ID
        max_attempts: Maximum number of recent attempts to include
        include_full_errors: Whether to include full error text

    Returns:
        Formatted string for prompt injection
    """
    attempts = get_attempt_history(feature_id)

    if not attempts:
        return ""

    # Get recent attempts
    recent = attempts[-max_attempts:]

    lines = ["## Previous Attempts This Feature", ""]

    for attempt in recent:
        lines.append(f"### Attempt {attempt.attempt_num}")

        if attempt.approach_summary:
            lines.append(f"**Approach:** {attempt.approach_summary[:200]}")

        if attempt.files_modified:
            lines.append(f"**Files:** {', '.join(attempt.files_modified[:5])}")

        if attempt.claimed_complete:
            lines.append("**Status:** Claimed complete")

        if attempt.verification:
            if attempt.verification.passed:
                lines.append("**Verification:** PASSED")
            else:
                lines.append("**Verification:** FAILED")
                if include_full_errors and attempt.verification.error_signature:
                    lines.append(f"```\n{attempt.verification.error_signature}\n```")
                elif attempt.verification.failed_tests:
                    lines.append(f"**Failed tests:** {', '.join(attempt.verification.failed_tests[:3])}")

        if attempt.review and not attempt.review.approved:
            lines.append(f"**Review ({attempt.review.reviewer}):** Rejected")
            if attempt.review.feedback_summary:
                lines.append(f"**Feedback:** {attempt.review.feedback_summary[:150]}")

        lines.append("")

    return "\n".join(lines)


def format_review_history(feature_id: str) -> str:
    """
    Format review exchange history for prompt injection.

    Useful for giving reviewers context of previous exchanges.

    Args:
        feature_id: The feature ID

    Returns:
        Formatted string for prompt injection
    """
    exchanges = get_review_history(feature_id)

    if not exchanges:
        return ""

    lines = ["## Previous Review Exchanges", ""]

    # Group by reviewer
    by_reviewer = {}
    for ex in exchanges:
        if ex.reviewer not in by_reviewer:
            by_reviewer[ex.reviewer] = []
        by_reviewer[ex.reviewer].append(ex)

    for reviewer, reviewer_exchanges in by_reviewer.items():
        lines.append(f"### Reviews by {reviewer.title()}")

        for ex in reviewer_exchanges:
            status = "APPROVED" if ex.approved else "REJECTED"
            lines.append(f"**Cycle {ex.cycle}:** {status}")

            if ex.submission_summary:
                lines.append(f"- Submitted: {ex.submission_summary[:100]}")

            if not ex.approved and ex.feedback_summary:
                lines.append(f"- Feedback: {ex.feedback_summary[:150]}")

            if ex.concerns:
                lines.append("- Concerns:")
                for concern in ex.concerns[:3]:
                    if isinstance(concern, dict):
                        lines.append(f"  - {concern.get('title', str(concern))}")
                    else:
                        lines.append(f"  - {concern}")

        lines.append("")

    return "\n".join(lines)


def clear_journal(feature_id: str) -> None:
    """
    Delete a journal (useful for testing or resetting).

    Args:
        feature_id: The feature ID
    """
    path = ATTEMPTS_DIR / f"{feature_id}.json"
    if path.exists():
        path.unlink()


def load_archived_journal(feature_id: str) -> Optional[AttemptJournal]:
    """
    Load an archived journal (from completed features).

    Args:
        feature_id: The feature ID

    Returns:
        AttemptJournal if found, None otherwise
    """
    path = ARCHIVE_DIR / f"{feature_id}.json"

    if path.exists():
        try:
            data = json.loads(path.read_text())
            return AttemptJournal(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    return None


def list_archived_journals() -> list[str]:
    """
    List all archived journal feature IDs.

    Returns:
        List of feature IDs with archived journals
    """
    if not ARCHIVE_DIR.exists():
        return []

    return [p.stem for p in ARCHIVE_DIR.glob("*.json")]


# Self-test
if __name__ == "__main__":
    print("Testing attempt journal module...")

    test_feature = "_test_feature"

    # Clean up any existing test data
    clear_journal(test_feature)

    # Test recording attempts
    attempt1 = record_attempt(
        test_feature,
        attempt_num=1,
        approach_summary="Added login form with email validation",
        files_modified=["src/Login.tsx", "src/api/auth.ts"],
        claimed_complete=True,
        verification_passed=False,
        error_output="TypeError: Cannot read property 'email' of undefined at Login.tsx:42",
        failed_tests=["login.spec.ts"]
    )

    print(f"Recorded attempt 1, error hash: {attempt1.verification.error_hash}")

    # Record same error again
    attempt2 = record_attempt(
        test_feature,
        attempt_num=2,
        approach_summary="Added null check for user object",
        files_modified=["src/Login.tsx"],
        claimed_complete=True,
        verification_passed=False,
        error_output="TypeError: Cannot read property 'email' of undefined at Login.tsx:45",
        failed_tests=["login.spec.ts"]
    )

    print(f"Recorded attempt 2, error hash: {attempt2.verification.error_hash}")

    # Check loop state
    state = get_loop_state(test_feature)
    print(f"Same error count: {state.same_error_count}")

    # Record review
    record_review(
        test_feature,
        attempt_num=2,
        reviewer="gemini",
        submission_summary="Fixed null check in Login component",
        approved=False,
        feedback_summary="Missing null check on user object",
        concerns=[{"title": "Security: localStorage vulnerable to XSS"}]
    )

    # Test formatting
    print("\n--- Formatted History ---")
    print(format_attempt_history(test_feature))

    print("\n--- Review History ---")
    print(format_review_history(test_feature))

    # Test archiving
    finalize_journal(test_feature, "passing")

    # Verify archive
    archived = load_archived_journal(test_feature)
    assert archived is not None, "Archive should exist"
    assert archived.final_status == "passing"

    print("\n--- Archive Test ---")
    print(f"Archived journals: {list_archived_journals()}")

    # Clean up
    archive_path = ARCHIVE_DIR / f"{test_feature}.json"
    if archive_path.exists():
        archive_path.unlink()

    print("\nAll tests passed!")
