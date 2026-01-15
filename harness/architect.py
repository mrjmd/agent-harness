#!/usr/bin/env python3
"""
Socratic Architect - Adversarial Specification System

This script interrogates users to eliminate specification ambiguity before
any code is written. It enforces the Five Gates model:

1. Problem Discovery - WHY, not HOW
2. Solution Space - explore 3+ alternatives
3. Technical Design - lock down architecture
4. Edge Cases - 3+ per feature (Rule of 3)
5. Synthesis - generate features.json

The key insight: Garbage specs -> garbage code.
This system refuses to generate specs until ambiguity is eliminated.

Uses the `claude` CLI for flat-rate subscription compatibility.
"""

import argparse
import json
import subprocess
import sys
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Review Board (Bicameral Mind)
try:
    from review_board import should_review, request_review, run_review_loop
    REVIEW_BOARD_AVAILABLE = True
except ImportError:
    REVIEW_BOARD_AVAILABLE = False

# Working Memory
try:
    from memory import (
        read_memory,
        record_answer,
        record_decision,
        update_understanding,
        get_memory_context,
        extract_qa_pairs,
    )
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False


# Paths
SPECS_DIR = Path("specs")
SESSION_PATH = SPECS_DIR / "session.json"
FEATURES_PATH = SPECS_DIR / "features.json"
TECH_PLAN_PATH = SPECS_DIR / "tech_plan.md"
HEALTH_REPORT_PATH = SPECS_DIR / "health_report.md"

# Gate definitions
GATES = ["problem", "solution", "technical", "edges", "synthesis", "complete"]

# Context window management
MAX_HISTORY_MESSAGES = 20  # Keep at most this many recent messages verbatim
SUMMARY_TRIGGER_COUNT = 25  # Summarize when messages exceed this count


# =============================================================================
# CLI Wrapper
# =============================================================================

def call_claude_cli(prompt_text: str) -> str:
    """
    Call claude CLI with formatted prompt.

    Uses --print for non-interactive mode and --dangerously-skip-permissions
    to avoid permission prompts.
    """
    try:
        result = subprocess.run(
            ["claude", "--print", prompt_text, "--dangerously-skip-permissions"],
            capture_output=True,
            text=True,
            check=True,
            timeout=300  # 5 minute timeout
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Claude CLI error: {e.stderr}")
        raise
    except subprocess.TimeoutExpired:
        print("Claude CLI timed out after 5 minutes")
        raise
    except FileNotFoundError:
        print("ERROR: 'claude' CLI not found. Install it first.")
        print("See: https://github.com/anthropics/claude-cli")
        sys.exit(1)


# XML-style delimiters for injection protection
XML_DELIMITERS = {
    "system_start": "<|SYSTEM_INSTRUCTIONS|>",
    "system_end": "</|SYSTEM_INSTRUCTIONS|>",
    "history_start": "<|CONVERSATION_HISTORY|>",
    "history_end": "</|CONVERSATION_HISTORY|>",
    "user_start": "<|USER_INPUT|>",
    "user_end": "</|USER_INPUT|>",
    "response_start": "<|ASSISTANT_RESPONSE|>",
}


def sanitize_user_input(text: str) -> str:
    """
    Sanitize user input to prevent delimiter injection.

    Escapes any strings that look like our delimiters and flags suspicious patterns.
    """
    # Escape our delimiter patterns
    for delimiter in XML_DELIMITERS.values():
        escaped = delimiter.replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(delimiter, escaped)

    # Flag common injection patterns (but don't block - could be legitimate)
    injection_patterns = [
        "SYSTEM INSTRUCTIONS",
        "IGNORE PREVIOUS",
        "NEW INSTRUCTIONS",
        "DISREGARD",
    ]
    for pattern in injection_patterns:
        if pattern.upper() in text.upper():
            text = f"[NOTE: Input contained pattern '{pattern}']\n{text}"
            break

    return text


def format_conversation(system: str, messages: list, current_input: str) -> str:
    """
    Format full conversation for CLI input with injection protection.

    Uses XML-style delimiters that are harder to forge than plain text markers.
    """
    parts = []

    # System prompt with secure delimiters
    parts.append(XML_DELIMITERS["system_start"])
    parts.append(system)
    parts.append(XML_DELIMITERS["system_end"])
    parts.append("")

    # Conversation history with role markers
    if messages:
        parts.append(XML_DELIMITERS["history_start"])
        for msg in messages:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            # Sanitize historical content too (could contain injections)
            content = sanitize_user_input(content)
            parts.append(f"\n<|{role}|>")
            parts.append(content)
            parts.append(f"</|{role}|>")
        parts.append(XML_DELIMITERS["history_end"])
        parts.append("")

    # Current user input - sanitized
    parts.append(XML_DELIMITERS["user_start"])
    parts.append(sanitize_user_input(current_input))
    parts.append(XML_DELIMITERS["user_end"])
    parts.append("")

    # Response marker
    parts.append(XML_DELIMITERS["response_start"])
    parts.append("(Your response as the Socratic Architect)")

    return "\n".join(parts)


# =============================================================================
# Context Window Management
# =============================================================================

def summarize_history(messages: list) -> list:
    """
    Summarize conversation history when it exceeds SUMMARY_TRIGGER_COUNT.

    Keeps the most recent MAX_HISTORY_MESSAGES verbatim and summarizes
    older messages into bullet points.

    Args:
        messages: Full conversation history

    Returns:
        Condensed message list with summary prefix
    """
    if len(messages) <= SUMMARY_TRIGGER_COUNT:
        return messages

    print("(Summarizing conversation history...)")

    # Split into old (to summarize) and recent (to keep)
    split_point = len(messages) - MAX_HISTORY_MESSAGES
    old_messages = messages[:split_point]
    recent_messages = messages[split_point:]

    # CRITICAL: Persist Q&A pairs to memory BEFORE they get summarized
    if MEMORY_AVAILABLE:
        qa_pairs = extract_qa_pairs(old_messages)
        if qa_pairs:
            print(f"(Persisting {len(qa_pairs)} Q&A pairs to working memory...)")
            for question, answer in qa_pairs:
                record_answer("architect", question, answer)

    # Build summary prompt
    summary_input = []
    for msg in old_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")[:500]  # Truncate long messages
        summary_input.append(f"{role.upper()}: {content}")

    summary_prompt = f"""Summarize the following conversation history into 5-10 bullet points.
Focus on: decisions made, key requirements discovered, blockers identified.

CONVERSATION:
{chr(10).join(summary_input)}

SUMMARY (bullet points only):"""

    try:
        summary = call_claude_cli(summary_prompt)

        # Create a summary message to prepend
        summary_message = {
            "role": "system",
            "content": f"[CONVERSATION SUMMARY - {len(old_messages)} messages condensed]\n{summary}"
        }

        return [summary_message] + recent_messages

    except Exception as e:
        print(f"Warning: Summarization failed ({e}), using truncation fallback")
        # Fallback: just truncate with a marker
        truncation_marker = {
            "role": "system",
            "content": f"[{len(old_messages)} earlier messages truncated for context management]"
        }
        return [truncation_marker] + recent_messages


# =============================================================================
# State Management
# =============================================================================

@dataclass
class SpecificationState:
    """Persisted to specs/session.json"""
    phase: str = "problem"

    # Gate 1 outputs
    problem_statement: str = ""
    user_personas: list = field(default_factory=list)

    # Gate 2 outputs
    solution_alternatives: list = field(default_factory=list)
    chosen_approach: dict = field(default_factory=dict)

    # Gate 3 outputs
    tech_plan_generated: bool = False

    # Gate 4 outputs (per-feature)
    features: list = field(default_factory=list)

    # Conversation history for context
    messages: list = field(default_factory=list)

    # Metadata
    created_at: str = ""
    last_updated: str = ""
    product_idea: str = ""


def load_state() -> Optional[SpecificationState]:
    """Load existing session state."""
    if not SESSION_PATH.exists():
        return None

    try:
        data = json.loads(SESSION_PATH.read_text())
        return SpecificationState(**data)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"Warning: Could not load session: {e}")
        return None


