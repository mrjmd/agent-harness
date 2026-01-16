#!/usr/bin/env python3
"""
Bicameral Review Board - Cross-Model Adversarial Review

Manual-first implementation: generates clipboard-ready review packets.
User pastes into Gemini/other model, then pastes response back.

ENABLING/DISABLING:
    The review board is DISABLED by default. To enable:

    Option 1 - Environment variable (highest priority):
        export REVIEW_BOARD_ENABLED=1

    Option 2 - Config file (.claude/config.json):
        "reviewBoard": { "enabled": true, ... }

    To disable (even if config says enabled):
        export REVIEW_BOARD_ENABLED=0

CLI Usage:
    python harness/review_board.py check architect       # Check if review needed
    python harness/review_board.py check implementer f1  # Check with file list
    python harness/review_board.py packet architect      # Generate review packet
    python harness/review_board.py review architect      # Run interactive review
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Optional
import fnmatch
import json
import os
import subprocess
import sys

# Working Memory
try:
    from memory import record_decision, update_understanding
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False

# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class ReviewResult:
    """Result of a review request."""
    approved: bool
    feedback: str
    reviewer: str = "manual"
    cycle: int = 1


# ============================================================================
# Configuration
# ============================================================================

DEFAULT_CONFIG_PATH = Path(".claude/config.json")

DEFAULT_CRITICAL_PATHS = [
    "src/auth/**/*",
    "src/payment/**/*",
    "api/security/**/*",
    "**/middleware/auth*",
    "**/*webhook*",
]

MAX_REVIEW_CYCLES = 3


def load_review_config() -> dict:
    """Load review board configuration from .claude/config.json."""
    if DEFAULT_CONFIG_PATH.exists():
        try:
            config = json.loads(DEFAULT_CONFIG_PATH.read_text())
            return config.get("reviewBoard", {})
        except json.JSONDecodeError:
            pass
    return {}


def is_enabled() -> bool:
    """
    Check if review board is enabled.

    Priority:
    1. Environment variable REVIEW_BOARD_ENABLED (1/true/yes to enable, 0/false/no to disable)
    2. Config file .claude/config.json -> reviewBoard.enabled
    3. Default: disabled (opt-in for safety)

    To enable:
        export REVIEW_BOARD_ENABLED=1
        # or set "enabled": true in .claude/config.json
    """
    # Check environment variable first (highest priority)
    env_val = os.environ.get("REVIEW_BOARD_ENABLED", "").lower()
    if env_val in ("1", "true", "yes", "on"):
        return True
    if env_val in ("0", "false", "no", "off"):
        return False

    # Check config file
    config = load_review_config()
    return config.get("enabled", False)  # Disabled by default (opt-in)


def get_critical_paths() -> list[str]:
    """Get list of critical file path patterns that trigger review."""
    config = load_review_config()
    return config.get("criticalPaths", DEFAULT_CRITICAL_PATHS)


def get_max_cycles() -> int:
    """Get maximum review cycles before human escalation."""
    config = load_review_config()
    return config.get("maxCycles", MAX_REVIEW_CYCLES)


def _validate_provider_config() -> None:
    """Warn if configured provider is not implemented."""
    config = load_review_config()
    provider = config.get("provider", "manual")
    valid_providers = ["manual", "gemini"]
    if provider not in valid_providers:
        print(f"WARNING: Review board provider '{provider}' is not implemented.")
        print(f"Available providers: {', '.join(valid_providers)}")
        print("Falling back to 'manual' (clipboard-based) review.")


# Validate provider config when review board is enabled
_provider_validated = False


def _ensure_provider_validated() -> None:
    """Lazily validate provider config on first use."""
    global _provider_validated
    if not _provider_validated and is_enabled():
        _validate_provider_config()
        _provider_validated = True


# ============================================================================
# Review Triggers
# ============================================================================

def match_glob_pattern(file_path: str, pattern: str) -> bool:
    """
    Match a file path against a glob pattern.

    Supports ** for recursive directory matching.
    """
    # Convert ** to a regex pattern
    import re

    # Escape regex special chars except * and ?
    regex_pattern = ""
    i = 0
    while i < len(pattern):
        if pattern[i:i+2] == "**":
            regex_pattern += ".*"
            i += 2
            # Skip trailing slash after **
            if i < len(pattern) and pattern[i] == "/":
                regex_pattern += "/?"
                i += 1
        elif pattern[i] == "*":
            regex_pattern += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            regex_pattern += "[^/]"
            i += 1
        elif pattern[i] in ".^$+{}[]|()":
            regex_pattern += "\\" + pattern[i]
            i += 1
        else:
            regex_pattern += pattern[i]
            i += 1

    return bool(re.match(f"^{regex_pattern}$", file_path))


def should_review(stage: str, modified_files: Optional[list[str]] = None) -> bool:
    """
    Determine if review is required for this stage/change.

    Args:
        stage: One of "architect", "implementer", "doctor", "archaeologist"
        modified_files: List of file paths modified (for implementer stage)

    Returns:
        True if review should be triggered
    """
    if not is_enabled():
        return False

    # Validate provider config on first use
    _ensure_provider_validated()

    config = load_review_config()
    stages_config = config.get("stages", {})
    stage_config = stages_config.get(stage, {})

    # Architect always requires review (highest leverage)
    if stage == "architect":
        return stage_config.get("required", True)

    # For implementer, check if any modified files match critical paths
    if stage == "implementer" and modified_files:
        critical_paths = get_critical_paths()
        for file in modified_files:
            for pattern in critical_paths:
                if match_glob_pattern(file, pattern):
                    return True

    # Other stages: check if explicitly required
    return stage_config.get("required", False)


def check_critical_paths(modified_files: list[str]) -> list[tuple[str, str]]:
    """
    Check which modified files match critical paths.

    Returns:
        List of (file, matched_pattern) tuples
    """
    matches = []
    critical_paths = get_critical_paths()

    for file in modified_files:
        for pattern in critical_paths:
            if match_glob_pattern(file, pattern):
                matches.append((file, pattern))
                break  # Only report first matching pattern per file

    return matches


# ============================================================================
# Review Prompts (Stage-Specific)
# ============================================================================

ARCHITECT_PROMPT = """You are a Security & Scalability Architect. Your job is to ATTACK this plan.

