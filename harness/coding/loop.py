#!/usr/bin/env python3
"""
Autonomous Agent Loop - CLI-Native Implementation

This script orchestrates the Plan -> Test -> Code cycle with:
1. Claude CLI for code execution (uses built-in tools)
2. External verification (harness runs tests, not agent)
3. Git checkpoints (commit on green, reset on red)
4. Regression fence (all tests must pass)
5. Repository map for brownfield safety
6. Reflection for knowledge transfer
7. Pattern enforcement for brownfield projects

The key insight: Claude CLI executes tools internally, but verification
is external - the harness runs tests to catch hallucinations.
"""

import argparse
import json
import signal
import subprocess
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

# Shared CLI module (add parent to path for import)
sys.path.insert(0, str(Path(__file__).parent.parent))
from cli import call_implementer as _call_claude_cli

# Local modules
from verify import (
    verify_feature,
    regression_check,
    claims_completion,
    format_verification_feedback,
    format_regression_feedback
)
from git_utils import (
    create_checkpoint,
    commit_feature,
    rollback,
    ensure_repo,
    RollbackError
)
from repo_map import build_feature_context
from reflection import run_reflection, save_learnings
from docs import maybe_generate_doc
from review import enforce_patterns, PatternViolation, get_modified_files, validate_file_scope, FileScopeViolation
from checkpoint import (
    run_checkpoint_review,
    handle_interrupt_options,
    save_checkpoint_history,
    generate_fix_features,
    extract_critical_issues,
    CheckpointRecord,
)

# Attempt Journal & Loop Detection
try:
    from attempt_journal import (
        record_attempt,
        record_review as record_review_exchange,
        finalize_journal,
        format_attempt_history,
        format_review_history,
        get_loop_state,
        load_journal,
    )
    from loop_detector import (
        analyze_attempts,
        get_escalation_level,
        EscalationLevel,
        format_loop_warning,
    )
    ATTEMPT_JOURNAL_AVAILABLE = True
except ImportError:
    ATTEMPT_JOURNAL_AVAILABLE = False

# Review Board (Bicameral Mind)
try:
    # Import from parent directory
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from review_board import should_review, request_review, check_critical_paths
    REVIEW_BOARD_AVAILABLE = True
except ImportError:
    REVIEW_BOARD_AVAILABLE = False

# Working Memory
try:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from memory import update_understanding, record_answer, get_memory_context
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False


# Configuration - values can be overridden via environment variables
FEATURES_PATH = Path("specs/features.json")
CLAUDE_MD_PATH = Path(".claude/CLAUDE.md")
CONFIG_PATH = Path(".claude/config.json")

# Configuration with environment variable overrides
import os

def _get_config_int(env_var: str, default: int) -> int:
    """Get config value from env var or return default."""
    val = os.environ.get(env_var)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return default

MAX_RETRIES_PER_FEATURE = _get_config_int("HARNESS_MAX_RETRIES", 5)
MAX_ITERATIONS_PER_FEATURE = _get_config_int("HARNESS_MAX_ITERATIONS", 20)
DEFAULT_CHECKPOINT_CADENCE = _get_config_int("HARNESS_CHECKPOINT_CADENCE", 5)


class LoopMode(Enum):
    """Operating mode for the main loop."""
    INTERACTIVE = "interactive"
    AUTONOMOUS = "autonomous"

# XML-style delimiters for prompt injection protection
XML_DELIMITERS = {
    "context_start": "<|TASK_CONTEXT|>",
    "context_end": "</|TASK_CONTEXT|>",
    "history_start": "<|ITERATION_HISTORY|>",
    "history_end": "</|ITERATION_HISTORY|>",
    "feedback_start": "<|HARNESS_FEEDBACK|>",
    "feedback_end": "</|HARNESS_FEEDBACK|>",
    "instructions_start": "<|INSTRUCTIONS|>",
    "instructions_end": "</|INSTRUCTIONS|>",
}


def sanitize_input(text: str) -> str:
    """
    Sanitize text to prevent delimiter injection.

    Escapes any strings that look like our delimiters.
    """
    for delimiter in XML_DELIMITERS.values():
        escaped = delimiter.replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(delimiter, escaped)
    return text


