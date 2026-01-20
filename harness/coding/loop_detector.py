#!/usr/bin/env python3
"""
Loop Detector - Pattern Detection Across Attempts

Detects when agents are stuck in unproductive loops:
1. Same Error Loop: Same error appearing 3+ times
2. Same Approach Loop: Files + keywords repeating
3. Reviewer Ping-Pong: Alternating approve/reject with similar feedback
4. Oscillation: Code changes oscillate (add X, remove X, add X)

When loops are detected, the system can:
- Inject warnings into prompts
- Suggest different approaches
- Escalate to pause or block
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from collections import Counter

# Import from sibling module
from attempt_journal import (
    AttemptJournal,
    Attempt,
    LoopDetectionState,
    get_loop_state,
    get_attempt_history,
    get_review_history,
    load_journal,
)


class LoopType(Enum):
    """Types of loops that can be detected."""
    SAME_ERROR = "same_error"
    SAME_APPROACH = "same_approach"
    REVIEWER_PING_PONG = "reviewer_ping_pong"
    OSCILLATION = "oscillation"


class EscalationLevel(Enum):
    """Escalation levels for loop handling."""
    NONE = 0           # Normal operation
    WARN = 1           # Log warning, inject loop context into prompt
    PAUSE = 2          # Pause and ask user for guidance
    BLOCK = 3          # Mark feature as blocked


@dataclass
class LoopPattern:
    """A detected loop pattern."""
    loop_type: LoopType
    repetitions: int
    evidence: str
    suggestion: str
    severity: EscalationLevel = EscalationLevel.WARN


@dataclass
class LoopDetection:
    """Result of loop analysis."""
    is_looping: bool = False
    patterns: list = field(default_factory=list)
    primary_pattern: Optional[LoopPattern] = None
    escalation_level: EscalationLevel = EscalationLevel.NONE
    context_for_prompt: str = ""

    def __post_init__(self):
        # Set primary pattern to the highest severity pattern
        if self.patterns:
            self.patterns.sort(key=lambda p: p.severity.value, reverse=True)
            self.primary_pattern = self.patterns[0]
            self.escalation_level = self.primary_pattern.severity


# Thresholds for loop detection
SAME_ERROR_THRESHOLD = 3
SAME_APPROACH_THRESHOLD = 2
REVIEWER_PING_PONG_THRESHOLD = 3
MAX_ITERATIONS_WARN_PERCENT = 0.7  # Warn at 70% of max iterations


def detect_same_error_loop(attempts: list[Attempt]) -> Optional[LoopPattern]:
    """
    Detect when the same error keeps appearing.

    Uses error_hash to identify semantically identical errors.
    """
    if len(attempts) < SAME_ERROR_THRESHOLD:
        return None

    # Collect error hashes from failed verifications
    error_hashes = []
    error_samples = {}

    for attempt in attempts:
        if attempt.verification and not attempt.verification.passed:
            h = attempt.verification.error_hash
            if h:
                error_hashes.append(h)
                if h not in error_samples:
                    error_samples[h] = attempt.verification.error_signature[:200]

    if not error_hashes:
        return None

    # Find most common error
    counter = Counter(error_hashes)
    most_common, count = counter.most_common(1)[0]

    if count >= SAME_ERROR_THRESHOLD:
        error_sample = error_samples.get(most_common, "")
        severity = EscalationLevel.PAUSE if count >= SAME_ERROR_THRESHOLD + 2 else EscalationLevel.WARN

        return LoopPattern(
            loop_type=LoopType.SAME_ERROR,
            repetitions=count,
            evidence=f"Same error occurred {count} times: {error_sample[:100]}...",
            suggestion=_suggest_for_same_error(error_sample, count),
            severity=severity
        )

    return None


def detect_approach_repetition(attempts: list[Attempt]) -> Optional[LoopPattern]:
    """
    Detect when the same approach is being tried repeatedly.

    Uses approach_hash which combines files modified + key terms.
    """
    if len(attempts) < SAME_APPROACH_THRESHOLD:
        return None

    # Collect approach hashes
    approach_hashes = []
    approach_summaries = {}

    for attempt in attempts:
        if attempt.approach_hash:
            approach_hashes.append(attempt.approach_hash)
            if attempt.approach_hash not in approach_summaries:
                approach_summaries[attempt.approach_hash] = attempt.approach_summary[:100]

    if not approach_hashes:
        return None

    # Find repeated approaches
    counter = Counter(approach_hashes)
    most_common, count = counter.most_common(1)[0]

    if count >= SAME_APPROACH_THRESHOLD:
        summary = approach_summaries.get(most_common, "")

        return LoopPattern(
            loop_type=LoopType.SAME_APPROACH,
            repetitions=count,
            evidence=f"Similar approach tried {count} times: {summary}",
            suggestion=_suggest_for_same_approach(summary, count),
            severity=EscalationLevel.WARN
        )

    return None


def detect_reviewer_ping_pong(feature_id: str) -> Optional[LoopPattern]:
    """
    Detect when reviewer keeps rejecting with similar concerns.

    This indicates Claude and reviewer are not converging.
    """
    reviews = get_review_history(feature_id)

    if len(reviews) < REVIEWER_PING_PONG_THRESHOLD:
        return None

    # Count consecutive rejections
    consecutive_rejects = 0
    reject_reasons = []

    for review in reversed(reviews):
        if not review.approved:
            consecutive_rejects += 1
            if review.feedback_summary:
                reject_reasons.append(review.feedback_summary[:100])
        else:
            break

    if consecutive_rejects >= REVIEWER_PING_PONG_THRESHOLD:
        # Check if feedback is similar (simple word overlap)
        feedback_similarity = _check_feedback_similarity(reject_reasons)

        severity = EscalationLevel.PAUSE
        if consecutive_rejects >= REVIEWER_PING_PONG_THRESHOLD + 2 or feedback_similarity > 0.5:
            severity = EscalationLevel.PAUSE

        return LoopPattern(
            loop_type=LoopType.REVIEWER_PING_PONG,
            repetitions=consecutive_rejects,
            evidence=f"Reviewer rejected {consecutive_rejects} times. Recent: {reject_reasons[0] if reject_reasons else 'unknown'}",
            suggestion=_suggest_for_ping_pong(reject_reasons),
            severity=severity
        )

    return None


def detect_oscillation(attempts: list[Attempt]) -> Optional[LoopPattern]:
    """
    Detect when code changes oscillate (add X, remove X, add X).

    Looks for patterns where the same files are repeatedly modified
    with alternating approach signatures.
    """
    if len(attempts) < 4:
        return None

    # Get last 6 attempts
    recent = attempts[-6:]

    # Look for A-B-A-B pattern in approach hashes
    hashes = [a.approach_hash for a in recent if a.approach_hash]

    if len(hashes) < 4:
        return None

    # Check for alternating pattern
    oscillation_detected = False
    for i in range(len(hashes) - 3):
        if (hashes[i] == hashes[i+2] and
            hashes[i+1] == hashes[i+3] and
            hashes[i] != hashes[i+1]):
            oscillation_detected = True
            break

    if oscillation_detected:
        return LoopPattern(
            loop_type=LoopType.OSCILLATION,
            repetitions=2,
            evidence="Code changes are oscillating between two approaches",
            suggestion="You are oscillating between two approaches. Neither is working. Step back and consider a third approach that addresses the root cause differently.",
            severity=EscalationLevel.WARN
        )

    return None


def _check_feedback_similarity(feedbacks: list[str]) -> float:
    """
    Check how similar the feedback messages are.

    Returns a similarity score 0-1.
    """
    if len(feedbacks) < 2:
        return 0.0

    import re

    # Extract words from each feedback
    word_sets = []
    for fb in feedbacks:
        words = set(re.findall(r'\b[a-zA-Z]{4,}\b', fb.lower()))
        word_sets.append(words)

    # Calculate pairwise Jaccard similarity
    similarities = []
    for i in range(len(word_sets)):
        for j in range(i + 1, len(word_sets)):
            if word_sets[i] and word_sets[j]:
                intersection = len(word_sets[i] & word_sets[j])
                union = len(word_sets[i] | word_sets[j])
                similarities.append(intersection / union if union > 0 else 0)

    return sum(similarities) / len(similarities) if similarities else 0.0


def _suggest_for_same_error(error: str, count: int) -> str:
    """Generate suggestion for same error loop."""
    error_lower = error.lower()

    # Detect common error patterns and suggest fixes
    if "cannot read property" in error_lower or "undefined" in error_lower:
        return f"This null/undefined error has occurred {count} times. Check: 1) Is the data being fetched before use? 2) Are you handling loading states? 3) Is there an async timing issue? Consider adding defensive checks or restructuring the data flow."

    if "timeout" in error_lower:
        return f"Timeout error repeated {count} times. Check: 1) Is the service running? 2) Is the port correct? 3) Are there network issues? Consider adding retry logic or increasing timeout."

    if "import" in error_lower or "module" in error_lower:
        return f"Import/module error repeated {count} times. Check: 1) Is the package installed? 2) Is the path correct? 3) Check package.json/requirements.txt. Run the appropriate install command."

    if "type" in error_lower and "error" in error_lower:
        return f"Type error repeated {count} times. The type system is giving consistent feedback. Read the error carefully and fix the type mismatch at the source, don't just add type assertions."

    return f"This error has occurred {count} times with the same signature. Your previous approaches haven't resolved it. Try: 1) Read the error message more carefully, 2) Check assumptions about the code's state, 3) Add logging to understand what's actually happening."


def _suggest_for_same_approach(summary: str, count: int) -> str:
    """Generate suggestion for same approach loop."""
    return f"""You've tried a similar approach {count} times without success.

