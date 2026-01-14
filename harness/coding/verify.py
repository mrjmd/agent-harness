#!/usr/bin/env python3
"""
External Verification Module

The "killer feature" that prevents false positives.

The harness - not the agent - runs the final test verification.
This catches:
- Agent hallucinating that tests pass
- Agent skipping test runs
- Agent modifying wrong files
- Regressions in other features
"""

import json
import shlex
import subprocess
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


def find_project_root() -> Path:
    """
    Find the project root by walking up from cwd looking for markers.

    Markers checked (in order):
    1. .claude/ directory
    2. .git directory
    3. package.json file

    Returns cwd if no markers found.
    """
    current = Path.cwd().resolve()

    for directory in [current] + list(current.parents):
        # Check for .claude directory (our marker)
        if (directory / ".claude").is_dir():
            return directory
        # Check for .git (common root marker)
        if (directory / ".git").exists():
            return directory
        # Check for package.json
        if (directory / "package.json").is_file():
            return directory

    # Fallback to cwd
    return Path.cwd()


def get_config_path() -> Path:
    """Get the config path relative to project root."""
    return find_project_root() / ".claude" / "config.json"


def load_test_config() -> tuple[list[str], int]:
    """
    Load test command and timeout from config.json.

    Returns:
        Tuple of (command_args: list[str], timeout_seconds: int)
        Falls back to defaults if config missing or invalid.
    """
    # Defaults
    default_cmd = ["npx", "playwright", "test"]
    default_timeout = 120

    config_path = get_config_path()
    if not config_path.exists():
        return default_cmd, default_timeout

    try:
        config = json.loads(config_path.read_text())
        settings = config.get("settings", {})

        # Parse test command - handles quoted strings properly
        test_cmd_str = settings.get("testCommand", "npx playwright test")
        test_cmd = shlex.split(test_cmd_str)

        # Parse timeout (config is in ms, convert to seconds)
        timeout_ms = settings.get("testTimeout", 120000)
        timeout_sec = max(10, timeout_ms // 1000)  # Minimum 10 seconds

        return test_cmd, timeout_sec

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"Warning: Could not load test config: {e}. Using defaults.")
        return default_cmd, default_timeout


@dataclass
class VerificationResult:
    """Result of verifying a feature."""
    passed: bool
    reason: str
    stdout: str = ""
    stderr: str = ""
    test_file: str = ""
    duration_ms: int = 0


@dataclass
class RegressionResult:
    """Result of regression check."""
    passed: bool
    failed_tests: list[str]
    output: str = ""


def verify_feature(feature: dict, agent_response: str = "") -> VerificationResult:
    """
    External verification - the harness runs the test, not the agent.

    This is the critical safeguard that ensures tests actually pass.

    Args:
        feature: The feature dict from features.json
        agent_response: Optional agent response text to check claims

    Returns:
        VerificationResult with pass/fail status and details
    """
    test_file = Path(feature.get("test_file", f"tests/e2e/test_{feature['id']}.spec.ts"))

    # Step 1: Test file must exist
    if not test_file.exists():
        return VerificationResult(
            passed=False,
            reason="Test file not created",
            test_file=str(test_file),
            stderr=f"Expected test file at: {test_file}"
        )

    # Step 2: THE HARNESS RUNS THE TEST (not the agent!)
    base_cmd, timeout = load_test_config()
    full_cmd = base_cmd + [str(test_file), "--reporter=list"]

    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=Path.cwd()
        )
    except subprocess.TimeoutExpired:
        return VerificationResult(
            passed=False,
            reason=f"Test timed out ({timeout}s limit)",
            test_file=str(test_file),
            stderr=f"Test execution exceeded {timeout} second timeout"
        )
    except FileNotFoundError:
        return VerificationResult(
            passed=False,
            reason="Test runner not found",
            test_file=str(test_file),
            stderr=f"Command not found: {' '.join(base_cmd)}. Check your config."
        )

    tests_pass = result.returncode == 0
    stdout = result.stdout[-5000:] if len(result.stdout) > 5000 else result.stdout
    stderr = result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr

    # Step 3: Check what agent claimed vs reality
    agent_claims_done = False
    if agent_response:
        agent_claims_done = any(phrase in agent_response for phrase in [
            "FEATURE PASSING",
            "test passes",
            "tests pass",
            "completed successfully",
            "implementation complete"
        ])

    if agent_claims_done and not tests_pass:
        # Agent lied or hallucinated
        return VerificationResult(
            passed=False,
            reason="Agent claimed completion but tests FAIL",
            test_file=str(test_file),
            stdout=stdout,
            stderr=stderr
        )

    if tests_pass:
        return VerificationResult(
            passed=True,
            reason="Tests pass (verified by harness)",
            test_file=str(test_file),
            stdout=stdout
        )

    return VerificationResult(
        passed=False,
        reason="Tests still failing",
        test_file=str(test_file),
        stdout=stdout,
        stderr=stderr
    )