def extract_approach_summary(response_text: str, max_length: int = 200) -> str:
    """
    Extract a brief summary of the approach from agent response.

    Looks for key indicators of what the agent is trying to do.
    """
    import re

    # Look for common patterns in agent responses
    patterns = [
        r"(?:I'll|I will|Let me|Going to)\s+([^.!?\n]+)",
        r"(?:Adding|Creating|Implementing|Fixing|Updating)\s+([^.!?\n]+)",
        r"(?:The (?:solution|fix|approach|change) is)\s+([^.!?\n]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, response_text[:1500], re.IGNORECASE)
        if match:
            summary = match.group(0)[:max_length]
            return summary.strip()

    # Fallback: take first substantive line
    lines = response_text.split('\n')
    for line in lines[:10]:
        line = line.strip()
        if len(line) > 20 and not line.startswith('#'):
            return line[:max_length]

    return response_text[:max_length] if response_text else ""


def check_loop_and_get_context(feature_id: str, iteration: int, max_iterations: int) -> tuple[str, bool]:
    """
    Check for loop patterns and return context to inject.

    Returns:
        Tuple of (context_string, should_pause)
    """
    if not ATTEMPT_JOURNAL_AVAILABLE:
        return "", False

    detection = analyze_attempts(feature_id, max_iterations)

    if not detection.is_looping:
        return "", False

    escalation = get_escalation_level(detection, iteration, max_iterations)

    should_pause = escalation.value >= EscalationLevel.PAUSE.value

    return detection.context_for_prompt, should_pause


@dataclass
class IterationRecord:
    """Record of a single iteration for history tracking."""
    timestamp: str
    iteration: int
    response_summary: str = ""
    error: str = ""
    verification_result: str = ""
    files_changed: list = field(default_factory=list)


@dataclass
class FeatureSession:
    """Tracks state for a single feature's implementation session."""
    feature: dict
    conversation_history: list = field(default_factory=list)
    iterations: list = field(default_factory=list)
    checkpoint: str = ""
    last_failure_was_infrastructure: bool = False  # Track if failure was due to infra issues


@dataclass
class CheckpointState:
    """Track progress for cadence-based checkpointing."""
    features_at_last_checkpoint: int = 0
    checkpoint_count: int = 0
    interrupt_requested: bool = False  # Set by signal handler
    mode: LoopMode = LoopMode.INTERACTIVE


def create_signal_handler(state: CheckpointState):
    """
    Create signal handler that requests graceful interrupt.

    First Ctrl+C: Set interrupt flag, finish current feature then pause
    Second Ctrl+C: Force immediate stop (raise KeyboardInterrupt)
    """
    interrupt_count = [0]  # Use list for mutable closure

    def handler(signum, frame):
        interrupt_count[0] += 1

        if interrupt_count[0] == 1:
            print("\n[Interrupt] Will pause after current feature...")
            state.interrupt_requested = True
        else:
            print("\n[Interrupt] Forcing immediate stop...")
            raise KeyboardInterrupt()

    return handler


def load_cadence_config() -> dict:
    """Load cadence configuration from .claude/config.json."""
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text())
            return config.get("cadence", {})
        except json.JSONDecodeError:
            pass
    return {}


def get_cadence() -> int:
    """Get checkpoint cadence (features per checkpoint)."""
    config = load_cadence_config()
    return config.get("featuresPerCheckpoint", DEFAULT_CHECKPOINT_CADENCE)


def get_passing_count(features: list[dict]) -> int:
    """Count features with status 'passing'."""
    return len([f for f in features if f.get("status") == "passing"])


def get_progress_stats(features: list[dict]) -> dict:
    """Get progress statistics for the feature backlog."""
    total = len(features)
    passing = len([f for f in features if f.get("status") == "passing"])
    failing = len([f for f in features if f.get("status") == "failing"])
    blocked = len([f for f in features if f.get("blocked")])
    in_progress = len([f for f in features if f.get("status") == "in_progress"])
    todo = len([f for f in features if f.get("status") == "todo"])

    return {
        "total": total,
        "passing": passing,
        "failing": failing,
        "blocked": blocked,
        "in_progress": in_progress,
        "todo": todo,
    }


def should_checkpoint(features: list[dict], state: CheckpointState, cadence: int) -> bool:
    """
    Check if we've hit a cadence checkpoint.

    Returns True if:
    - Number of passing features since last checkpoint >= cadence
    - OR interrupt was requested (Ctrl+C)
    """
    passing = get_passing_count(features)
    since_last = passing - state.features_at_last_checkpoint
    return since_last >= cadence or state.interrupt_requested