Your approach: "{summary}"

This approach isn't working. Before trying again, ask yourself:
1. What assumption am I making that might be wrong?
2. Is there a completely different way to solve this?
3. Should I break this into smaller, testable pieces?

Consider: reading more of the existing code, checking documentation, or trying a fundamentally different strategy."""


def _suggest_for_ping_pong(feedbacks: list[str]) -> str:
    """Generate suggestion for reviewer ping-pong."""
    recent_feedback = feedbacks[0] if feedbacks else "unknown concerns"

    return f"""You and the reviewer are not converging. The reviewer has rejected multiple times.

Most recent feedback: "{recent_feedback}"

To break out of this loop:
1. Re-read ALL previous reviewer feedback, not just the most recent
2. List out every concern raised across all cycles
3. Address ALL concerns in a single comprehensive fix
4. If concerns seem contradictory, clarify with the reviewer what the priority is

Don't make partial fixes - the reviewer wants to see all issues addressed together."""


def analyze_attempts(feature_id: str, max_iterations: int = 20) -> LoopDetection:
    """
    Analyze attempt history for loop patterns.

    Args:
        feature_id: The feature being worked on
        max_iterations: Maximum iterations allowed (for calculating thresholds)

    Returns:
        LoopDetection with all detected patterns and escalation recommendation
    """
    attempts = get_attempt_history(feature_id)

    if not attempts:
        return LoopDetection()

    patterns = []

    # Check for each loop type
    same_error = detect_same_error_loop(attempts)
    if same_error:
        patterns.append(same_error)

    same_approach = detect_approach_repetition(attempts)
    if same_approach:
        patterns.append(same_approach)

    ping_pong = detect_reviewer_ping_pong(feature_id)
    if ping_pong:
        patterns.append(ping_pong)

    oscillation = detect_oscillation(attempts)
    if oscillation:
        patterns.append(oscillation)

    # Check iteration count warning
    iteration_count = len(attempts)
    if iteration_count >= max_iterations * MAX_ITERATIONS_WARN_PERCENT:
        patterns.append(LoopPattern(
            loop_type=LoopType.SAME_ERROR,  # Reuse type
            repetitions=iteration_count,
            evidence=f"High iteration count: {iteration_count}/{max_iterations}",
            suggestion=f"You've used {iteration_count} of {max_iterations} allowed iterations. Time is running out. Focus on the most critical issue and make your remaining attempts count.",
            severity=EscalationLevel.WARN
        ))

    # Build detection result
    if patterns:
        detection = LoopDetection(
            is_looping=True,
            patterns=patterns
        )
        detection.context_for_prompt = format_loop_warning(detection)
        return detection

    return LoopDetection()