def save_state(state: SpecificationState) -> None:
    """Persist session state."""
    state.last_updated = datetime.now(timezone.utc).isoformat()
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps(asdict(state), indent=2))


# =============================================================================
# Prompts
# =============================================================================

SYSTEM_PROMPT = """You are the SOCRATIC ARCHITECT. Your job is to PREVENT specification failures.

PRIME DIRECTIVE: Refuse to generate specs until ambiguity is eliminated.

RULES:
1. NEVER accept vague terms without clarification
2. ALWAYS apply Rule of 3 (3+ edge cases per feature)
3. CHALLENGE every "obviously" and "just"
4. ENFORCE granularity (>1 day implementation = too big, must decompose)
5. DEMAND falsifiability (testable acceptance criteria)

VAGUE TERMS TO CHALLENGE:
- "login" → Magic link? Password? OAuth? 2FA? Passkeys?
- "save" → Auto-save? Manual? Cloud? Local?
- "notification" → Email? Push? In-app? SMS?
- "user" → Admin? Member? Guest? Anonymous?
- "fast" → How many ms? What's acceptable?
- "secure" → What threat model? What's the risk?

You are NOT here to help the user feel good.
You are here to PREVENT implementation failures.

Be thorough. Be annoying. Be right.

Current phase: {phase}
"""

