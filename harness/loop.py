#!/usr/bin/env python3
"""
Autonomous Agent Loop - The Brain

This script orchestrates the Plan -> Test -> Code cycle by:
1. Reading specs/features.json for the current backlog
2. Finding the next actionable feature (todo or failing)
3. Prompting Claude to implement it via TDD
4. Looping until all features pass
"""

import json
import subprocess
import sys
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Try to import anthropic SDK, fall back to CLI if not available
try:
    import anthropic
    USE_SDK = True
except ImportError:
    USE_SDK = False


FEATURES_PATH = Path("specs/features.json")
CLAUDE_MD_PATH = Path(".claude/CLAUDE.md")
MAX_RETRIES_PER_FEATURE = 5


def load_features() -> list[dict]:
    """Load the features backlog from specs/features.json."""
    if not FEATURES_PATH.exists():
        print(f"ERROR: {FEATURES_PATH} not found. Run planning phase first.")
        sys.exit(1)

    with open(FEATURES_PATH) as f:
        data = json.load(f)

    return data.get("features", [])


def save_features(features: list[dict]) -> None:
    """Save the features backlog back to specs/features.json."""
    with open(FEATURES_PATH, "w") as f:
        json.dump({"features": features}, f, indent=2)


def find_next_feature(features: list[dict]) -> Optional[dict]:
    """Find the next feature to work on (in_progress > failing > todo)."""
    # First, look for in_progress
    for feature in features:
        if feature.get("status") == "in_progress":
            return feature

    # Then, look for failing (needs retry)
    for feature in features:
        if feature.get("status") == "failing":
            if feature.get("retries", 0) < MAX_RETRIES_PER_FEATURE:
                return feature

    # Finally, look for todo
    for feature in features:
        if feature.get("status") == "todo":
            return feature

    return None


def update_feature_status(features: list[dict], feature_id: str, status: str) -> None:
    """Update a feature's status and timestamp."""
    for feature in features:
        if feature.get("id") == feature_id:
            feature["status"] = status
            feature["last_updated"] = datetime.now(timezone.utc).isoformat()
            if status == "failing":
                feature["retries"] = feature.get("retries", 0) + 1
            break
    save_features(features)


def build_prompt(feature: dict) -> str:
    """Build the prompt for Claude to implement a feature."""
    constitution = ""
    if CLAUDE_MD_PATH.exists():
        constitution = CLAUDE_MD_PATH.read_text()

    test_file = feature.get("test_file", f"tests/e2e/test_{feature['id']}.spec.ts")

    return f"""
# CONSTITUTION
{constitution}

# CURRENT TASK
You are working on feature: {feature['id']}
Description: {feature['description']}
Status: {feature['status']}
Test file: {test_file}

# INSTRUCTIONS
Follow the Test-First methodology strictly:

1. WRITE TEST: Create a failing Playwright test in `{test_file}` that verifies the feature works.

2. RUN TEST: Execute `npx playwright test {test_file}` to confirm it fails.
   - If it passes immediately, your test is not testing the right thing. Rewrite it.

3. IMPLEMENT: Write the minimum code to make the test pass.
   - Only modify files necessary for this specific feature.
   - Do not refactor unrelated code.

4. RUN TEST AGAIN: Execute `npx playwright test {test_file}` to confirm it passes.

5. UPDATE STATUS: Modify `specs/features.json`:
   - Set status to "passing" if test passes
   - Set status to "failing" if test still fails

6. COMMIT: If passing, run:
   ```bash
   git add -A
   git commit -m "PASSING: {feature['description']}"
   ```

7. EXIT: Once the feature is passing, stop and report success.

# OUTPUT
Report your actions step by step. End with either:
- "FEATURE PASSING: {feature['id']}" if successful
- "FEATURE FAILING: {feature['id']} - <reason>" if blocked
"""


def run_with_sdk(prompt: str) -> str:
    """Run Claude using the Anthropic SDK."""
    client = anthropic.Anthropic()

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    return message.content[0].text


def run_with_cli(prompt: str) -> str:
    """Run Claude using the CLI (claude-code or similar)."""
    # Write prompt to temp file to avoid shell escaping issues
    prompt_file = Path("/tmp/agent_prompt.txt")
    prompt_file.write_text(prompt)

    result = subprocess.run(
        ["claude", "--print", "-f", str(prompt_file)],
        capture_output=True,
        text=True,
        timeout=300
    )

    return result.stdout


def run_claude(prompt: str) -> str:
    """Run Claude using available method (SDK or CLI)."""
    if USE_SDK:
        return run_with_sdk(prompt)
    else:
        return run_with_cli(prompt)


def check_all_passing(features: list[dict]) -> bool:
    """Check if all features are passing."""
    return all(f.get("status") == "passing" for f in features)


def main():
    """Main loop: find feature, prompt Claude, repeat until done."""
    print("=" * 60)
    print("AUTONOMOUS AGENT LOOP - Starting")
    print("=" * 60)

    iteration = 0
    max_iterations = 100  # Safety limit

    while iteration < max_iterations:
        iteration += 1
        print(f"\n--- Iteration {iteration} ---")

        # Reload features each iteration (Claude may have modified them)
        features = load_features()

        # Check if we're done
        if check_all_passing(features):
            print("\n" + "=" * 60)
            print("SUCCESS: All features are passing!")
            print("=" * 60)
            return 0

        # Find next feature
        feature = find_next_feature(features)

        if not feature:
            # Check for blocked features
            blocked = [f for f in features if f.get("blocked")]
            if blocked:
                print(f"\nBLOCKED: {len(blocked)} features are blocked")
                for f in blocked:
                    print(f"  - {f['id']}: {f.get('block_reason', 'unknown')}")
                return 1

            # Check for max retries exceeded
            exhausted = [f for f in features if f.get("retries", 0) >= MAX_RETRIES_PER_FEATURE]
            if exhausted:
                print(f"\nEXHAUSTED: {len(exhausted)} features exceeded max retries")
                for f in exhausted:
                    print(f"  - {f['id']}")
                return 1

            print("\nNo actionable features found. Exiting.")
            return 0

        print(f"Working on: {feature['id']} ({feature['status']})")
        print(f"Description: {feature['description']}")

        # Update status to in_progress if it was todo
        if feature["status"] == "todo":
            update_feature_status(features, feature["id"], "in_progress")

        # Build and run prompt
        prompt = build_prompt(feature)

        try:
            response = run_claude(prompt)
            print("\n--- Claude Response ---")
            print(response[:2000] + "..." if len(response) > 2000 else response)

            # Check response for success/failure indicators
            if "FEATURE PASSING" in response:
                print(f"\nFeature {feature['id']} marked as passing by Claude")
            elif "FEATURE FAILING" in response:
                print(f"\nFeature {feature['id']} still failing")
                # Reload and update retry count
                features = load_features()
                update_feature_status(features, feature["id"], "failing")

        except subprocess.TimeoutExpired:
            print(f"\nTimeout while working on {feature['id']}")
            features = load_features()
            update_feature_status(features, feature["id"], "failing")

        except Exception as e:
            print(f"\nError while working on {feature['id']}: {e}")
            features = load_features()
            update_feature_status(features, feature["id"], "failing")

    print(f"\nMax iterations ({max_iterations}) reached. Exiting.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