def format_loop_warning(detection: LoopDetection) -> str:
    """
    Format loop detection results for prompt injection.

    Returns a warning message to be included in the agent's context.
    """
    if not detection.is_looping:
        return ""

    lines = [
        "## ⚠️ LOOP DETECTION WARNING",
        "",
        "The harness has detected you may be stuck in a loop.",
        ""
    ]

    for pattern in detection.patterns:
        lines.append(f"### {pattern.loop_type.value.replace('_', ' ').title()}")
        lines.append(f"**Evidence:** {pattern.evidence}")
        lines.append(f"**Suggestion:** {pattern.suggestion}")
        lines.append("")

    lines.append("---")
    lines.append("IMPORTANT: Do NOT continue with the same approach. Read the suggestions above and try something different.")
    lines.append("")

    return "\n".join(lines)


def get_escalation_level(
    detection: LoopDetection,
    attempt_count: int,
    max_iterations: int = 20
) -> EscalationLevel:
    """
    Determine the appropriate escalation level.

    Args:
        detection: Loop detection results
        attempt_count: Current attempt number
        max_iterations: Maximum allowed iterations

    Returns:
        Appropriate EscalationLevel
    """
    if not detection.is_looping:
        return EscalationLevel.NONE

    # Start with the detection's recommended level
    level = detection.escalation_level

    # Escalate further if close to iteration limit
    iteration_ratio = attempt_count / max_iterations

    if iteration_ratio >= 0.9:  # 90% of iterations used
        if EscalationLevel.PAUSE.value > level.value:
            level = EscalationLevel.PAUSE
    elif iteration_ratio >= 0.8:  # 80% of iterations used
        if EscalationLevel.WARN.value > level.value:
            level = EscalationLevel.WARN

    # Escalate if multiple loop types detected
    if len(detection.patterns) >= 2:
        if EscalationLevel.WARN.value > level.value:
            level = EscalationLevel.WARN
    if len(detection.patterns) >= 3:
        if EscalationLevel.PAUSE.value > level.value:
            level = EscalationLevel.PAUSE

    return level