GATE_PROMPTS = {
    "problem": """## GATE 1: Problem Discovery

Your objective: Understand the PROBLEM, not the solution.

Questions to explore:
- "Who experiences this problem?"
- "How painful is it? (1-10)"
- "How do they currently cope?"
- "What would 'solved' look like?"

EXIT CRITERIA (all must be met):
[ ] Problem articulated WITHOUT mentioning solutions
[ ] At least one user persona identified
[ ] User confirmed the problem statement

ANTI-PATTERN: If user describes a solution, respond with:
"That's HOW. Tell me WHY. What problem does this solve?"

When all criteria are met, say: "GATE 1 COMPLETE. Moving to Solution Space."
""",

    "solution": """## GATE 2: Solution Space

Your objective: Explore alternatives BEFORE committing.

Questions to explore:
- "What are THREE different ways to solve this?"
- "What are the trade-offs of each approach?"
- "What's the SIMPLEST solution that could work?"

ASSUMPTION BUSTING - Challenge vague terms:
| Term | Ask |
|------|-----|
| "login" | Magic link? Password? OAuth? 2FA? Passkeys? |
| "save" | Auto-save? Manual? Cloud? Local? |
| "notification" | Email? Push? In-app? SMS? |
| "user" | Admin? Member? Guest? Anonymous? |

EXIT CRITERIA (all must be met):
[ ] 3+ alternatives explored with trade-offs
[ ] ONE approach explicitly chosen with reasoning
[ ] Trade-offs documented and accepted

When all criteria are met, say: "GATE 2 COMPLETE. Moving to Technical Design."
""",

    "technical": """## GATE 3: Technical Design

Your objective: Lock down architecture BEFORE defining features.

WHY THIS MATTERS: Without shared technical foundation, different features might:
- Invent conflicting data models
- Use different patterns for similar problems
- Create incompatible APIs

YOU MUST HELP THE USER DEFINE:

1. **Data Models** - What entities exist? What fields do they have?
   Example: User: { id, email, name, created_at }

2. **API Design** - What endpoints? What request/response shapes?
   Example: POST /api/auth/login -> { token, user }

3. **Component/Page Structure** - What pages? How organized?
   Example: /app/dashboard/page.tsx

4. **Key Patterns** - Auth strategy? State management? Validation?
   Example: JWT in httpOnly cookie, React Query, Zod

EXIT CRITERIA (all must be met):
[ ] Data models defined for ALL entities mentioned
[ ] API endpoints listed with request/response shapes
[ ] Component/page structure outlined
[ ] Key patterns decided (auth, state, validation)

After collecting this information, generate specs/tech_plan.md with the full architecture.

When complete, say: "GATE 3 COMPLETE. Technical plan saved. Moving to Edge Cases."
""",

    "edges": """## GATE 4: Edge Case Interrogation

Your objective: Force the user to think about failure modes.

THE RULE OF 3: Every feature needs 3+ edge cases from these categories:

| Category | Examples |
|----------|----------|
| **Input** | Empty, too long, malformed, special chars |
| **State** | Logged out, no data, stale data, too much data |
| **Network** | Offline, timeout, rate limited, API down |
| **Concurrency** | Multi-user edit, multi-device, interrupted |
| **Security** | Wrong user, invalid token, guessed URL |

For EACH feature being discussed:
1. Ask: "What happens when [edge case]?"
2. Demand specific behavior, not "handle gracefully"
3. Require specific error messages

EXIT CRITERIA (all must be met):
[ ] Every feature has 3+ documented edge cases
[ ] User specified EXACT behavior for each edge case
[ ] Error messages are defined (actual strings)

When all features have adequate edge cases, say: "GATE 4 COMPLETE. Moving to Synthesis."
""",

    "synthesis": """## GATE 5: Synthesis

Your objective: Convert interrogation into machine-readable specs.

GRANULARITY ENFORCEMENT:
- Task takes >1 day to implement? MUST decompose into smaller features
- Description contains "and then..."? SPLIT into separate features
- Each feature has exactly ONE acceptance criterion

FALSIFIABILITY RATCHET:
BAD: "User can log in"
BAD: "User can view their dashboard"
GOOD: "After valid login, user sees dashboard with their name within 2s"

For each feature, you MUST have:
- id: kebab-case identifier (e.g., "auth-001-email-login")
- description: Falsifiable statement of what happens
- acceptance_criteria: Single testable assertion
- edge_cases: Array of {id, description, expected_behavior} - MINIMUM 3
- priority: Integer (1 = highest)

COMPLETION CHECK - Display this before generating:
```
SPECIFICATION COMPLETION CHECK
==============================
Problem Definition:    [✓/✗]
User Personas:         [count] defined
Solution Alternatives: [count] explored
Chosen Approach:       [name]
Technical Plan:        [✓/✗]

Features: [count]
  [id]: [✓/✗] [edge_count] edge cases, [falsifiable?]
  ...

BLOCKERS: [count]
```

Only generate features.json when ALL checks pass.

When ready, generate the JSON and say: "GATE 5 COMPLETE. Specification saved to specs/features.json"
"""
}


# =============================================================================
# Exit Criteria Checking
# =============================================================================

def check_gate1_criteria(state: SpecificationState) -> tuple[bool, list[str]]:
    """Check if Gate 1 exit criteria are met."""
    issues = []

    if not state.problem_statement:
        issues.append("Problem statement not captured")
    if not state.user_personas:
        issues.append("No user personas identified")

    return len(issues) == 0, issues


def check_gate2_criteria(state: SpecificationState) -> tuple[bool, list[str]]:
    """Check if Gate 2 exit criteria are met."""
    issues = []

    if len(state.solution_alternatives) < 3:
        issues.append(f"Only {len(state.solution_alternatives)} alternatives explored (need 3+)")
    if not state.chosen_approach:
        issues.append("No approach explicitly chosen")

    return len(issues) == 0, issues