def handle_interactive_checkpoint(result, state: CheckpointState, features: list[dict]) -> Optional[list[dict]]:
    """
    Handle checkpoint in interactive mode - pause and prompt.

    Returns:
        List of new fix features to insert, or None if user wants to stop
    """
    from datetime import datetime, timezone

    print("\n--- Review Results ---")
    print(result.feedback[:2000] if result.feedback else "No issues found.")

    stats = get_progress_stats(features)

    if result.approved:
        print("\n[APPROVED] Backlog looks healthy.")
        try:
            choice = input("\n[C]ontinue, [S]top? ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None

        if choice == "s":
            return None  # Signal to stop
    else:
        print("\n[ISSUES FOUND] Review flagged concerns.")
        print("\nOptions:")
        print("[1] Generate fix features and continue")
        print("[2] Continue without fixes (I'll handle manually)")
        print("[3] Stop and review")

        try:
            choice = input("\nChoice [1/2/3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if choice == "1":
            # Get next priority number
            max_priority = max((f.get("priority", 0) for f in features), default=0)
            fix_features = generate_fix_features(result.feedback, max_priority + 1)
            if fix_features:
                print(f"\nGenerated {len(fix_features)} fix features:")
                for f in fix_features:
                    print(f"  - {f['id']} (priority: {f['priority']})")

            # Save checkpoint record
            record = CheckpointRecord(
                number=state.checkpoint_count + 1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                features_passing=stats["passing"],
                features_total=stats["total"],
                review_approved=result.approved,
                issues_found=extract_critical_issues(result.feedback),
                fixes_generated=[f["id"] for f in fix_features],
                was_interrupt=state.interrupt_requested,
            )
            save_checkpoint_history(record)

            state.checkpoint_count += 1
            state.interrupt_requested = False
            state.features_at_last_checkpoint = stats["passing"]
            return fix_features

        elif choice == "3":
            return None  # Signal to stop

    # Save checkpoint record for continue case
    record = CheckpointRecord(
        number=state.checkpoint_count + 1,
        timestamp=datetime.now(timezone.utc).isoformat(),
        features_passing=stats["passing"],
        features_total=stats["total"],
        review_approved=result.approved,
        issues_found=[],
        fixes_generated=[],
        was_interrupt=state.interrupt_requested,
    )
    save_checkpoint_history(record)

    state.checkpoint_count += 1
    state.interrupt_requested = False
    state.features_at_last_checkpoint = stats["passing"]
    return []  # Continue without new features


def handle_autonomous_checkpoint(result, state: CheckpointState, features: list[dict]) -> list[dict]:
    """
    Handle checkpoint in autonomous mode - auto-fix critical, log others.

    Returns:
        List of fix features to insert (always continues, never returns None)
    """
    from datetime import datetime, timezone

    stats = get_progress_stats(features)

    if not result.approved:
        # Parse feedback for critical vs non-critical
        critical_issues = extract_critical_issues(result.feedback)

        if critical_issues:
            print(f"\n[CHECKPOINT] {len(critical_issues)} critical issues - generating fixes")
            max_priority = max((f.get("priority", 0) for f in features), default=0)
            fix_features = generate_fix_features(result.feedback, max_priority + 1)

            # Save checkpoint record
            record = CheckpointRecord(
                number=state.checkpoint_count + 1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                features_passing=stats["passing"],
                features_total=stats["total"],
                review_approved=result.approved,
                issues_found=critical_issues,
                fixes_generated=[f["id"] for f in fix_features],
                was_interrupt=state.interrupt_requested,
            )
            save_checkpoint_history(record)

            state.checkpoint_count += 1
            state.interrupt_requested = False
            state.features_at_last_checkpoint = stats["passing"]
            return fix_features
        else:
            print("\n[CHECKPOINT] Non-critical issues logged, continuing...")
    else:
        print("\n[CHECKPOINT] Review passed, continuing...")

    # Save checkpoint record
    record = CheckpointRecord(
        number=state.checkpoint_count + 1,
        timestamp=datetime.now(timezone.utc).isoformat(),
        features_passing=stats["passing"],
        features_total=stats["total"],
        review_approved=result.approved,
        issues_found=extract_critical_issues(result.feedback) if not result.approved else [],
        fixes_generated=[],
        was_interrupt=state.interrupt_requested,
    )
    save_checkpoint_history(record)

    state.checkpoint_count += 1
    state.interrupt_requested = False
    state.features_at_last_checkpoint = stats["passing"]
    return []


def run_checkpoint(features: list[dict], state: CheckpointState) -> Optional[list[dict]]:
    """
    Run holistic checkpoint review.

    Returns:
        List of new fix features to insert, or None if user wants to stop
    """
    stats = get_progress_stats(features)

    print(f"\n{'='*60}")
    print(f"CHECKPOINT #{state.checkpoint_count + 1}")
    if state.interrupt_requested:
        print("(Triggered by interrupt)")
    print(f"{'='*60}")

    print(f"Progress: {stats['passing']}/{stats['total']} features passing")
    print(f"Failing: {stats['failing']}, Blocked: {stats['blocked']}")

    # Run backlog review via review_board
    if REVIEW_BOARD_AVAILABLE:
        print("\nRunning backlog review...")
        result = run_checkpoint_review(features)

        if state.mode == LoopMode.INTERACTIVE:
            return handle_interactive_checkpoint(result, state, features)
        else:
            return handle_autonomous_checkpoint(result, state, features)

    # No review board - just log and continue
    print("\n[CHECKPOINT] Review board not available - continuing...")
    state.checkpoint_count += 1
    state.features_at_last_checkpoint = stats["passing"]
    state.interrupt_requested = False
    return []


def insert_fix_features(data: dict, fix_features: list[dict]) -> None:
    """Insert fix features into the backlog."""
    if not fix_features:
        return

    features = data.get("features", [])

    # Insert fix features
    for fix in fix_features:
        features.append(fix)

    # Re-sort by priority
    data["features"] = sorted(features, key=lambda f: f.get("priority", 999))

    # Save
    with open(FEATURES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def call_claude_cli(prompt_text: str, timeout: int = 1800) -> str:
    """
    Call claude CLI with streaming output and full tool access.

    Uses the shared CLI module which provides:
    - Streaming output for real-time feedback
    - Full tool access (Edit, Write, Bash, etc.)
    - Configurable timeout (default 30 min for implementation)
    """
    return _call_claude_cli(prompt_text, timeout=timeout)


def format_conversation(context: str, history: list, current_feedback: str = "") -> str:
    """
    Format the full conversation for CLI input.

    Since CLI takes a single string (not a messages array), we concatenate
    the context, history, and any current feedback.

    Uses XML-style delimiters to prevent prompt injection attacks.
    """
    parts = []

    # Initial context (includes constitution, repo map, feature spec)
    parts.append(XML_DELIMITERS["context_start"])
    parts.append(sanitize_input(context))
    parts.append(XML_DELIMITERS["context_end"])
    parts.append("")

    # Conversation history (previous iterations and feedback)
    if history:
        parts.append(XML_DELIMITERS["history_start"])
        for entry in history:
            if entry.get("type") == "response":
                content = sanitize_input(entry['content'][:2000])
                parts.append(f"\n<|AGENT_RESPONSE|>\n{content}\n</|AGENT_RESPONSE|>")
            elif entry.get("type") == "feedback":
                content = sanitize_input(entry['content'])
                parts.append(f"\n<|FEEDBACK|>\n{content}\n</|FEEDBACK|>")
        parts.append(XML_DELIMITERS["history_end"])
        parts.append("")

    # Current feedback (if any)
    if current_feedback:
        parts.append(XML_DELIMITERS["feedback_start"])
        parts.append(sanitize_input(current_feedback))
        parts.append(XML_DELIMITERS["feedback_end"])
        parts.append("")

    # Instructions
    parts.append(XML_DELIMITERS["instructions_start"])
    parts.append("""
AUTONOMOUS MODE - Do NOT ask for permission or confirmation. Execute immediately.

Work on the feature described above. Follow TDD:
1. Write a failing test first (in tests/e2e/)
2. Implement the minimum code to make it pass
3. When complete, say "IMPLEMENTATION COMPLETE" clearly

CRITICAL RULES:
- Do NOT ask "Would you like me to continue?" or similar questions
- Do NOT wait for user confirmation
- Do NOT output conversational pleasantries
- Just DO the work and report completion
- The harness will verify your work externally and provide feedback if tests fail

This is a non-interactive autonomous loop. Execute the task fully, then state IMPLEMENTATION COMPLETE.
""")
    parts.append(XML_DELIMITERS["instructions_end"])

    return "\n".join(parts)


def validate_feature(feature: dict) -> list[str]:
    """
    Validate a feature against the shared schema.
    Returns a list of warnings/errors.
    """
    warnings = []
    feature_id = feature.get("id", "unknown")

    # Required fields
    required = ["id", "description", "acceptance_criteria", "edge_cases", "priority"]
    for field_name in required:
        if field_name not in feature:
            warnings.append(f"[{feature_id}] Missing required field: {field_name}")

    # Edge cases validation (Rule of 3)
    edge_cases = feature.get("edge_cases", [])
    if len(edge_cases) < 3:
        warnings.append(f"[{feature_id}] Only {len(edge_cases)} edge cases (minimum 3 required)")

    for i, ec in enumerate(edge_cases):
        if not ec.get("id"):
            warnings.append(f"[{feature_id}] Edge case {i} missing 'id'")
        if not ec.get("expected_behavior"):
            warnings.append(f"[{feature_id}] Edge case {i} missing 'expected_behavior'")

    # Falsifiability check (basic heuristic)
    desc = feature.get("description", "")
    vague_patterns = ["can ", "should ", "will be able to", "allows ", "enables "]
    for pattern in vague_patterns:
        if pattern in desc.lower():
            warnings.append(f"[{feature_id}] Description may not be falsifiable (contains '{pattern.strip()}')")
            break

    return warnings


def load_features() -> dict:
    """Load the features backlog from specs/features.json."""
    if not FEATURES_PATH.exists():
        print(f"ERROR: {FEATURES_PATH} not found.")
        print("Run '/architect new \"your idea\"' first.")
        sys.exit(1)

    with open(FEATURES_PATH) as f:
        data = json.load(f)

    # Validate all features
    all_warnings = []
    for feature in data.get("features", []):
        warnings = validate_feature(feature)
        all_warnings.extend(warnings)

    if all_warnings:
        print("\n" + "=" * 60)
        print("FEATURE VALIDATION WARNINGS")
        print("=" * 60)
        for warning in all_warnings:
            print(f"  ⚠ {warning}")
        print("=" * 60 + "\n")

    return data


def save_features(data: dict) -> None:
    """Save the features backlog back to specs/features.json."""
    with open(FEATURES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def check_dependencies_met(feature: dict, features: list[dict]) -> bool:
    """Check if all dependencies for a feature are passing."""
    depends_on = feature.get("depends_on", [])
    if not depends_on:
        return True

    # Build a map of feature statuses
    status_map = {f.get("id"): f.get("status") for f in features}

    for dep_id in depends_on:
        if status_map.get(dep_id) != "passing":
            return False

    return True


def find_next_feature(features: list[dict]) -> Optional[dict]:
    """Find the next feature to work on (in_progress > failing > todo)."""
    # Sort by priority if available
    sorted_features = sorted(features, key=lambda f: f.get("priority", 99))

    # First, look for in_progress
    for feature in sorted_features:
        if feature.get("status") == "in_progress":
            return feature

    # Then, look for failing (needs retry)
    for feature in sorted_features:
        if feature.get("status") == "failing":
            if feature.get("retries", 0) < MAX_RETRIES_PER_FEATURE:
                if not feature.get("blocked"):
                    if check_dependencies_met(feature, features):
                        return feature

    # Finally, look for todo (respecting priority and dependencies)
    for feature in sorted_features:
        if feature.get("status") == "todo":
            if not feature.get("blocked"):
                if check_dependencies_met(feature, features):
                    return feature

    return None


def update_feature_status(feature_id: str, status: str, increment_retries: bool = False) -> None:
    """Update a feature's status in the JSON file."""
    data = load_features()

    for feature in data.get("features", []):
        if feature.get("id") == feature_id:
            feature["status"] = status
            feature["last_updated"] = datetime.now(timezone.utc).isoformat()
            if increment_retries:
                feature["retries"] = feature.get("retries", 0) + 1
            break

    save_features(data)


def run_feature_loop(session: FeatureSession) -> bool:
    """
    Run the implementation loop for a single feature.

    Returns True if feature passes, False otherwise.
    """
    feature = session.feature
    feature_id = feature.get("id", "unknown")

    print(f"\n{'='*60}")
    print(f"Working on: {feature_id}")
    print(f"Description: {feature.get('description', '')}")
    print(f"{'='*60}")

    # Update working memory with current feature
    if MEMORY_AVAILABLE:
        update_understanding("coding_loop", "current_feature", feature_id)
        update_understanding("coding_loop", "feature_description", feature.get("description", "")[:200])
        update_understanding("coding_loop", "feature_status", "in_progress")

    # Build initial context with repo map
    initial_context = build_feature_context(feature)

    # Include memory context if available
    if MEMORY_AVAILABLE:
        memory_ctx = get_memory_context("coding_loop")
        if memory_ctx:
            initial_context += f"\n\n{memory_ctx}"

    iteration = 0
    current_feedback = ""

    while iteration < MAX_ITERATIONS_PER_FEATURE:
        iteration += 1
        print(f"\n--- Iteration {iteration}/{MAX_ITERATIONS_PER_FEATURE} ---")

        # Check for loop patterns before this iteration
        loop_context = ""
        if ATTEMPT_JOURNAL_AVAILABLE and iteration > 3:
            loop_context, should_pause = check_loop_and_get_context(
                feature_id, iteration, MAX_ITERATIONS_PER_FEATURE
            )
            if loop_context:
                print("[LOOP DETECTION] Warning injected into context")
            if should_pause:
                print("\n[LOOP DETECTION] Escalation triggered - pausing for guidance")
                print("The agent appears to be stuck. Options:")
                print("  [c] Continue anyway")
                print("  [b] Block this feature")
                print("  [h] Add a hint to help the agent")
                try:
                    choice = input("Choice [c/b/h]: ").strip().lower()
                    if choice == "b":
                        return False
                    elif choice == "h":
                        hint = input("Enter hint for agent: ").strip()
                        if hint:
                            loop_context += f"\n\n## Human Hint\n{hint}"
                except (EOFError, KeyboardInterrupt):
                    pass

        try:
            # Format the full prompt (include loop context if detected)
            effective_feedback = current_feedback
            if loop_context:
                effective_feedback = loop_context + "\n\n" + current_feedback if current_feedback else loop_context

            prompt = format_conversation(
                initial_context,
                session.conversation_history,
                effective_feedback
            )

            # Call Claude CLI
            print("Calling Claude CLI (streaming, 30 min timeout)...\n")
            response_text = call_claude_cli(prompt, timeout=1800)

            print(f"\nResponse preview: {response_text[:500]}...")

            # Record iteration
            record = IterationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                iteration=iteration,
                response_summary=response_text[:1000]
            )

            # Add response to history
            session.conversation_history.append({
                "type": "response",
                "content": response_text
            })

            # Check if agent claims completion
            if claims_completion(response_text):
                print("\nAgent claims completion. Running verification...")

                # Extract approach summary for attempt journal
                approach_summary = extract_approach_summary(response_text)

                # EXTERNAL VERIFICATION - harness runs the test
                verification = verify_feature(feature, response_text)
                record.verification_result = verification.reason

                # Record this attempt in the journal
                if ATTEMPT_JOURNAL_AVAILABLE:
                    try:
                        modified_files = get_modified_files()
                    except Exception:
                        modified_files = []

                    record_attempt(
                        feature_id=feature_id,
                        attempt_num=iteration,
                        approach_summary=approach_summary,
                        files_modified=modified_files,
                        claimed_complete=True,
                        verification_passed=verification.passed,
                        error_output=verification.stderr if hasattr(verification, 'stderr') else verification.reason,
                        failed_tests=verification.failed_tests if hasattr(verification, 'failed_tests') else []
                    )
                    print(f"  [Attempt Journal] Recorded attempt #{iteration} (passed={verification.passed})")

                if verification.passed:
                    print("✓ Feature test passes!")

                    # Check file scope restrictions before pattern compliance
                    print("Checking file scope restrictions...")
                    try:
                        modified_files = get_modified_files()
                        validate_file_scope(modified_files, feature)
                        print("✓ File scope OK")
                    except FileScopeViolation as e:
                        print(f"✗ File scope violation: {e}")
                        current_feedback = f"""Your changes violate file scope restrictions:

{e}

The feature specifies which files you are allowed to create, modify, and which are forbidden.
You MUST revert changes to forbidden files and limit modifications to allowed files only.

Check the feature's 'file_scope' in specs/features.json for the exact restrictions."""
                        session.conversation_history.append({
                            "type": "feedback",
                            "content": current_feedback
                        })
                        record.error = f"File scope violation: {str(e)[:200]}"
                        session.iterations.append(record)
                        continue  # Let agent fix it

                    # Check pattern compliance before proceeding
                    print("Checking pattern compliance...")
                    try:
                        enforce_patterns(modified_files)
                        print("✓ Pattern compliance OK")
                    except PatternViolation as e:
                        print(f"✗ Pattern violation: {e}")
                        current_feedback = f"""Your changes violate established project patterns:

{e}

You MUST fix these violations before the feature can be completed.
Review specs/context/patterns.md for the required patterns.
Modify your code to comply with the existing codebase conventions."""
                        session.conversation_history.append({
                            "type": "feedback",
                            "content": current_feedback
                        })
                        record.error = f"Pattern violation: {str(e)[:200]}"
                        session.iterations.append(record)
                        continue  # Let agent fix it

                    # Run regression check before final commit
                    print("Running regression check...")
                    regression = regression_check()

                    if regression.passed:
                        # Review Board: Check if cross-model review is needed
                        if REVIEW_BOARD_AVAILABLE and should_review("implementer", modified_files):
                            print("\n" + "=" * 40)
                            print("REVIEW BOARD: Code Review Required")
                            print("=" * 40)

                            # Show which files triggered the review
                            critical_matches = check_critical_paths(modified_files)
                            if critical_matches:
                                print("\nCritical files modified:")
                                for file, pattern in critical_matches:
                                    print(f"  - {file} (matched: {pattern})")

                            # Get the diff for review
                            try:
                                diff_result = subprocess.run(
                                    ["git", "diff", "--staged"],
                                    capture_output=True,
                                    text=True
                                )
                                diff_content = diff_result.stdout or "(no staged changes)"
                            except Exception:
                                diff_content = "(could not get diff)"

                            # Build context for reviewer
                            review_context = {
                                "feature_id": feature_id,
                                "description": feature.get("description", ""),
                                "acceptance_criteria": feature.get("acceptance_criteria", ""),
                                "files_modified": ", ".join(modified_files[:10]),
                            }

                            result = request_review(
                                stage="implementer",
                                context=review_context,
                                output=diff_content
                            )

                            # Record review exchange in attempt journal
                            if ATTEMPT_JOURNAL_AVAILABLE:
                                record_review_exchange(
                                    feature_id=feature_id,
                                    attempt_num=iteration,
                                    reviewer=result.reviewer if hasattr(result, 'reviewer') else "unknown",
                                    submission_summary=approach_summary if 'approach_summary' in dir() else "",
                                    approved=result.approved,
                                    feedback_summary=result.feedback[:300] if result.feedback else "",
                                    concerns=result.concerns if hasattr(result, 'concerns') else []
                                )

                            if not result.approved:
                                print("\n[REVIEW BOARD] Review rejected. Addressing feedback...")
                                current_feedback = f"""REVIEWER VETO:

{result.feedback}

You MUST address these review comments before the feature can be committed.
Make the necessary changes and ensure they pass verification again."""
                                session.conversation_history.append({
                                    "type": "feedback",
                                    "content": current_feedback
                                })
                                record.error = f"Review rejected: {result.feedback[:200]}"
                                session.iterations.append(record)
                                continue  # Go back to let agent fix issues
                            else:
                                print("\n[REVIEW BOARD] Code review approved.")

                        print("✓ All tests pass! Committing...")
                        commit_feature(feature_id, feature.get("description", ""))
                        session.iterations.append(record)

                        # Update working memory with success
                        if MEMORY_AVAILABLE:
                            update_understanding("coding_loop", "feature_status", "passing")
                            update_understanding("coding_loop", f"feature_{feature_id}_iterations", str(iteration))

                        # Finalize attempt journal
                        if ATTEMPT_JOURNAL_AVAILABLE:
                            finalize_journal(feature_id, "passing")

                        return True
                    else:
                        # Regression detected - feed back to agent
                        if regression.is_infrastructure_failure:
                            print(f"✗ Regression detected (INFRASTRUCTURE ISSUE): {len(regression.failed_tests)} tests")
                            session.last_failure_was_infrastructure = True
                        else:
                            print(f"✗ Regression detected: {regression.failed_tests}")
                            session.last_failure_was_infrastructure = False
                        current_feedback = format_regression_feedback(regression, feature_id)
                        session.conversation_history.append({
                            "type": "feedback",
                            "content": current_feedback
                        })
                        record.error = f"Regression: {regression.failed_tests}"
                else:
                    # Verification failed - feed error back to agent
                    print(f"✗ Verification failed: {verification.reason}")
                    current_feedback = format_verification_feedback(verification)
                    session.conversation_history.append({
                        "type": "feedback",
                        "content": current_feedback
                    })
                    record.error = verification.stderr[:500] if verification.stderr else verification.reason
            else:
                # Agent still working - no specific feedback needed
                current_feedback = ""

            session.iterations.append(record)

        except subprocess.CalledProcessError as e:
            # Extract stderr for better debugging
            stderr_msg = ""
            if hasattr(e, 'stderr') and e.stderr:
                stderr_msg = e.stderr if isinstance(e.stderr, str) else e.stderr.decode('utf-8', errors='replace')
            print(f"Error in iteration: {e}")
            if stderr_msg:
                print(f"  stderr: {stderr_msg[:500]}")
            session.iterations.append(IterationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                iteration=iteration,
                error=f"{e}\nstderr: {stderr_msg[:200]}"
            ))
            # Add error as feedback for next iteration
            current_feedback = f"Error occurred: {e}\nstderr: {stderr_msg}\nPlease try again."
        except Exception as e:
            print(f"Error in iteration: {e}")
            session.iterations.append(IterationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                iteration=iteration,
                error=str(e)
            ))
            # Add error as feedback for next iteration
            current_feedback = f"Error occurred: {e}\nPlease try again."

    print(f"\nMax iterations ({MAX_ITERATIONS_PER_FEATURE}) reached for {feature_id}")

    # Finalize attempt journal as exhausted
    if ATTEMPT_JOURNAL_AVAILABLE:
        finalize_journal(feature_id, "exhausted")

    return False


def run_feature_with_checkpoints(feature: dict) -> tuple[bool, bool]:
    """
    Run a feature with git checkpoint/rollback support.

    Returns:
        Tuple of (success: bool, is_infrastructure_failure: bool)
        - success: True if feature passes all checks
        - is_infrastructure_failure: True if failure was due to infra issues (not code bugs)
    """
    feature_id = feature.get("id", "unknown")

    # Create checkpoint before starting
    checkpoint = create_checkpoint(feature_id)

    session = FeatureSession(feature=feature, checkpoint=checkpoint)

    try:
        success = run_feature_loop(session)

        # Build iteration history for reflection (used in both success and failure)
        iteration_history = [
            {
                "error": r.error,
                "response_summary": r.response_summary,
                "verification_result": r.verification_result
            }
            for r in session.iterations
        ]

        if success:
            # Run reflection to extract lessons
            print("\nRunning reflection...")
            learnings = run_reflection(feature, iteration_history)
            if learnings:
                save_learnings(learnings)
                print(f"Extracted {len(learnings)} lessons for future agents")

            # Generate documentation for the completed feature
            print("\nGenerating documentation...")
            doc_path = maybe_generate_doc(feature)
            if doc_path:
                print(f"Documentation saved to {doc_path}")

            return True, False
        else:
            # Run reflection on failure too - valuable lessons from what went wrong
            if len(session.iterations) >= 2:  # Only if we had multiple attempts
                print("\nRunning reflection on failure (lessons from what went wrong)...")
                learnings = run_reflection(feature, iteration_history, failed=True)
                if learnings:
                    save_learnings(learnings)
                    print(f"Extracted {len(learnings)} lessons from failed attempts")

            # Rollback on failure
            print("\nRolling back changes...")
            try:
                rollback(strict=True)
            except RollbackError as e:
                print(f"CRITICAL: Rollback failed! {e}")
                print("Manual intervention required. Working directory is dirty.")
                # Re-raise to halt the loop - dirty state is dangerous
                raise
            return False, session.last_failure_was_infrastructure

    except KeyboardInterrupt:
        print("\n\nInterrupted. Rolling back...")
        try:
            rollback(strict=False)  # Best effort on interrupt
        except RollbackError:
            print("Warning: Rollback failed on interrupt. Working directory may be dirty.")
        raise


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Autonomous Agent Harness - CLI-Native Loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  /loop                    # Auto-detect mode from TTY (in harness shell)
  /loop --interactive      # Always pause at checkpoints
  /loop --autonomous       # Never pause, auto-fix critical
  /loop -a                 # Short form for autonomous
  /loop --cadence 3        # Checkpoint every 3 features
  /loop -a --cadence 10    # Autonomous, every 10 features
        """
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Interactive mode: pause at checkpoints for user input"
    )
    mode_group.add_argument(
        "--autonomous", "-a",
        action="store_true",
        help="Autonomous mode: auto-continue, auto-fix critical issues"
    )

    parser.add_argument(
        "--cadence", "-c",
        type=int,
        default=None,
        help=f"Checkpoint every N features (default: {DEFAULT_CHECKPOINT_CADENCE})"
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()

    # Determine mode
    if args.interactive:
        mode = LoopMode.INTERACTIVE
    elif args.autonomous:
        mode = LoopMode.AUTONOMOUS
    else:
        # Auto-detect from TTY
        mode = LoopMode.INTERACTIVE if sys.stdin.isatty() else LoopMode.AUTONOMOUS

    # Determine cadence
    cadence = args.cadence if args.cadence is not None else get_cadence()

    print("=" * 60)
    print("AUTONOMOUS AGENT HARNESS - CLI-Native Loop")
    print(f"Mode: {mode.value.upper()}")
    print(f"Checkpoint cadence: every {cadence} features")
    print("=" * 60)

    # Show feature availability
    print("\nFeatures:")
    print(f"  Attempt Journal: {'ENABLED' if ATTEMPT_JOURNAL_AVAILABLE else 'DISABLED'}")
    print(f"  Loop Detection:  {'ENABLED' if ATTEMPT_JOURNAL_AVAILABLE else 'DISABLED'}")
    print(f"  Working Memory:  {'ENABLED' if MEMORY_AVAILABLE else 'DISABLED'}")
    print(f"  Review Board:    {'ENABLED' if REVIEW_BOARD_AVAILABLE else 'DISABLED'}")

    # Pre-flight dependency checks
    print("\nPre-flight checks...")
    preflight_failed = False

    # 1. Check git is available
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            check=True,
            timeout=10
        )
        print("  ✓ git")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        print("  ✗ git - not found or not working")
        print("    Install git: https://git-scm.com/downloads")
        preflight_failed = True

    # 2. Check Claude CLI is available
    try:
        subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            check=True,
            timeout=10
        )
        print("  ✓ claude CLI")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        print("  ✗ claude CLI - not found or not working")
        print("    Install it: npm install -g @anthropic-ai/claude-code")
        preflight_failed = True

    # 3. Check test runner is available (based on config)
    config_path = Path(".claude/config.json")
    test_cmd = ["npx", "playwright", "test"]  # default
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            test_cmd_str = config.get("settings", {}).get("testCommand", "npx playwright test")
            import shlex
            test_cmd = shlex.split(test_cmd_str)
        except (json.JSONDecodeError, KeyError):
            pass

    # Check if the test runner exists (just the command, not running tests)
    test_runner = test_cmd[0]
    try:
        # For npx commands, check that npm/npx exists
        if test_runner == "npx":
            subprocess.run(
                ["npx", "--version"],
                capture_output=True,
                check=True,
                timeout=10
            )
        elif test_runner == "pytest":
            subprocess.run(
                ["pytest", "--version"],
                capture_output=True,
                check=True,
                timeout=10
            )
        else:
            subprocess.run(
                [test_runner, "--version"],
                capture_output=True,
                timeout=10
            )
        print(f"  ✓ test runner ({test_runner})")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        print(f"  ✗ test runner ({test_runner}) - not found")
        if test_runner == "npx":
            print("    Install Node.js: https://nodejs.org/")
            print("    Then run: npm install (to install dependencies)")
        elif test_runner == "pytest":
            print("    Install pytest: pip install pytest")
        preflight_failed = True

    # 4. Check Python 3 is available (harness scripts need it)
    try:
        subprocess.run(
            ["python3", "--version"],
            capture_output=True,
            check=True,
            timeout=10
        )
        print("  ✓ python3")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        print("  ✗ python3 - not found")
        print("    Install Python 3.9+: https://www.python.org/downloads/")
        preflight_failed = True

    if preflight_failed:
        print("\nERROR: Pre-flight checks failed. Please install missing dependencies.")
        return 1

    print("  All pre-flight checks passed.\n")

    # Ensure git repo exists
    if not ensure_repo():
        print("ERROR: Could not initialize git repository")
        return 1

    print("Claude CLI ready")

    # Initialize checkpoint state
    checkpoint_state = CheckpointState(mode=mode)

    # Register signal handler for graceful interrupt
    original_handler = signal.signal(signal.SIGINT, create_signal_handler(checkpoint_state))

    try:
        # Main loop
        while True:
            # Reload features each iteration
            data = load_features()
            features = data.get("features", [])

            # === CHECKPOINT CHECK ===
            if should_checkpoint(features, checkpoint_state, cadence):
                # Handle interrupt-triggered checkpoint in interactive mode
                if checkpoint_state.interrupt_requested and mode == LoopMode.INTERACTIVE:
                    action = handle_interrupt_options()
                    if action is None:
                        print("\nStopping at user request.")
                        return 0
                    elif action == "continue":
                        checkpoint_state.interrupt_requested = False
                        continue
                    # action == "review" falls through to run checkpoint

                new_features = run_checkpoint(features, checkpoint_state)

                if new_features is None:
                    print("\nStopping at checkpoint.")
                    return 0
                elif new_features:
                    # Insert fix features into backlog
                    insert_fix_features(data, new_features)
                    continue  # Re-evaluate with new features

            # Check if all done
            all_passing = all(f.get("status") == "passing" for f in features)
            if all_passing:
                print("\n" + "=" * 60)
                print("SUCCESS: All features are passing!")
                print("=" * 60)
                return 0

            # Find next feature
            feature = find_next_feature(features)

            if not feature:
                # Check for blocked or exhausted features
                blocked = [f for f in features if f.get("blocked")]
                exhausted = [f for f in features if f.get("retries", 0) >= MAX_RETRIES_PER_FEATURE]

                if blocked:
                    print(f"\n{len(blocked)} features are blocked:")
                    for f in blocked:
                        print(f"  - {f['id']}: {f.get('block_reason', 'unknown')}")

                if exhausted:
                    print(f"\n{len(exhausted)} features exhausted retries:")
                    for f in exhausted:
                        print(f"  - {f['id']}")

                return 1 if (blocked or exhausted) else 0

            # Update status to in_progress
            if feature.get("status") == "todo":
                update_feature_status(feature["id"], "in_progress")

            # Run the feature
            success, is_infra_failure = run_feature_with_checkpoints(feature)

            if success:
                update_feature_status(feature["id"], "passing")
            else:
                # Don't increment retries for infrastructure failures
                # (server crashes, port conflicts, etc. aren't code bugs)
                if is_infra_failure:
                    print("  [Note: Not incrementing retries - infrastructure failure detected]")
                    update_feature_status(feature["id"], "failing", increment_retries=False)
                else:
                    update_feature_status(feature["id"], "failing", increment_retries=True)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        return 130

    finally:
        # Restore original signal handler
        signal.signal(signal.SIGINT, original_handler)


if __name__ == "__main__":
    sys.exit(main())