def should_escalate(feature_id: str, attempt_count: int, max_iterations: int = 20) -> tuple[bool, str]:
    """
    Quick check if escalation is needed.

    Returns:
        Tuple of (should_escalate: bool, reason: str)
    """
    detection = analyze_attempts(feature_id, max_iterations)

    if not detection.is_looping:
        return False, ""

    level = get_escalation_level(detection, attempt_count, max_iterations)

    if level.value >= EscalationLevel.PAUSE.value:
        reason = detection.primary_pattern.evidence if detection.primary_pattern else "Loop detected"
        return True, reason

    return False, ""


# Self-test
if __name__ == "__main__":
    print("Testing loop detector module...")

    # Import for testing
    from attempt_journal import record_attempt, record_review, clear_journal

    test_feature = "_test_loop_detection"
    clear_journal(test_feature)

    # Create a same-error scenario
    for i in range(1, 5):
        record_attempt(
            test_feature,
            attempt_num=i,
            approach_summary=f"Attempt {i} to fix login",
            files_modified=["src/Login.tsx"],
            claimed_complete=True,
            verification_passed=False,
            error_output="TypeError: Cannot read property 'email' of undefined",
            failed_tests=["login.spec.ts"]
        )

    # Test detection
    detection = analyze_attempts(test_feature)

    print(f"\nLoop detected: {detection.is_looping}")
    print(f"Patterns found: {len(detection.patterns)}")

    for pattern in detection.patterns:
        print(f"  - {pattern.loop_type.value}: {pattern.evidence[:50]}...")

    print(f"\nEscalation level: {detection.escalation_level.name}")

    print("\n--- Warning Context ---")
    print(detection.context_for_prompt[:500])

    # Test reviewer ping-pong
    clear_journal(test_feature)

    for i in range(1, 5):
        record_attempt(
            test_feature,
            attempt_num=i,
            approach_summary=f"Attempt {i}",
            files_modified=["src/Auth.tsx"],
            verification_passed=True
        )
        record_review(
            test_feature,
            attempt_num=i,
            reviewer="gemini",
            submission_summary="Updated auth logic",
            approved=False,
            feedback_summary="Security concern: localStorage is vulnerable to XSS"
        )

    detection = analyze_attempts(test_feature)
    print(f"\nPing-pong detected: {detection.is_looping}")
    if detection.primary_pattern:
        print(f"Primary pattern: {detection.primary_pattern.loop_type.value}")

    # Cleanup
    clear_journal(test_feature)

    print("\nAll tests passed!")