def check_gate3_criteria(state: SpecificationState) -> tuple[bool, list[str]]:
    """Check if Gate 3 exit criteria are met."""
    issues = []

    if not state.tech_plan_generated:
        issues.append("Technical plan not generated")
    if not TECH_PLAN_PATH.exists():
        issues.append("specs/tech_plan.md does not exist")

    return len(issues) == 0, issues


def check_gate4_criteria(state: SpecificationState) -> tuple[bool, list[str]]:
    """Check if Gate 4 exit criteria are met."""
    issues = []

    if not state.features:
        issues.append("No features defined yet")
        return False, issues

    for feature in state.features:
        edge_cases = feature.get("edge_cases", [])
        if len(edge_cases) < 3:
            issues.append(f"Feature '{feature.get('id', '?')}' has only {len(edge_cases)} edge cases (need 3+)")

    return len(issues) == 0, issues


def check_gate5_criteria(state: SpecificationState) -> tuple[bool, list[str]]:
    """Check if Gate 5 (final) criteria are met."""
    issues = []

    # Check all previous gates
    g1_ok, g1_issues = check_gate1_criteria(state)
    g2_ok, g2_issues = check_gate2_criteria(state)
    g3_ok, g3_issues = check_gate3_criteria(state)
    g4_ok, g4_issues = check_gate4_criteria(state)

    if not g1_ok:
        issues.extend(g1_issues)
    if not g2_ok:
        issues.extend(g2_issues)
    if not g3_ok:
        issues.extend(g3_issues)
    if not g4_ok:
        issues.extend(g4_issues)

    # Additional synthesis checks
    for feature in state.features:
        if not feature.get("acceptance_criteria"):
            issues.append(f"Feature '{feature.get('id', '?')}' missing acceptance_criteria")

        desc = feature.get("description", "")
        # Check for falsifiability (very basic heuristic)
        vague_terms = ["can", "should", "will be able to", "allows"]
        if any(term in desc.lower() for term in vague_terms):
            issues.append(f"Feature '{feature.get('id', '?')}' description may not be falsifiable")

    return len(issues) == 0, issues


# =============================================================================
# Feature Synthesis
# =============================================================================

def synthesize_features(state: SpecificationState) -> dict:
    """Generate the final features.json structure."""
    features = []

    for feature in state.features:
        synthesized = {
            "id": feature.get("id", "unknown"),
            "description": feature.get("description", ""),
            "acceptance_criteria": feature.get("acceptance_criteria", ""),
            "edge_cases": feature.get("edge_cases", []),
            "priority": feature.get("priority", 99),
            "status": "todo"
        }

        # Add optional fields if present
        if feature.get("file_scope"):
            synthesized["file_scope"] = feature["file_scope"]
        if feature.get("depends_on"):
            synthesized["depends_on"] = feature["depends_on"]

        features.append(synthesized)

    # Sort by priority
    features.sort(key=lambda f: f.get("priority", 99))

    return {"features": features}


def save_features(state: SpecificationState) -> None:
    """Save features.json."""
    data = synthesize_features(state)
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    FEATURES_PATH.write_text(json.dumps(data, indent=2))
    print(f"\nSaved {len(data['features'])} features to {FEATURES_PATH}")


def save_tech_plan(content: str) -> None:
    """Save tech_plan.md."""
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    TECH_PLAN_PATH.write_text(content)
    print(f"\nSaved technical plan to {TECH_PLAN_PATH}")


# =============================================================================
# Response Parsing
# =============================================================================

def extract_gate_completion(response: str) -> Optional[str]:
    """Check if response indicates gate completion.

    Only matches actual declarations, not future tense ("I will declare GATE X COMPLETE").
    The pattern must appear at the start of a line or after punctuation.
    """
    patterns = [
        (r"(?:^|[.!?\n])\s*GATE 1 COMPLETE", "solution"),
        (r"(?:^|[.!?\n])\s*GATE 2 COMPLETE", "technical"),
        (r"(?:^|[.!?\n])\s*GATE 3 COMPLETE", "edges"),
        (r"(?:^|[.!?\n])\s*GATE 4 COMPLETE", "synthesis"),
        (r"(?:^|[.!?\n])\s*GATE 5 COMPLETE", "complete"),
    ]

    for pattern, next_phase in patterns:
        if re.search(pattern, response, re.IGNORECASE | re.MULTILINE):
            return next_phase

    return None


def extract_tech_plan(response: str) -> Optional[str]:
    """Extract tech plan markdown from response."""
    # Look for markdown code blocks or # Technical Architecture header
    match = re.search(r"```markdown\n(.*?)```", response, re.DOTALL)
    if match:
        return match.group(1).strip()

    match = re.search(r"(# Technical Architecture.*?)(?=\n\n##|\Z)", response, re.DOTALL)
    if match:
        return match.group(1).strip()

    return None


def extract_features_json(response: str) -> Optional[list]:
    """Extract features array from response."""
    # Look for JSON in code blocks
    match = re.search(r"```json\n(.*?)```", response, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict) and "features" in data:
                return data["features"]
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    return None