def regression_check(exclude_test: str = None) -> RegressionResult:
    """
    Run ALL e2e tests to catch regressions.

    This should only be called on the FINAL verification before marking
    a feature as passing, not on every iteration.

    Args:
        exclude_test: Optional test file to exclude (already verified)

    Returns:
        RegressionResult with pass/fail and list of broken tests
    """
    base_cmd, single_timeout = load_test_config()
    # For regression, use 5x single test timeout, capped at 10 minutes
    regression_timeout = min(single_timeout * 5, 600)
    full_cmd = base_cmd + ["--reporter=list"]

    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=regression_timeout,
            cwd=Path.cwd()
        )
    except subprocess.TimeoutExpired:
        return RegressionResult(
            passed=False,
            failed_tests=["TIMEOUT"],
            output=f"Full test suite exceeded {regression_timeout}s timeout"
        )
    except FileNotFoundError:
        return RegressionResult(
            passed=False,
            failed_tests=["TEST_RUNNER_NOT_FOUND"],
            output=f"Command not found: {' '.join(base_cmd)}"
        )

    if result.returncode == 0:
        return RegressionResult(
            passed=True,
            failed_tests=[],
            output=result.stdout
        )

    # Parse failed tests from output
    failed_tests = parse_failed_tests(result.stdout + result.stderr)

    return RegressionResult(
        passed=False,
        failed_tests=failed_tests,
        output=result.stderr[-3000:]
    )


def parse_failed_tests(output: str) -> list[str]:
    """
    Parse Playwright output to extract failed test names.

    Handles various Playwright reporter formats.
    """
    failed = []

    # Pattern for list reporter: "  ✘  1 test_auth.spec.ts:15:5 › Login › should fail with invalid credentials"
    list_pattern = r"[✘×]\s+\d+\s+(.+\.spec\.ts:\d+:\d+)"
    matches = re.findall(list_pattern, output)
    failed.extend(matches)

    # Pattern for basic failures: "FAILED: test_name.spec.ts"
    basic_pattern = r"FAILED:\s*(.+\.spec\.ts)"
    matches = re.findall(basic_pattern, output, re.IGNORECASE)
    failed.extend(matches)

    # Pattern for error context: "Error in tests/e2e/test_foo.spec.ts"
    error_pattern = r"Error in (tests/e2e/.+\.spec\.ts)"
    matches = re.findall(error_pattern, output)
    failed.extend(matches)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for test in failed:
        if test not in seen:
            seen.add(test)
            unique.append(test)

    return unique if unique else ["Unknown test failure - check output"]


def claims_completion(text: str) -> bool:
    """
    Check if text indicates the agent claims the feature is complete.

    Used to determine when to trigger external verification.
    """
    completion_phrases = [
        "FEATURE PASSING",
        "feature is passing",
        "test passes",
        "tests pass",
        "all tests pass",
        "implementation complete",
        "completed successfully",
        "feature complete",
        "task complete"
    ]

    text_lower = text.lower()
    return any(phrase.lower() in text_lower for phrase in completion_phrases)


def format_verification_feedback(result: VerificationResult) -> str:
    """
    Format verification result as feedback for the agent.

    This gets injected back into the conversation when verification fails.
    """
    if result.passed:
        return f"""
✓ VERIFICATION PASSED

The harness independently verified that tests pass.
Test file: {result.test_file}

{result.stdout[:1000] if result.stdout else ""}
"""

    return f"""
✗ VERIFICATION FAILED

Your claim of completion was REJECTED by the verification system.

The harness ran: npx playwright test {result.test_file}
Result: FAILED
Reason: {result.reason}

Error output:
{result.stderr}

Stdout:
{result.stdout[:1500] if result.stdout else "(empty)"}

Please analyze this error and try again. Do NOT claim the feature is passing
until you are certain the test actually passes.
"""


def format_regression_feedback(result: RegressionResult, feature_id: str) -> str:
    """
    Format regression check result as feedback for the agent.
    """
    if result.passed:
        return f"""
✓ REGRESSION CHECK PASSED

All tests in the suite still pass.
Feature {feature_id} did not break any existing functionality.
"""

    return f"""
✗ REGRESSION CHECK FAILED

Feature {feature_id} broke other tests!

Failed tests:
{chr(10).join(f"  - {t}" for t in result.failed_tests)}

Error output:
{result.output}

You must fix these regressions before the feature can be marked as passing.
The implementation likely affected shared code or dependencies.
"""


# Test the module
if __name__ == "__main__":
    print("Testing verification module...")

    # Create a mock feature
    mock_feature = {
        "id": "test-feature",
        "test_file": "tests/e2e/test_example.spec.ts"
    }

    # Test verification
    result = verify_feature(mock_feature)
    print(f"\nVerification result:")
    print(f"  Passed: {result.passed}")
    print(f"  Reason: {result.reason}")

    # Test regression check
    print("\nRegression check...")
    regression = regression_check()
    print(f"  Passed: {regression.passed}")
    print(f"  Failed tests: {regression.failed_tests}")
