#!/usr/bin/env python3
"""
Autonomous Agent Loop - MCP-Native Implementation

This script orchestrates the Plan -> Test -> Code cycle with:
1. MCP server integration for real tool execution
2. External verification (harness runs tests, not agent)
3. Git checkpoints (commit on green, reset on red)
4. Regression fence (all tests must pass)
5. Repository map for brownfield safety
6. Reflection for knowledge transfer

The key insight: Claude actually executes tools through MCP servers,
not just talking about what it would do.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

# Local modules
from mcp_manager import MCPManager
from verify import (
    verify_feature,
    regression_check,
    claims_completion,
    format_verification_feedback,
    format_regression_feedback
)
from git_utils import (
    get_status,
    create_checkpoint,
    commit_feature,
    rollback,
    ensure_repo
)
from repo_map import build_feature_context
from reflection import run_reflection, save_learnings
from review import enforce_patterns, PatternViolation, get_modified_files

# Anthropic SDK
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    print("Warning: anthropic SDK not installed. Run: pip install anthropic")


# Configuration
FEATURES_PATH = Path("specs/features.json")
CLAUDE_MD_PATH = Path(".claude/CLAUDE.md")
MAX_RETRIES_PER_FEATURE = 5
MAX_ITERATIONS_PER_FEATURE = 20

# Model selection
MODELS = {
    "coding": "claude-sonnet-4-20250514",      # Sonnet for coding (sharper at syntax)
    "specification": "claude-opus-4-5-20251101",  # Opus for high-level reasoning
    "reflection": "claude-opus-4-5-20251101"     # Opus for lesson extraction
}


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
    messages: list = field(default_factory=list)
    iterations: list = field(default_factory=list)
    checkpoint: str = ""


def validate_feature(feature: dict) -> list[str]:
    """
    Validate a feature against the shared schema.
    Returns a list of warnings/errors.
    """
    warnings = []
    feature_id = feature.get("id", "unknown")

    # Required fields
    required = ["id", "description", "acceptance_criteria", "edge_cases", "priority"]
    for field in required:
        if field not in feature:
            warnings.append(f"[{feature_id}] Missing required field: {field}")

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


def extract_text_from_response(response) -> str:
    """Extract text content from Claude API response."""
    text_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
    return "\n".join(text_parts)


def run_feature_loop(
    session: FeatureSession,
    client: "anthropic.Anthropic",
    mcp: Optional[MCPManager] = None
) -> bool:
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

    # Build initial context with repo map
    initial_context = build_feature_context(feature)

    session.messages = [{
        "role": "user",
        "content": initial_context
    }]

    # Get tools from MCP if available
    tools = mcp.get_all_tools() if mcp else []

    iteration = 0
    while iteration < MAX_ITERATIONS_PER_FEATURE:
        iteration += 1
        print(f"\n--- Iteration {iteration}/{MAX_ITERATIONS_PER_FEATURE} ---")

        try:
            # Call Claude
            response = client.messages.create(
                model=MODELS["coding"],
                max_tokens=8192,
                tools=tools if tools else None,
                messages=session.messages
            )

            # Handle tool use
            while response.stop_reason == "tool_use":
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        print(f"  Tool: {block.name}")
                        if mcp:
                            result = mcp.execute_tool(block.name, block.input)
                        else:
                            result = json.dumps({"error": "MCP not available"})
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result
                        })

                # Continue conversation with tool results
                session.messages.append({"role": "assistant", "content": response.content})
                session.messages.append({"role": "user", "content": tool_results})

                response = client.messages.create(
                    model=MODELS["coding"],
                    max_tokens=8192,
                    tools=tools if tools else None,
                    messages=session.messages
                )

            # Extract response text
            response_text = extract_text_from_response(response)
            print(f"\nResponse preview: {response_text[:500]}...")

            # Record iteration
            record = IterationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                iteration=iteration,
                response_summary=response_text[:1000]
            )

            # Check if agent claims completion
            if claims_completion(response_text):
                print("\nAgent claims completion. Running verification...")

                # EXTERNAL VERIFICATION - harness runs the test
                verification = verify_feature(feature, response_text)
                record.verification_result = verification.reason

                if verification.passed:
                    print(f"✓ Feature test passes!")

                    # Check pattern compliance before proceeding
                    print("Checking pattern compliance...")
                    try:
                        modified_files = get_modified_files()
                        enforce_patterns(modified_files)
                        print("✓ Pattern compliance OK")
                    except PatternViolation as e:
                        print(f"✗ Pattern violation: {e}")
                        feedback = f"""Your changes violate established project patterns:

{e}

You MUST fix these violations before the feature can be completed.
Review specs/context/patterns.md for the required patterns.
Modify your code to comply with the existing codebase conventions."""
                        session.messages.append({"role": "assistant", "content": response.content})
                        session.messages.append({"role": "user", "content": feedback})
                        record.error = f"Pattern violation: {str(e)[:200]}"
                        session.iterations.append(record)
                        continue  # Let agent fix it

                    # Run regression check before final commit
                    print("Running regression check...")
                    regression = regression_check()

                    if regression.passed:
                        print("✓ All tests pass! Committing...")
                        commit_feature(feature_id, feature.get("description", ""))
                        session.iterations.append(record)
                        return True
                    else:
                        # Regression detected - feed back to agent
                        print(f"✗ Regression detected: {regression.failed_tests}")
                        feedback = format_regression_feedback(regression, feature_id)
                        session.messages.append({"role": "assistant", "content": response.content})
                        session.messages.append({"role": "user", "content": feedback})
                        record.error = f"Regression: {regression.failed_tests}"
                else:
                    # Verification failed - feed error back to agent
                    print(f"✗ Verification failed: {verification.reason}")
                    feedback = format_verification_feedback(verification)
                    session.messages.append({"role": "assistant", "content": response.content})
                    session.messages.append({"role": "user", "content": feedback})
                    record.error = verification.stderr[:500]
            else:
                # Agent still working - continue conversation
                session.messages.append({"role": "assistant", "content": response.content})

            session.iterations.append(record)

        except Exception as e:
            print(f"Error in iteration: {e}")
            session.iterations.append(IterationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                iteration=iteration,
                error=str(e)
            ))

    print(f"\nMax iterations ({MAX_ITERATIONS_PER_FEATURE}) reached for {feature_id}")
    return False


def run_feature_with_checkpoints(
    feature: dict,
    client: "anthropic.Anthropic",
    mcp: Optional[MCPManager] = None
) -> bool:
    """
    Run a feature with git checkpoint/rollback support.
    """
    feature_id = feature.get("id", "unknown")

    # Create checkpoint before starting
    checkpoint = create_checkpoint(feature_id)

    session = FeatureSession(feature=feature, checkpoint=checkpoint)

    try:
        success = run_feature_loop(session, client, mcp)

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
            learnings = run_reflection(feature, iteration_history, client)
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
    print("AUTONOMOUS AGENT HARNESS - MCP-Native Loop")
    print("=" * 60)

    # Verify prerequisites
    if not HAS_ANTHROPIC:
        print("ERROR: anthropic SDK required. Run: pip install anthropic")
        return 1

    # Ensure git repo exists
    if not ensure_repo():
        print("ERROR: Could not initialize git repository")
        return 1

    # Initialize Anthropic client
    client = anthropic.Anthropic()

    # Initialize MCP servers
    mcp = None
    try:
        mcp = MCPManager.from_config()
        tools = mcp.get_all_tools()
        print(f"MCP servers loaded: {len(tools)} tools available")
    except FileNotFoundError:
        print("Warning: No MCP config found. Running without tool execution.")
    except Exception as e:
        print(f"Warning: MCP initialization failed: {e}")
        print("Running without tool execution.")

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
            success = run_feature_with_checkpoints(feature, client, mcp)

            if success:
                update_feature_status(feature["id"], "passing")
            else:
                update_feature_status(feature["id"], "failing", increment_retries=True)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        return 130
    finally:
        if mcp:
            mcp.shutdown()


if __name__ == "__main__":
    sys.exit(main())