def extract_state_updates(response: str, state: SpecificationState) -> None:
    """Extract and apply state updates from response."""

    # Extract problem statement
    match = re.search(r"PROBLEM STATEMENT:\s*(.+?)(?:\n\n|\Z)", response, re.DOTALL | re.IGNORECASE)
    if match:
        state.problem_statement = match.group(1).strip()

    # Extract user personas
    match = re.search(r"USER PERSONA[S]?:\s*(.+?)(?:\n\n|\Z)", response, re.DOTALL | re.IGNORECASE)
    if match:
        # Simple extraction - could be more sophisticated
        personas_text = match.group(1).strip()
        if personas_text and not state.user_personas:
            state.user_personas.append({"description": personas_text})

    # Extract chosen approach
    match = re.search(r"CHOSEN APPROACH:\s*(.+?)(?:\n\n|\Z)", response, re.DOTALL | re.IGNORECASE)
    if match:
        state.chosen_approach = {"description": match.group(1).strip()}

    # Extract solution alternatives
    match = re.search(r"ALTERNATIVES?:\s*(.+?)(?:\n\n|\Z)", response, re.DOTALL | re.IGNORECASE)
    if match:
        alts_text = match.group(1).strip()
        if alts_text and len(state.solution_alternatives) < 3:
            state.solution_alternatives.append({"description": alts_text})

    # Extract tech plan
    tech_plan = extract_tech_plan(response)
    if tech_plan:
        save_tech_plan(tech_plan)
        state.tech_plan_generated = True

    # Extract features
    features = extract_features_json(response)
    if features:
        state.features = features


# =============================================================================
# REPL Loop
# =============================================================================

def build_context(state: SpecificationState) -> str:
    """Build context message for Claude."""
    context_parts = [f"Product idea: {state.product_idea}"]

    if state.problem_statement:
        context_parts.append(f"\nProblem statement: {state.problem_statement}")

    if state.user_personas:
        context_parts.append(f"\nUser personas: {json.dumps(state.user_personas)}")

    if state.solution_alternatives:
        context_parts.append(f"\nAlternatives explored: {len(state.solution_alternatives)}")

    if state.chosen_approach:
        context_parts.append(f"\nChosen approach: {state.chosen_approach.get('description', 'unknown')}")

    if state.tech_plan_generated:
        context_parts.append("\nTechnical plan: Generated")

    if state.features:
        context_parts.append(f"\nFeatures defined: {len(state.features)}")
        for f in state.features:
            edge_count = len(f.get("edge_cases", []))
            context_parts.append(f"  - {f.get('id', '?')}: {edge_count} edge cases")

    # Include health report context if available (brownfield projects)
    if HEALTH_REPORT_PATH.exists():
        context_parts.append(_summarize_health_for_architect())

    # Include working memory context (Q&A, decisions, understanding)
    if MEMORY_AVAILABLE:
        memory_context = get_memory_context("architect")
        if memory_context:
            context_parts.append(memory_context)

    return "\n".join(context_parts)


def _extract_health_context() -> str:
    """Extract relevant context from health report for the Architect (truncation fallback)."""
    try:
        content = HEALTH_REPORT_PATH.read_text()
        context_parts = ["\n## Known Issues from Health Report"]

        # Extract documented issues section
        if "## Documented Issues" in content:
            start = content.find("## Documented Issues")
            end = content.find("\n## ", start + 1)
            if end == -1:
                end = len(content)
            issues_section = content[start:end].strip()
            # Limit to first 10 lines
            lines = issues_section.split("\n")[:12]
            context_parts.append("\n".join(lines))

        # Extract code annotations summary
        if "## Code Annotations" in content:
            start = content.find("## Code Annotations")
            end = content.find("\n## ", start + 1)
            if end == -1:
                end = len(content)
            annotations_section = content[start:end].strip()
            lines = annotations_section.split("\n")[:12]
            context_parts.append("\n".join(lines))

        # Extract external services
        if "## External Services" in content:
            start = content.find("## External Services")
            end = content.find("\n## ", start + 1)
            if end == -1:
                end = len(content)
            services_section = content[start:end].strip()
            lines = services_section.split("\n")[:10]
            context_parts.append("\n".join(lines))

        if len(context_parts) > 1:
            return "\n".join(context_parts)

    except IOError:
        pass

    return ""


def _summarize_health_for_architect() -> str:
    """
    Generate executive summary of health report for Architect context.

    Uses Claude to intelligently summarize the health report, focusing on
    critical issues that affect architecture decisions. Falls back to
    truncation if summarization fails.
    """
    if not HEALTH_REPORT_PATH.exists():
        return ""

    try:
        content = HEALTH_REPORT_PATH.read_text()

        # If report is small, just return it (no need to summarize)
        if len(content) < 2000:
            return f"\n## Health Report (Brownfield)\n{content}"

        # Use Claude to summarize
        summary_prompt = f"""Summarize this health report for an AI Architect planning a new feature.

Focus on:
- Critical blockers that affect architecture decisions
- Security concerns (SECURITY, HACK annotations)
- External service dependencies that need consideration
- Test coverage gaps
- Any patterns marked as LEGACY or ANTIPATTERN

Be concise (10 bullet points max). Highlight what the Architect MUST know before designing.

HEALTH REPORT:
{content[:8000]}

EXECUTIVE SUMMARY (bullet points):"""

        summary = call_claude_cli(summary_prompt)
        return f"\n## Health Report Summary (Brownfield)\n{summary}"

    except Exception as e:
        # Fallback to truncation if CLI fails
        print(f"(Health summary failed: {e}, using truncation)")
        return _extract_health_context()