TECHNICAL PLAN:
{output}

CONTEXT:
{context}

TASK: Find 3 ways this plan will fail:
1. Under load (10x, 100x users)
2. Security vulnerabilities (OWASP Top 10)
3. Technical debt traps (tight coupling, missing abstractions)

Be ruthless. Assume the worst. The plan cannot proceed until your concerns are addressed.

Format your response as:
CONCERN 1: [Title]
- Problem: ...
- Impact: ...
- Mitigation: ...

CONCERN 2: [Title]
- Problem: ...
- Impact: ...
- Mitigation: ...

CONCERN 3: [Title]
- Problem: ...
- Impact: ...
- Mitigation: ...

After listing concerns, provide a final verdict:
- APPROVED: If concerns are minor and mitigations are straightforward
- REVISE: If concerns require plan changes before proceeding
"""

IMPLEMENTER_PROMPT = """You are a Senior Code Reviewer. Tests have passed, but that's not enough.

FEATURE SPEC:
{context}

GIT DIFF:
{output}

TASK: Review this code for:
1. Logic bugs (off-by-one, null handling, race conditions)
2. Security issues (injection, auth bypass, data exposure)
3. Spec deviations (does it actually do what the spec says?)
4. Maintainability (will someone understand this in 6 months?)

Ignore style issues (linter handles that).

Respond with either:
- APPROVED: Code is good to commit
- CHANGES_REQUESTED: With specific line-by-line comments

If requesting changes, format as:
CHANGES_REQUESTED

File: <filename>
Line: <line_number>
Issue: <description>
Suggestion: <how to fix>
"""

DOCTOR_PROMPT = """You are a Principal Engineer reviewing a Junior Developer's diagnosis.

HEALTH REPORT:
{output}

RAW DATA:
{context}

TASK: Is the diagnosis correct, or is the Junior over-complicating things?

