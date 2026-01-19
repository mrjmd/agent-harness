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
import time
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TestRunnerConfig:
    """Configuration for the test runner."""
    runner: str  # playwright, pytest, jest, vitest
    base_cmd: list[str]
    timeout: int
    reporter_flag: str
    file_pattern: str


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


def detect_test_runner(config: dict) -> str:
    """
    Detect test runner from config or project files.

    Priority:
    1. Explicit testRunner setting in config
    2. Analyze testCommand string
    3. Detect from project files (pytest.ini, pyproject.toml)
    4. Default to playwright
    """
    settings = config.get("settings", {})

    # Explicit setting takes priority
    if "testRunner" in settings:
        return settings["testRunner"]

    # Analyze testCommand
    test_cmd = settings.get("testCommand", "")
    if "pytest" in test_cmd:
        return "pytest"
    elif "vitest" in test_cmd:
        return "vitest"
    elif "jest" in test_cmd:
        return "jest"

    # Analyze project files
    project_root = find_project_root()
    if (project_root / "pytest.ini").exists():
        return "pytest"
    if (project_root / "pyproject.toml").exists():
        try:
            content = (project_root / "pyproject.toml").read_text()
            if "[tool.pytest" in content or "pytest" in content.lower():
                return "pytest"
        except IOError:
            pass

    return "playwright"  # default


def get_reporter_flag(runner: str, config: dict) -> str:
    """
    Get appropriate reporter flag for the test runner.

    Can be overridden via testReporterFlag in config.
    """
    settings = config.get("settings", {})

    # Explicit setting takes priority
    if "testReporterFlag" in settings:
        return settings["testReporterFlag"]

    # Defaults per runner
    defaults = {
        "playwright": "--reporter=list",
        "pytest": "-v",
        "jest": "--verbose",
        "vitest": "--reporter=verbose",
    }
    return defaults.get(runner, "")


def get_default_file_pattern(runner: str) -> str:
    """Get default test file pattern for the runner."""
    patterns = {
        "playwright": "tests/e2e/test_{id}.spec.ts",
        "pytest": "tests/test_{id}.py",
        "jest": "tests/{id}.test.ts",
        "vitest": "tests/{id}.test.ts",
    }
    return patterns.get(runner, "tests/test_{id}")