def _extract_questions_from_message(content: str) -> list[str]:
    """Extract questions from an assistant message for Q&A tracking."""
    questions = []
    # Find sentences ending with ?
    potential = re.findall(r'([^.!?\n]+\?)', content)
    for q in potential:
        q = q.strip()
        # Skip short/rhetorical questions
        if len(q) < 15:
            continue
        if any(meta in q.lower() for meta in ["does that make sense", "shall i", "should i", "ready to"]):
            continue
        questions.append(q)
    return questions


def run_repl(state: SpecificationState) -> None:
    """Run the interactive REPL loop using Claude CLI."""

    print("\n" + "=" * 60)
    print("SOCRATIC ARCHITECT - Specification System")
    print("=" * 60)
    print(f"\nCurrent phase: {state.phase.upper()}")
    print("Type 'quit' to save and exit, 'status' to see progress.\n")

    # Track pending questions from the last assistant message
    pending_questions: list[str] = []

    while state.phase != "complete":
        # Get user input
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nSaving session...")
            save_state(state)
            return

        if not user_input:
            continue

        if user_input.lower() == "quit":
            print("\nSaving session...")
            save_state(state)
            print("Session saved. Run 'python harness/architect.py resume' to continue.")
            return

        if user_input.lower() == "status":
            print_status(state)
            continue

        # Record Q&A from previous exchange if we had pending questions
        if MEMORY_AVAILABLE and pending_questions:
            # Take the most significant question (last one)
            main_question = pending_questions[-1]
            record_answer("architect", main_question, user_input[:500])
            pending_questions = []

        # Build system prompt
        system = SYSTEM_PROMPT.format(phase=state.phase.upper())
        if state.phase in GATE_PROMPTS:
            system += "\n\n" + GATE_PROMPTS[state.phase]

        # Add context
        context = build_context(state)
        full_system = system + f"\n\n## Current Context\n{context}"

        # Format conversation for CLI
        prompt = format_conversation(full_system, state.messages, user_input)

        try:
            # Call Claude CLI
            print("\n(Thinking...)")
            assistant_message = call_claude_cli(prompt)

            # Add to history
            state.messages.append({"role": "user", "content": user_input})
            state.messages.append({"role": "assistant", "content": assistant_message})

            # Context window management - summarize if too long
            if len(state.messages) > SUMMARY_TRIGGER_COUNT:
                state.messages = summarize_history(state.messages)

            # Print response
            print(f"\nArchitect: {assistant_message}")

            # Extract questions for Q&A tracking
            if MEMORY_AVAILABLE:
                pending_questions = _extract_questions_from_message(assistant_message)

            # Extract state updates
            extract_state_updates(assistant_message, state)

            # Check for gate completion
            next_phase = extract_gate_completion(assistant_message)
            if next_phase:
                # Gate 5 validation: Don't complete unless features were actually generated
                if next_phase == "complete":
                    features = extract_features_json(assistant_message)
                    if not features or len(features) == 0:
                        print("\n[BLOCKED] Cannot complete: No features.json generated in response.")
                        print("The architect must output a ```json block with the features array.")
                        print("Staying in synthesis phase.\n")
                        next_phase = None  # Block advancement

            # Review Board: Trigger adversarial review after Gate 3 (Technical Plan)
            if next_phase and state.phase == "technical" and next_phase == "edges":
                if REVIEW_BOARD_AVAILABLE and should_review("architect"):
                    print(f"\n{'=' * 40}")
                    print("REVIEW BOARD: Technical Plan Review")
                    print(f"{'=' * 40}")

                    # Get the tech plan content
                    tech_plan_content = ""
                    if TECH_PLAN_PATH.exists():
                        tech_plan_content = TECH_PLAN_PATH.read_text()

                    # Build context for reviewer
                    review_context = {
                        "product": state.product_idea,
                        "problem": state.problem_statement[:500] if state.problem_statement else "",
                        "approach": state.chosen_approach.get("description", "")[:300] if state.chosen_approach else "",
                    }

                    result = request_review(
                        stage="architect",
                        context=review_context,
                        output=tech_plan_content
                    )

                    if not result.approved:
                        # Feed reviewer feedback back into the conversation
                        print("\n[REVIEW BOARD] Review rejected. Addressing feedback...")
                        state.messages.append({
                            "role": "system",
                            "content": f"REVIEWER VETO:\n{result.feedback}\n\nAddress these concerns before proceeding to edge cases."
                        })
                        # Don't advance phase - stay in technical
                        save_state(state)
                        continue
                    else:
                        print("\n[REVIEW BOARD] Technical plan approved.")

            if next_phase:
                state.phase = next_phase
                print(f"\n{'=' * 40}")
                print(f"ADVANCING TO: {state.phase.upper()}")
                print(f"{'=' * 40}")

                # Update working memory with gate completion
                if MEMORY_AVAILABLE:
                    update_understanding("architect", "current_gate", state.phase)
                    record_decision(
                        "architect",
                        f"Gate completed: {next_phase}",
                        context=f"Product: {state.product_idea[:100]}",
                        options=["proceed", "stay"],
                        chosen="proceed",
                        rationale="Exit criteria met for previous gate"
                    )

                # Save features if complete
                if state.phase == "complete":
                    save_features(state)

            # Save state periodically
            save_state(state)

        except Exception as e:
            print(f"\nError: {e}")
            continue

    print("\n" + "=" * 60)
    print("SPECIFICATION COMPLETE")
    print("=" * 60)
    print(f"\nGenerated files:")
    print(f"  - {FEATURES_PATH}")
    if TECH_PLAN_PATH.exists():
        print(f"  - {TECH_PLAN_PATH}")
    print(f"\nNext step: python harness/coding/loop.py")