Common mistakes to check:
- Inventing complex fixes for simple problems (missing npm install, wrong node version)
- Misattributing errors (blaming tests when it's actually config)
- Over-engineering (rewriting configs when env var is missing)

Respond: APPROVED if diagnosis is sound, or REVISE with corrections.
"""

ARCHAEOLOGIST_PROMPT = """You are a Tech Lead reviewing extracted codebase patterns.

PATTERNS EXTRACTED:
{output}

TASK: Annotate patterns as:
- CURRENT: Good patterns to follow
- LEGACY: Tolerate but don't replicate
- ANTIPATTERN: Actively avoid

For each LEGACY/ANTIPATTERN, suggest the modern alternative.

Output the annotated patterns in the same format, adding your classification.
"""

BACKLOG_PROMPT = """You are a Principal Product Manager reviewing a Feature Backlog.

FEATURE BACKLOG:
{output}

CONTEXT:
{context}

Compare this backlog against the Project Goals and Technical Plan.

FIND:
1. **Missing Flows**: Did we forget logout? Error states? Loading indicators? Password reset?
2. **Dependency Gaps**: Does Feature B require Feature A but they're mis-ordered?
3. **Scope Creep**: Is any feature too big to implement in one iteration?
4. **Vague Criteria**: Are acceptance criteria actually testable and falsifiable?
5. **Edge Case Coverage**: Does each feature have at least 3 edge cases defined?

For each issue found, specify:
- Which feature ID is affected (or "NEW" if missing)
- What the problem is
- Your recommended fix

Be thorough. Don't be nice. Your job is to catch problems BEFORE implementation.

Format:
ISSUE 1: [Feature ID or NEW]
- Problem: ...
- Recommendation: ...

After listing issues, provide a verdict:
- APPROVED: Backlog is ready for implementation
- REVISE: Issues must be addressed first
"""

GENERIC_PROMPT = """You are reviewing the following output for quality and correctness.

OUTPUT:
{output}

CONTEXT:
{context}

Respond with APPROVED if acceptable, or provide specific feedback for improvements.
"""

REVIEW_PROMPTS = {
    "architect": ARCHITECT_PROMPT,
    "implementer": IMPLEMENTER_PROMPT,
    "doctor": DOCTOR_PROMPT,
    "archaeologist": ARCHAEOLOGIST_PROMPT,
    "backlog": BACKLOG_PROMPT,
}


# ============================================================================
# Packet Generation
# ============================================================================

def generate_packet(stage: str, context: dict, output: str) -> str:
    """
    Generate clipboard-ready review packet.

    Args:
        stage: Review stage name
        context: Dictionary of context values
        output: The output to be reviewed

    Returns:
        Formatted prompt string ready for external reviewer
    """
    template = REVIEW_PROMPTS.get(stage, GENERIC_PROMPT)

    # Format context as readable string if it's a dict
    if isinstance(context, dict):
        context_str = "\n".join(f"- {k}: {v}" for k, v in context.items())
    else:
        context_str = str(context)

    return template.format(context=context_str, output=output)


def copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard. Returns True on success."""
    try:
        if sys.platform == "darwin":
            process = subprocess.Popen(
                ["pbcopy"],
                stdin=subprocess.PIPE,
                env={"LANG": "en_US.UTF-8"}
            )
            process.communicate(text.encode("utf-8"))
            return process.returncode == 0
        elif sys.platform == "linux":
            # Try xclip first, then xsel
            for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
                try:
                    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                    process.communicate(text.encode("utf-8"))
                    if process.returncode == 0:
                        return True
                except FileNotFoundError:
                    continue
        elif sys.platform == "win32":
            process = subprocess.Popen(
                ["clip"],
                stdin=subprocess.PIPE,
                shell=True
            )
            process.communicate(text.encode("utf-16"))
            return process.returncode == 0
    except Exception:
        pass
    return False


# ============================================================================
# Review Request (Manual Implementation)
# ============================================================================

def request_review(
    stage: str,
    context: dict,
    output: str,
    cycle: int = 1
) -> ReviewResult:
    """
    Request review using the configured provider.

    Supports multiple providers:
    - manual: Clipboard-based (user pastes into Gemini/ChatGPT)
    - gemini: Automatic API calls to Google Gemini

    Configure in .claude/config.json:
        "reviewBoard": {
            "enabled": true,
            "provider": "gemini"  // or "manual"
        }

    For gemini provider, set GOOGLE_API_KEY or GEMINI_API_KEY env var.

    Args:
        stage: Review stage name
        context: Context dictionary for the review
        output: The output being reviewed
        cycle: Current review cycle (1-based)

    Returns:
        ReviewResult with approval status and feedback
    """
    config = load_review_config()
    provider = config.get("provider", "manual")
    max_cycles = get_max_cycles()

    # Use factory to get appropriate reviewer
    if provider != "manual":
        try:
            from .reviewers import get_reviewer
            reviewer = get_reviewer(provider)

            print(f"\n{'=' * 60}")
            print(f"REVIEW BOARD - {stage.upper()} (Cycle {cycle}/{max_cycles})")
            print(f"Provider: {provider}")
            print(f"{'=' * 60}")
            print(f"Sending to {provider.capitalize()} API...")

            result = reviewer.review(stage, context, output, cycle)

            # Record in working memory
            if MEMORY_AVAILABLE:
                record_decision(
                    "review_board",
                    f"Review: {stage} cycle {cycle}",
                    context=f"Stage: {stage}, Provider: {provider}",
                    options=["approve", "reject"],
                    chosen="approved" if result.approved else "rejected",
                    rationale=result.feedback[:200] if result.feedback else "Approved"
                )

            if result.approved:
                print(f"\n[{provider.upper()}] APPROVED")
            else:
                print(f"\n[{provider.upper()}] REVISE REQUESTED")
                if result.feedback:
                    print(f"\nFeedback:\n{result.feedback[:1500]}")

            return result

        except Exception as e:
            print(f"\nWarning: {provider} provider failed: {e}")
            print("Falling back to manual review...")
            # Fall through to manual review

    # Manual review workflow
    packet = generate_packet(stage, context, output)
    clipboard_success = copy_to_clipboard(packet)

    print(f"\n{'=' * 60}")
    print(f"REVIEW BOARD - {stage.upper()} (Cycle {cycle}/{max_cycles})")
    print(f"{'=' * 60}")

    if clipboard_success:
        print("Review packet copied to clipboard.")
    else:
        print("Could not copy to clipboard. Packet printed below:")
        print("-" * 40)
        print(packet[:2000] + ("..." if len(packet) > 2000 else ""))
        print("-" * 40)

    print("\nInstructions:")
    print("1. Paste the packet into Gemini/ChatGPT")
    print("2. Copy the response")
    print("3. Paste below (or type 'LGTM' to approve)")
    print(f"{'=' * 60}\n")

    # Collect multi-line response
    print("Reviewer response (end with empty line):")
    lines = []
    while True:
        try:
            line = input()
            if line == "":
                break
            lines.append(line)
        except EOFError:
            break

    response = "\n".join(lines).strip()

    # Parse response for approval
    response_upper = response.upper()
    approved = any(x in response_upper for x in ["LGTM", "APPROVED", "PASS"])

    # Check for explicit rejection
    if any(x in response_upper for x in ["CHANGES_REQUESTED", "REVISE", "REJECT", "FAIL"]):
        approved = False

    # Record review decision in working memory
    if MEMORY_AVAILABLE:
        record_decision(
            "review_board",
            f"Review: {stage} cycle {cycle}",
            context=f"Stage: {stage}, Output length: {len(output)} chars",
            options=["approve", "reject"],
            chosen="approved" if approved else "rejected",
            rationale=response[:200] if response else "No feedback provided"
        )

    return ReviewResult(
        approved=approved,
        feedback=response if not approved else "",
        reviewer="manual",
        cycle=cycle
    )


def run_review_loop(
    stage: str,
    context: dict,
    output: str,
    on_feedback: callable = None
) -> ReviewResult:
    """
    Run the full review loop with retry cycles.

    Args:
        stage: Review stage name
        context: Context dictionary
        output: Output being reviewed (may be updated between cycles)
        on_feedback: Callback function(feedback) -> new_output
                    Called when review is rejected, should return updated output

    Returns:
        Final ReviewResult (approved or escalated)
    """
    max_cycles = get_max_cycles()
    current_output = output

    for cycle in range(1, max_cycles + 1):
        result = request_review(stage, context, current_output, cycle)

        if result.approved:
            print(f"\n[REVIEW BOARD] Approved on cycle {cycle}")
            return result

        if cycle < max_cycles:
            print(f"\n[REVIEW BOARD] Review rejected. Cycle {cycle}/{max_cycles}")
            print(f"Feedback: {result.feedback[:500]}...")

            if on_feedback:
                # Let the caller handle feedback and provide updated output
                current_output = on_feedback(result.feedback)
            else:
                # No callback, just retry with same output
                print("\nPress Enter to retry, or Ctrl+C to abort...")
                try:
                    input()
                except KeyboardInterrupt:
                    print("\nAborted by user.")
                    return result

    # Max cycles reached - escalate to human
    print(f"\n{'=' * 60}")
    print("ESCALATION: Maximum review cycles reached")
    print(f"{'=' * 60}")
    print(f"Stage: {stage}")
    print(f"Cycles attempted: {max_cycles}")
    print(f"Last feedback: {result.feedback[:1000]}")
    print("\nThis requires human intervention to proceed.")

    return ReviewResult(
        approved=False,
        feedback=f"ESCALATED: {max_cycles} cycles without approval. Last feedback: {result.feedback}",
        reviewer="escalation",
        cycle=max_cycles
    )


# ============================================================================
# CLI Interface
# ============================================================================

def cmd_packet(stage: str, output_file: Optional[str] = None):
    """Generate and copy a review packet for the given stage."""
    # Load context based on stage
    context = {}
    output = ""

    if stage == "architect":
        tech_plan_path = Path("specs/tech_plan.md")
        if tech_plan_path.exists():
            output = tech_plan_path.read_text()
        else:
            print("Error: specs/tech_plan.md not found")
            return 1

        product_spec = Path("specs/product_spec.md")
        if product_spec.exists():
            context["product_spec"] = product_spec.read_text()[:2000]

    elif stage == "implementer":
        # Get staged diff
        try:
            result = subprocess.run(
                ["git", "diff", "--staged"],
                capture_output=True,
                text=True
            )
            output = result.stdout or "(no staged changes)"
        except Exception as e:
            output = f"Error getting diff: {e}"

        features_path = Path("specs/features.json")
        if features_path.exists():
            try:
                features = json.loads(features_path.read_text())
                # Find in_progress feature
                for f in features.get("features", []):
                    if f.get("status") == "in_progress":
                        context["feature"] = f
                        break
            except json.JSONDecodeError:
                pass

    packet = generate_packet(stage, context, output)

    if output_file:
        Path(output_file).write_text(packet)
        print(f"Packet written to: {output_file}")
    else:
        if copy_to_clipboard(packet):
            print("Packet copied to clipboard.")
        else:
            print(packet)

    return 0


def cmd_check(stage: str, *files: str):
    """Check if review is needed for the given stage/files."""
    modified_files = list(files) if files else None

    if not modified_files and stage == "implementer":
        # Get modified files from git
        try:
            result = subprocess.run(
                ["git", "diff", "--staged", "--name-only"],
                capture_output=True,
                text=True
            )
            modified_files = result.stdout.strip().split("\n") if result.stdout.strip() else []
        except Exception:
            modified_files = []

    needs_review = should_review(stage, modified_files)

    if needs_review:
        print(f"Review REQUIRED for stage: {stage}")
        if modified_files:
            matches = check_critical_paths(modified_files)
            if matches:
                print("\nCritical path matches:")
                for file, pattern in matches:
                    print(f"  {file} (matched: {pattern})")
        return 1
    else:
        print(f"Review NOT required for stage: {stage}")
        return 0


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Bicameral Review Board - Cross-Model Adversarial Review"
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # packet command
    packet_parser = subparsers.add_parser("packet", help="Generate review packet")
    packet_parser.add_argument("stage", choices=["architect", "implementer", "doctor", "archaeologist"])
    packet_parser.add_argument("-o", "--output", help="Output file (default: clipboard)")

    # check command
    check_parser = subparsers.add_parser("check", help="Check if review needed")
    check_parser.add_argument("stage", choices=["architect", "implementer", "doctor", "archaeologist"])
    check_parser.add_argument("files", nargs="*", help="Files to check (for implementer)")

    # review command (full interactive review)
    review_parser = subparsers.add_parser("review", help="Run interactive review")
    review_parser.add_argument("stage", choices=["architect", "implementer", "doctor", "archaeologist"])

    args = parser.parse_args()

    if args.command == "packet":
        return cmd_packet(args.stage, args.output)
    elif args.command == "check":
        return cmd_check(args.stage, *args.files)
    elif args.command == "review":
        # For interactive review, we need context
        context = {}
        output = ""

        if args.stage == "architect":
            tech_plan = Path("specs/tech_plan.md")
            if tech_plan.exists():
                output = tech_plan.read_text()

        result = request_review(args.stage, context, output)
        print(f"\nResult: {'APPROVED' if result.approved else 'REJECTED'}")
        return 0 if result.approved else 1
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
