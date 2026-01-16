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

import json
import select
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

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
    ensure_repo
)
from repo_map import build_feature_context
from reflection import run_reflection, save_learnings
from review import enforce_patterns, PatternViolation, get_modified_files

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


# Configuration
FEATURES_PATH = Path("specs/features.json")
CLAUDE_MD_PATH = Path(".claude/CLAUDE.md")
MAX_RETRIES_PER_FEATURE = 5
MAX_ITERATIONS_PER_FEATURE = 20

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


def call_claude_cli(prompt_text: str, timeout: int = 1800) -> str:
    """
    Call claude CLI with streaming output for real-time feedback.

    The CLI will execute with its built-in tools (file editing, etc.)
    Streams output in real-time so user can see progress.
    """
    try:
        process = subprocess.Popen(
            ["claude", "--print", "--output-format", "stream-json",
             "--verbose", "--include-partial-messages", "--dangerously-skip-permissions"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Send prompt via stdin
        process.stdin.write(prompt_text)
        process.stdin.close()

        full_response = []
        final_result = None
        last_activity = time.time()

        while True:
            # Check for timeout
            if time.time() - last_activity > timeout:
                process.kill()
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

            # Read available output
            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            if ready:
                line = process.stdout.readline()
                if not line:
                    break
                last_activity = time.time()

                # Parse streaming JSON and show real-time output
                try:
                    data = json.loads(line)

                    # Stream events are wrapped: {"type":"stream_event","event":{...}}
                    if data.get("type") == "stream_event":
                        event = data.get("event", {})
                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                full_response.append(text)
                                # Print actual text as it streams
                                print(text, end="", flush=True)
                        elif event.get("type") == "message_stop":
                            pass  # Continue to get final result

                    # Final result contains the complete response
                    elif data.get("type") == "result":
                        final_result = data.get("result", "")

                except json.JSONDecodeError:
                    pass

            # Check if process ended
            if process.poll() is not None:
                break

        print()  # Newline after streaming output

        # Get any remaining output
        remaining = process.stdout.read()
        if remaining:
            for line in remaining.strip().split("\n"):
                if line:
                    try:
                        data = json.loads(line)
                        if data.get("type") == "stream_event":
                            event = data.get("event", {})
                            if event.get("type") == "content_block_delta":
                                delta = event.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    full_response.append(delta.get("text", ""))
                        elif data.get("type") == "result":
                            final_result = data.get("result", "")
                    except json.JSONDecodeError:
                        pass

        process.wait()

        if process.returncode != 0:
            stderr = process.stderr.read()
            raise subprocess.CalledProcessError(process.returncode, "claude", stderr=stderr)

        # Prefer the final result if available, otherwise use streamed content
        if final_result is not None:
            return final_result
        return "".join(full_response)

    except FileNotFoundError:
        print("ERROR: 'claude' CLI not found.")
        print("Install it: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)


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
        print("Run 'python harness/architect.py new \"your idea\"' first.")
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

        try:
            # Format the full prompt
            prompt = format_conversation(
                initial_context,
                session.conversation_history,
                current_feedback
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

                # EXTERNAL VERIFICATION - harness runs the test
                verification = verify_feature(feature, response_text)
                record.verification_result = verification.reason

                if verification.passed:
                    print("✓ Feature test passes!")

                    # Check pattern compliance before proceeding
                    print("Checking pattern compliance...")
                    try:
                        modified_files = get_modified_files()
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

                        return True
                    else:
                        # Regression detected - feed back to agent
                        print(f"✗ Regression detected: {regression.failed_tests}")
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
    return False


def run_feature_with_checkpoints(feature: dict) -> bool:
    """
    Run a feature with git checkpoint/rollback support.
    """
    feature_id = feature.get("id", "unknown")

    # Create checkpoint before starting
    checkpoint = create_checkpoint(feature_id)

    session = FeatureSession(feature=feature, checkpoint=checkpoint)

    try:
        success = run_feature_loop(session)

        if success:
            # Run reflection to extract lessons
            print("\nRunning reflection...")
            iteration_history = [
                {
                    "error": r.error,
                    "response_summary": r.response_summary,
                    "verification_result": r.verification_result
                }
                for r in session.iterations
            ]
            learnings = run_reflection(feature, iteration_history)
            if learnings:
                save_learnings(learnings)
                print(f"Extracted {len(learnings)} lessons for future agents")

            return True
        else:
            # Rollback on failure
            print("\nRolling back changes...")
            rollback()
            return False

    except KeyboardInterrupt:
        print("\n\nInterrupted. Rolling back...")
        rollback()
        raise


def main():
    """Main entry point."""
    print("=" * 60)
    print("AUTONOMOUS AGENT HARNESS - CLI-Native Loop")
    print("=" * 60)

    # Verify Claude CLI is available
    try:
        subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            check=True,
            timeout=10
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        print("ERROR: 'claude' CLI not found or not working.")
        print("Install it: npm install -g @anthropic-ai/claude-code")
        return 1

    # Ensure git repo exists
    if not ensure_repo():
        print("ERROR: Could not initialize git repository")
        return 1

    print("Claude CLI ready")

    try:
        # Main loop
        while True:
            # Reload features each iteration
            data = load_features()
            features = data.get("features", [])

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
            success = run_feature_with_checkpoints(feature)

            if success:
                update_feature_status(feature["id"], "passing")
            else:
                update_feature_status(feature["id"], "failing", increment_retries=True)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