# =============================================================================
# Status & Audit
# =============================================================================

def print_status(state: SpecificationState) -> None:
    """Print current specification status.

    Checks multiple sources of truth:
    1. State object (from session.json)
    2. Filesystem (specs/*.md files)
    3. Working memory (if available)
    """
    print("\n" + "=" * 50)
    print("SPECIFICATION STATUS")
    print("=" * 50)

    print(f"\nPhase: {state.phase.upper()}")
    print(f"Product: {state.product_idea}")

    # Check filesystem for additional evidence
    specs_dir = Path("specs")
    has_problem_doc = any(specs_dir.glob("*problem*.md")) or any(specs_dir.glob("*Problem*.md"))
    has_solution_doc = any(specs_dir.glob("*solution*.md")) or any(specs_dir.glob("*Solution*.md"))
    has_tech_plan = TECH_PLAN_PATH.exists()
    has_features = FEATURES_PATH.exists()

    # Load features from file if state doesn't have them
    file_features = []
    if has_features:
        try:
            data = json.loads(FEATURES_PATH.read_text())
            if isinstance(data, dict) and "features" in data:
                file_features = [f for f in data["features"] if f.get("id") != "example-001"]
            elif isinstance(data, list):
                file_features = data
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    # Check working memory for additional context
    memory_context = ""
    if MEMORY_AVAILABLE:
        memory = read_memory("architect")
        memory_context = f"{len(memory.questions)} Q&A, {len(memory.decisions)} decisions recorded"

    # Gate 1 - also check for problem docs
    g1_ok = bool(state.problem_statement) or has_problem_doc
    print(f"\nGate 1 (Problem):    {'OK' if g1_ok else 'INCOMPLETE'}")
    if state.problem_statement:
        print(f"  Problem: {state.problem_statement[:50]}...")
    elif has_problem_doc:
        print(f"  Problem: Documented in specs/")
    if state.user_personas:
        print(f"  Personas: {len(state.user_personas)} defined")
    if not g1_ok:
        print("    - Problem statement not captured in state")

    # Gate 2 - also check for solution docs
    g2_ok = bool(state.chosen_approach) or has_solution_doc or len(state.solution_alternatives) >= 3
    print(f"\nGate 2 (Solution):   {'OK' if g2_ok else 'INCOMPLETE'}")
    if state.solution_alternatives:
        print(f"  Alternatives: {len(state.solution_alternatives)} explored")
    if state.chosen_approach:
        print(f"  Chosen: {state.chosen_approach.get('description', 'unknown')[:40]}...")
    elif has_solution_doc:
        print(f"  Solution: Documented in specs/")
    if not g2_ok:
        print("    - Solution approach not captured in state")

    # Gate 3 - check tech_plan.md
    g3_ok = state.tech_plan_generated or has_tech_plan
    print(f"\nGate 3 (Technical):  {'OK' if g3_ok else 'INCOMPLETE'}")
    if has_tech_plan:
        print(f"  Tech plan: {TECH_PLAN_PATH}")
    elif state.tech_plan_generated:
        print(f"  Tech plan: Generated (in state)")
    else:
        print(f"  Tech plan: Not generated")
        print(f"    - {TECH_PLAN_PATH} does not exist")

    # Gate 4 - check features (from state or file)
    features = state.features if state.features else file_features
    g4_ok = len(features) > 0
    print(f"\nGate 4 (Edge Cases): {'OK' if g4_ok else 'INCOMPLETE'}")
    print(f"  Features: {len(features)}")
    if features:
        for feature in features[:5]:  # Show first 5
            edge_count = len(feature.get("edge_cases", []))
            print(f"    - {feature.get('id', '?')}: {feature.get('description', '')[:40]}...")
        if len(features) > 5:
            print(f"    ... and {len(features) - 5} more")
    else:
        print("    - No features defined yet")

    # Gate 5
    g5_ok = g1_ok and g2_ok and g3_ok and g4_ok
    print(f"\nGate 5 (Synthesis):  {'OK' if g5_ok else 'INCOMPLETE'}")
    if not g5_ok:
        if not g1_ok:
            print("    - Gate 1 incomplete")
        if not g2_ok:
            print("    - Gate 2 incomplete")
        if not g3_ok:
            print("    - Gate 3 incomplete")
        if not g4_ok:
            print("    - Gate 4 incomplete")

    # Working memory status
    if memory_context:
        print(f"\nWorking Memory: {memory_context}")

    print("\n" + "=" * 50)


