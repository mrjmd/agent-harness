"""
Manual reviewer implementation.

This reviewer generates clipboard-ready packets for copy/paste into
external models (Gemini, ChatGPT, etc.) and parses the pasted response.

This is the Phase 1 implementation - no API integration, user is the bridge.
"""

import subprocess
import sys
from typing import Optional

from .base import ReviewResult


# Stage-specific review prompts
PROMPTS = {
    "architect": """You are a Security & Scalability Architect. Your job is to ATTACK this plan.

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
""",

    "implementer": """You are a Senior Code Reviewer. Tests have passed, but that's not enough.

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
""",

    "doctor": """You are a Principal Engineer reviewing a Junior Developer's diagnosis.

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
""",

    "archaeologist": """You are a Tech Lead reviewing extracted codebase patterns.

PATTERNS EXTRACTED:
{output}

TASK: Annotate patterns as:
- CURRENT: Good patterns to follow
- LEGACY: Tolerate but don't replicate
- ANTIPATTERN: Actively avoid

For each LEGACY/ANTIPATTERN, suggest the modern alternative.

Output the annotated patterns in the same format, adding your classification.
""",

    "generic": """You are reviewing the following output for quality and correctness.

OUTPUT:
{output}

CONTEXT:
{context}

Respond with APPROVED if acceptable, or provide specific feedback for improvements.
"""
}


class ManualReviewer:
    """
    Clipboard-based manual reviewer.

    Workflow:
    1. Generate review packet with stage-specific prompt
    2. Copy to system clipboard
    3. User pastes into external model (Gemini, ChatGPT)
    4. User copies response and pastes back
    5. Parse response for approval/rejection
    """

    @property
    def name(self) -> str:
        return "manual"

    def generate_packet(self, stage: str, context: dict, output: str) -> str:
        """Generate the review packet string."""
        template = PROMPTS.get(stage, PROMPTS["generic"])

        # Format context as readable string
        if isinstance(context, dict):
            context_str = "\n".join(f"- {k}: {v}" for k, v in context.items())
        else:
            context_str = str(context)

        return template.format(context=context_str, output=output)

    def copy_to_clipboard(self, text: str) -> bool:
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

    def parse_response(self, response: str) -> tuple[bool, list[dict]]:
        """
        Parse reviewer response for approval status and concerns.

        Returns:
            Tuple of (approved: bool, concerns: list[dict])
        """
        response_upper = response.upper()

        # Check for explicit approval
        approved = any(x in response_upper for x in ["LGTM", "APPROVED", "PASS"])

        # Check for explicit rejection
        if any(x in response_upper for x in ["CHANGES_REQUESTED", "REVISE", "REJECT", "FAIL"]):
            approved = False

        # Extract concerns if present
        concerns = []
        if "CONCERN" in response_upper:
            lines = response.split("\n")
            current_concern = None

            for line in lines:
                if line.upper().startswith("CONCERN"):
                    if current_concern:
                        concerns.append(current_concern)
                    # Extract title after CONCERN N:
                    title = line.split(":", 1)[1].strip() if ":" in line else line
                    current_concern = {"title": title, "details": []}
                elif current_concern and line.strip().startswith("-"):
                    current_concern["details"].append(line.strip()[1:].strip())

            if current_concern:
                concerns.append(current_concern)

        return approved, concerns

    def review(
        self,
        stage: str,
        context: dict,
        output: str,
        cycle: int = 1,
        max_cycles: int = 3
    ) -> ReviewResult:
        """
        Request manual review.

        Generates packet, copies to clipboard, prompts for response.
        """
        packet = self.generate_packet(stage, context, output)
        clipboard_success = self.copy_to_clipboard(packet)

        # Display review prompt
        print(f"\n{'=' * 60}")
        print(f"REVIEW BOARD - {stage.upper()} (Cycle {cycle}/{max_cycles})")
        print(f"{'=' * 60}")

        if clipboard_success:
            print("Review packet copied to clipboard.")
        else:
            print("Could not copy to clipboard. Packet printed below:")
            print("-" * 40)
            # Truncate very long packets for display
            if len(packet) > 3000:
                print(packet[:3000])
                print(f"\n... ({len(packet) - 3000} more characters)")
            else:
                print(packet)
            print("-" * 40)

        print("\nInstructions:")
        print("1. Paste the packet into Gemini/ChatGPT")
        print("2. Copy the response")
        print("3. Paste below (or type 'LGTM' to approve, 'SKIP' to skip review)")
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
            except KeyboardInterrupt:
                print("\nReview cancelled.")
                return ReviewResult(
                    approved=False,
                    feedback="Review cancelled by user",
                    reviewer=self.name,
                    cycle=cycle
                )

        response = "\n".join(lines).strip()

        # Handle skip
        if response.upper() == "SKIP":
            print("[REVIEW BOARD] Review skipped by user")
            return ReviewResult(
                approved=True,
                feedback="Skipped by user",
                reviewer=self.name,
                cycle=cycle
            )

        # Parse the response
        approved, concerns = self.parse_response(response)

        return ReviewResult(
            approved=approved,
            feedback=response if not approved else "",
            reviewer=self.name,
            cycle=cycle,
            concerns=concerns
        )