def load_test_runner_config() -> TestRunnerConfig:
    """
    Load full test runner configuration.

    Returns TestRunnerConfig with runner type, command, timeout,
    reporter flag, and file pattern.
    """
    config_path = get_config_path()
    config = {}

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            pass

    settings = config.get("settings", {})
    runner = detect_test_runner(config)

    # Build command
    default_cmds = {
        "playwright": "npx playwright test",
        "pytest": "pytest",
        "jest": "npx jest",
        "vitest": "npx vitest run",
    }
    test_cmd_str = settings.get("testCommand", default_cmds.get(runner, "npx playwright test"))
    base_cmd = shlex.split(test_cmd_str)

    # Timeout
    timeout_ms = settings.get("testTimeout", 120000)
    timeout_sec = max(10, timeout_ms // 1000)

    # Reporter flag
    reporter_flag = get_reporter_flag(runner, config)

    # File pattern
    file_pattern = settings.get("testFilePattern", get_default_file_pattern(runner))

    return TestRunnerConfig(
        runner=runner,
        base_cmd=base_cmd,
        timeout=timeout_sec,
        reporter_flag=reporter_flag,
        file_pattern=file_pattern,
    )


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
    is_infrastructure_failure: bool = False  # True if failure is likely due to server crash, etc.
    retry_count: int = 0


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
    # Get runner config for default test file pattern
    config = load_test_runner_config()
    default_test_file = config.file_pattern.format(id=feature['id'])
    test_file = Path(feature.get("test_file", default_test_file))

    # Step 1: Test file must exist
    if not test_file.exists():
        return VerificationResult(
            passed=False,
            reason="Test file not created",
            test_file=str(test_file),
            stderr=f"Expected test file at: {test_file}"
        )

    # Step 2: THE HARNESS RUNS THE TEST (not the agent!)
    full_cmd = config.base_cmd + [str(test_file)]
    if config.reporter_flag:
        full_cmd.append(config.reporter_flag)

    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=config.timeout,
            cwd=Path.cwd()
        )
    except subprocess.TimeoutExpired:
        return VerificationResult(
            passed=False,
            reason=f"Test timed out ({config.timeout}s limit)",
            test_file=str(test_file),
            stderr=f"Test execution exceeded {config.timeout} second timeout"
        )
    except FileNotFoundError:
        return VerificationResult(
            passed=False,
            reason="Test runner not found",
            test_file=str(test_file),
            stderr=f"Command not found: {' '.join(config.base_cmd)}. Check your config."
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


def cleanup_stale_processes() -> None:
    """
    Kill stale server processes that might interfere with tests.

    Targets:
    - next-server (Next.js dev/prod server)
    - node processes on port 3000

    This helps prevent "address already in use" and server crash issues.
    """
    platform = sys.platform

    # Kill processes by name
    stale_patterns = ["next-server", "next dev", "next start"]

    for pattern in stale_patterns:
        try:
            if platform == "darwin" or platform.startswith("linux"):
                subprocess.run(
                    ["pkill", "-f", pattern],
                    capture_output=True,
                    timeout=5
                )
            elif platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/IM", "node.exe", "/FI", f"WINDOWTITLE eq *{pattern}*"],
                    capture_output=True,
                    timeout=5
                )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # Kill anything on port 3000 (common dev server port)
    try:
        if platform == "darwin" or platform.startswith("linux"):
            # Find PID using port 3000
            result = subprocess.run(
                ["lsof", "-ti", ":3000"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.stdout.strip():
                pids = result.stdout.strip().split('\n')
                for pid in pids:
                    if pid.strip():
                        subprocess.run(["kill", "-9", pid.strip()], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Brief pause to let OS clean up
    time.sleep(0.5)


def is_infrastructure_failure(output: str, failed_tests: list[str]) -> bool:
    """
    Detect if test failures are likely due to infrastructure issues.

    Signs of infrastructure failure:
    - ERR_CONNECTION_REFUSED (server died)
    - ECONNRESET (connection reset)
    - EADDRINUSE (port already in use)
    - Large number of failures that all occur after a certain point
    - Timeout errors
    """
    infra_patterns = [
        "ERR_CONNECTION_REFUSED",
        "ECONNRESET",
        "EADDRINUSE",
        "net::ERR_",
        "ETIMEDOUT",
        "socket hang up",
        "connect ECONNREFUSED",
        "Server is not running",
        "Could not connect",
        "Connection refused",
        "address already in use",
    ]

    output_upper = output.upper()
    for pattern in infra_patterns:
        if pattern.upper() in output_upper:
            return True

    # If more than 30% of tests failed AND they're spread across many files,
    # it's likely an infrastructure issue
    if len(failed_tests) > 20:
        unique_files = set()
        for test in failed_tests:
            # Extract file name
            if ".spec.ts" in test or ".test.ts" in test:
                parts = test.split(":")
                if parts:
                    unique_files.add(parts[0])
        # If failures span many different test files, likely infra issue
        if len(unique_files) > 10:
            return True

    return False


def regression_check(exclude_test: str = None, max_retries: int = 2, cleanup: bool = True) -> RegressionResult:
    """
    Run ALL tests to catch regressions.

    This should only be called on the FINAL verification before marking
    a feature as passing, not on every iteration.

    Args:
        exclude_test: Optional test file to exclude (already verified)
        max_retries: Number of retries for infrastructure failures (default 2)
        cleanup: Whether to kill stale processes before running (default True)

    Returns:
        RegressionResult with pass/fail and list of broken tests
    """
    config = load_test_runner_config()
    # For regression, use 5x single test timeout, capped at 10 minutes
    regression_timeout = min(config.timeout * 5, 600)

    full_cmd = list(config.base_cmd)  # Copy to avoid mutation
    if config.reporter_flag:
        full_cmd.append(config.reporter_flag)

    last_result = None
    for attempt in range(max_retries + 1):
        # Clean up stale processes before each attempt
        if cleanup:
            if attempt > 0:
                print(f"  [Retry {attempt}/{max_retries}] Cleaning up stale processes...")
            cleanup_stale_processes()

        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=regression_timeout,
                cwd=Path.cwd()
            )
        except subprocess.TimeoutExpired:
            last_result = RegressionResult(
                passed=False,
                failed_tests=["TIMEOUT"],
                output=f"Full test suite exceeded {regression_timeout}s timeout",
                is_infrastructure_failure=True,
                retry_count=attempt
            )
            if attempt < max_retries:
                print(f"  [Retry {attempt + 1}/{max_retries}] Test suite timed out, retrying...")
                continue
            return last_result
        except FileNotFoundError:
            return RegressionResult(
                passed=False,
                failed_tests=["TEST_RUNNER_NOT_FOUND"],
                output=f"Command not found: {' '.join(config.base_cmd)}",
                retry_count=attempt
            )

        if result.returncode == 0:
            return RegressionResult(
                passed=True,
                failed_tests=[],
                output=result.stdout,
                retry_count=attempt
            )

        # Parse failed tests from output
        combined_output = result.stdout + result.stderr
        failed_tests = parse_failed_tests(combined_output, config.runner)
        is_infra = is_infrastructure_failure(combined_output, failed_tests)

        last_result = RegressionResult(
            passed=False,
            failed_tests=failed_tests,
            output=result.stderr[-3000:],
            is_infrastructure_failure=is_infra,
            retry_count=attempt
        )

        # Only retry on infrastructure failures
        if is_infra and attempt < max_retries:
            print(f"  [Retry {attempt + 1}/{max_retries}] Detected infrastructure failure, retrying...")
            time.sleep(2)  # Brief pause before retry
            continue

        # Not an infra failure or out of retries
        return last_result

    return last_result


def parse_failed_tests(output: str, runner: str = None) -> list[str]:
    """
    Parse test output to extract failed test names.

    Supports multiple test runners: playwright, pytest, jest, vitest.

    Args:
        output: Combined stdout/stderr from test run
        runner: Test runner type (auto-detected if not provided)

    Returns:
        List of failed test identifiers
    """
    if runner is None:
        config = load_test_runner_config()
        runner = config.runner

    failed = []

    if runner == "pytest":
        # Pattern: "FAILED tests/test_foo.py::test_bar"
        pytest_pattern = r"FAILED\s+([\w/]+\.py::\w+)"
        failed.extend(re.findall(pytest_pattern, output))

        # Short form: "tests/test_foo.py::test_bar FAILED"
        short_pattern = r"([\w/]+\.py::\w+)\s+FAILED"
        failed.extend(re.findall(short_pattern, output))

        # Error pattern: "ERROR tests/test_foo.py"
        error_pattern = r"ERROR\s+([\w/]+\.py)"
        failed.extend(re.findall(error_pattern, output))

    elif runner in ("jest", "vitest"):
        # Pattern: "FAIL tests/foo.test.ts"
        jest_pattern = r"FAIL\s+([\w/]+\.test\.[tj]sx?)"
        failed.extend(re.findall(jest_pattern, output))

        # Pattern: "✕ test name"
        fail_pattern = r"[✕×]\s+(.+)"
        failed.extend(re.findall(fail_pattern, output))

    else:  # playwright (default)
        # Pattern for list reporter: "  ✘  1 test_auth.spec.ts:15:5 › Login"
        list_pattern = r"[✘×]\s+\d+\s+(.+\.spec\.ts:\d+:\d+)"
        failed.extend(re.findall(list_pattern, output))

        # Pattern for basic failures: "FAILED: test_name.spec.ts"
        basic_pattern = r"FAILED:\s*(.+\.spec\.ts)"
        failed.extend(re.findall(basic_pattern, output, re.IGNORECASE))

        # Pattern for error context: "Error in tests/e2e/test_foo.spec.ts"
        error_pattern = r"Error in (tests/e2e/.+\.spec\.ts)"
        failed.extend(re.findall(error_pattern, output))

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
    config = load_test_runner_config()
    cmd_str = " ".join(config.base_cmd)

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

The harness ran: {cmd_str} {result.test_file}
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
        retry_note = f" (passed on retry {result.retry_count})" if result.retry_count > 0 else ""
        return f"""
✓ REGRESSION CHECK PASSED{retry_note}

All tests in the suite still pass.
Feature {feature_id} did not break any existing functionality.
"""

    if result.is_infrastructure_failure:
        return f"""
⚠ REGRESSION CHECK FAILED - INFRASTRUCTURE ISSUE DETECTED

The test failures appear to be due to infrastructure problems, not code issues.
Detected signs: server crash, connection refused, port conflicts, or mass failures.

Attempted retries: {result.retry_count}
Failed tests count: {len(result.failed_tests)}

Sample failures:
{chr(10).join(f"  - {t}" for t in result.failed_tests[:10])}
{"  ... and more" if len(result.failed_tests) > 10 else ""}

Error output:
{result.output[:1500]}

RECOMMENDED ACTIONS:
1. The harness has already attempted to clean up stale processes
2. Check if port 3000 is free: lsof -i :3000
3. Kill any stale Next.js servers: pkill -f "next-server"
4. Try running tests manually: npx playwright test
5. If tests pass manually, this is a harness/environment issue, not a code issue

If tests consistently pass manually but fail in the harness, the feature implementation
may be correct. Consider marking as infrastructure-blocked if this persists.
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