def audit_spec() -> int:
    """Audit existing specification for completeness."""
    state = load_state()
    if not state:
        print("No session found. Run 'python harness/architect.py new \"idea\"' first.")
        return 1

    print_status(state)

    # Return exit code based on completeness
    g5_ok, _ = check_gate5_criteria(state)
    return 0 if g5_ok else 1


# =============================================================================
# CLI Entry Points
# =============================================================================

def cmd_new(product_idea: str) -> int:
    """Start a new specification session."""
    # Check for existing session
    existing = load_state()
    if existing:
        print(f"Existing session found for: {existing.product_idea}")
        response = input("Overwrite? (y/N): ").strip().lower()
        if response != "y":
            print("Aborted. Use 'resume' to continue existing session.")
            return 0

    # Phase 0: Run Doctor health check for brownfield projects
    if Path("src").exists() or Path("package.json").exists() or Path("requirements.txt").exists():
        print("Existing codebase detected.")

        # Run health diagnosis if not already done
        if not HEALTH_REPORT_PATH.exists():
            print("\nRunning health diagnosis (Phase 0)...")
            try:
                from doctor import cmd_diagnose
                cmd_diagnose()
            except ImportError:
                print("Warning: doctor module not found, skipping health check")
            except Exception as e:
                print(f"Warning: Health check failed: {e}")

        # Check health status and soft-block if critical
        if HEALTH_REPORT_PATH.exists():
            health_content = HEALTH_REPORT_PATH.read_text()
            if "CRITICAL" in health_content:
                print("\n" + "=" * 60)
                print("PROJECT HEALTH: CRITICAL")
                print("=" * 60)
                print("\nThe health report indicates critical issues that should be")
                print("addressed before adding new features.")
                print("\nRecommended: Run 'python harness/doctor.py stabilize' first.")
                print("")
                response = input("Proceed anyway? (y/N): ").strip().lower()
                if response != "y":
                    print("\nRun 'python harness/doctor.py stabilize' to generate fix tasks.")
                    return 0
                print("\nProceeding with caution...")

        # Extract patterns
        print("\nExtracting codebase patterns...")
        try:
            from archaeologist import run_extraction
            run_extraction()
        except ImportError:
            print("Warning: archaeologist module not found, skipping pattern extraction")
        except Exception as e:
            print(f"Warning: Pattern extraction failed: {e}")

    # Create new state
    state = SpecificationState(
        phase="problem",
        product_idea=product_idea,
        created_at=datetime.now(timezone.utc).isoformat()
    )
    save_state(state)

    # Add initial message
    state.messages.append({
        "role": "user",
        "content": f"I want to build: {product_idea}"
    })

    run_repl(state)
    return 0


def cmd_resume() -> int:
    """Resume an existing specification session."""
    state = load_state()
    if not state:
        print("No session found. Run 'python harness/architect.py new \"idea\"' first.")
        return 1

    print(f"Resuming session for: {state.product_idea}")
    print(f"Current phase: {state.phase}")

    run_repl(state)
    return 0


def cmd_add_feature(description: str) -> int:
    """Add a feature to existing specification (re-enters edge case gate)."""
    state = load_state()
    if not state:
        print("No session found. Run 'python harness/architect.py new \"idea\"' first.")
        return 1

    if state.phase not in ["edges", "synthesis", "complete"]:
        print(f"Cannot add features in phase '{state.phase}'. Complete gates 1-3 first.")
        return 1

    # Reset to edge case gate with new feature context
    state.phase = "edges"
    state.messages.append({
        "role": "user",
        "content": f"I want to add a new feature: {description}"
    })
    save_state(state)

    run_repl(state)
    return 0


def cmd_scan() -> int:
    """Scan existing codebase for patterns."""
    try:
        from archaeologist import run_extraction
        return run_extraction()
    except ImportError:
        print("ERROR: archaeologist module not found.")
        print("Create harness/archaeologist.py first.")
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Socratic Architect - Adversarial Specification System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python harness/architect.py new "A todo app with user authentication"
  python harness/architect.py resume
  python harness/architect.py audit
  python harness/architect.py add-feature "Email notifications"
  python harness/architect.py scan
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # new command
    new_parser = subparsers.add_parser("new", help="Start new specification session")
    new_parser.add_argument("idea", help="Product idea to specify")

    # resume command
    subparsers.add_parser("resume", help="Resume existing session")

    # audit command
    subparsers.add_parser("audit", help="Audit specification completeness")

    # add-feature command
    add_parser = subparsers.add_parser("add-feature", help="Add feature to existing spec")
    add_parser.add_argument("description", help="Feature description")

    # scan command
    subparsers.add_parser("scan", help="Scan existing codebase for patterns")

    args = parser.parse_args()

    if args.command == "new":
        return cmd_new(args.idea)
    elif args.command == "resume":
        return cmd_resume()
    elif args.command == "audit":
        return audit_spec()
    elif args.command == "add-feature":
        return cmd_add_feature(args.description)
    elif args.command == "scan":
        return cmd_scan()
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
