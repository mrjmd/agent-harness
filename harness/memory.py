#!/usr/bin/env python3
"""
Working Memory Module

Persistent memory system that survives conversation summarization.
Each component maintains its own memory file with:
- Answered questions (DO NOT RE-ASK)
- Key decisions made
- Current understanding
- Open questions

Usage:
    from memory import read_memory, record_answer, get_memory_context

    # At session start
    memory = read_memory("architect")

    # Check before asking a question
    existing = check_before_asking("architect", "Who is the target user?")
    if existing:
        # Don't ask again, use cached answer

    # After user answers
    record_answer("architect", "Who is the target user?", user_response)

    # Get context for prompt injection
    context = get_memory_context("architect")
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Memory storage location
MEMORY_DIR = Path("specs/memory")
INDEX_PATH = MEMORY_DIR / "index.json"


@dataclass
class QAEntry:
    """A question-answer pair."""
    question: str
    answer: str
    timestamp: str
    confidence: str = "high"  # high, medium, low


@dataclass
class Decision:
    """A recorded decision."""
    summary: str
    context: str
    options: list[str]
    chosen: str
    rationale: str
    timestamp: str


@dataclass
class WorkingMemory:
    """
    Persistent working memory for a component.

    Stores Q&A pairs, decisions, and understanding that survives
    conversation summarization.
    """
    component: str
    questions: dict[str, QAEntry] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    understanding: dict[str, str] = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)
    session_id: str = ""
    created_at: str = ""
    last_updated: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.last_updated:
            self.last_updated = self.created_at
        if not self.session_id:
            self.session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    @property
    def path(self) -> Path:
        """Path to this component's memory file."""
        return MEMORY_DIR / f"{self.component}.md"

    def save(self) -> None:
        """Persist memory to disk as markdown."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self.last_updated = datetime.now(timezone.utc).isoformat()

        lines = [
            f"# Working Memory: {self.component.title()}",
            "",
            f"> Session: {self.session_id}",
            f"> Last updated: {self.last_updated}",
            "",
        ]

        # Answered Questions
        lines.append("## Answered Questions")
        lines.append("<!-- DO NOT RE-ASK these questions -->")
        lines.append("")

        if self.questions:
            for q, entry in self.questions.items():
                lines.append(f"### Q: {entry.question}")
                lines.append(f"**Answer:** {entry.answer}")
                lines.append(f"**When:** {entry.timestamp}")
                lines.append(f"**Confidence:** {entry.confidence}")
                lines.append("")
        else:
            lines.append("_No questions answered yet._")
            lines.append("")

        # Key Decisions
        lines.append("## Key Decisions")
        lines.append("")

        if self.decisions:
            for i, d in enumerate(self.decisions, 1):
                lines.append(f"### D{i}: {d.summary}")
                lines.append(f"- **Context:** {d.context}")
                lines.append(f"- **Options:** {', '.join(d.options)}")
                lines.append(f"- **Chosen:** {d.chosen}")
                lines.append(f"- **Rationale:** {d.rationale}")
                lines.append(f"- **When:** {d.timestamp}")
                lines.append("")
        else:
            lines.append("_No decisions recorded yet._")
            lines.append("")

        # Understanding
        lines.append("## Understanding")
        lines.append("")

        if self.understanding:
            for key, value in self.understanding.items():
                # Truncate long values for readability
                display_value = value if len(value) < 500 else value[:500] + "..."
                lines.append(f"- **{key}:** {display_value}")
            lines.append("")
        else:
            lines.append("_No understanding captured yet._")
            lines.append("")

        # Open Questions
        lines.append("## Open Questions")
        lines.append("")

        if self.open_questions:
            for q in self.open_questions:
                lines.append(f"- [ ] {q}")
            lines.append("")
        else:
            lines.append("_No open questions._")
            lines.append("")

        self.path.write_text("\n".join(lines))
        self._update_index()

    def _update_index(self) -> None:
        """Update the central index file."""
        index = {}
        if INDEX_PATH.exists():
            try:
                index = json.loads(INDEX_PATH.read_text())
            except json.JSONDecodeError:
                index = {}

        index[self.component] = {
            "path": str(self.path),
            "session_id": self.session_id,
            "last_updated": self.last_updated,
            "question_count": len(self.questions),
            "decision_count": len(self.decisions),
        }

        INDEX_PATH.write_text(json.dumps(index, indent=2))


def _parse_memory_file(path: Path) -> WorkingMemory:
    """Parse a markdown memory file back into a WorkingMemory object."""
    if not path.exists():
        component = path.stem
        return WorkingMemory(component=component)

    content = path.read_text()
    component = path.stem
    memory = WorkingMemory(component=component)

    # Parse session info from header
    session_match = re.search(r"> Session: (.+)", content)
    if session_match:
        memory.session_id = session_match.group(1).strip()

    updated_match = re.search(r"> Last updated: (.+)", content)
    if updated_match:
        memory.last_updated = updated_match.group(1).strip()

    # Parse Answered Questions section
    qa_section = re.search(
        r"## Answered Questions.*?(?=## Key Decisions|$)",
        content,
        re.DOTALL
    )
    if qa_section:
        qa_text = qa_section.group(0)
        # Find all Q&A blocks
        qa_blocks = re.findall(
            r"### Q: (.+?)\n\*\*Answer:\*\* (.+?)\n\*\*When:\*\* (.+?)\n\*\*Confidence:\*\* (.+?)(?=\n\n|\n###|$)",
            qa_text,
            re.DOTALL
        )
        for question, answer, timestamp, confidence in qa_blocks:
            memory.questions[question.strip()] = QAEntry(
                question=question.strip(),
                answer=answer.strip(),
                timestamp=timestamp.strip(),
                confidence=confidence.strip()
            )

    # Parse Key Decisions section
    decisions_section = re.search(
        r"## Key Decisions.*?(?=## Understanding|$)",
        content,
        re.DOTALL
    )
    if decisions_section:
        decisions_text = decisions_section.group(0)
        # Find all decision blocks
        decision_blocks = re.findall(
            r"### D\d+: (.+?)\n- \*\*Context:\*\* (.+?)\n- \*\*Options:\*\* (.+?)\n- \*\*Chosen:\*\* (.+?)\n- \*\*Rationale:\*\* (.+?)\n- \*\*When:\*\* (.+?)(?=\n\n|\n###|$)",
            decisions_text,
            re.DOTALL
        )
        for summary, context, options, chosen, rationale, timestamp in decision_blocks:
            memory.decisions.append(Decision(
                summary=summary.strip(),
                context=context.strip(),
                options=[o.strip() for o in options.split(",")],
                chosen=chosen.strip(),
                rationale=rationale.strip(),
                timestamp=timestamp.strip()
            ))

    # Parse Understanding section
    understanding_section = re.search(
        r"## Understanding.*?(?=## Open Questions|$)",
        content,
        re.DOTALL
    )
    if understanding_section:
        understanding_text = understanding_section.group(0)
        # Find all key-value pairs
        kv_pairs = re.findall(
            r"- \*\*(.+?):\*\* (.+?)(?=\n- \*\*|\n\n|$)",
            understanding_text,
            re.DOTALL
        )
        for key, value in kv_pairs:
            memory.understanding[key.strip()] = value.strip()

    # Parse Open Questions section
    open_section = re.search(r"## Open Questions.*?$", content, re.DOTALL)
    if open_section:
        open_text = open_section.group(0)
        questions = re.findall(r"- \[ \] (.+?)(?=\n|$)", open_text)
        memory.open_questions = [q.strip() for q in questions]

    return memory


def read_memory(component: str) -> WorkingMemory:
    """
    Load working memory for a component.

    Args:
        component: Component name (architect, doctor, archaeologist, etc.)

    Returns:
        WorkingMemory instance (empty if no existing memory)
    """
    path = MEMORY_DIR / f"{component}.md"
    return _parse_memory_file(path)


def check_before_asking(component: str, question: str, threshold: float = 0.6) -> Optional[str]:
    """
    Check if a question has already been answered.

    Uses fuzzy matching to detect semantically similar questions.

    Args:
        component: Component name
        question: The question to check
        threshold: Word overlap threshold (0.0-1.0) for fuzzy matching

    Returns:
        The cached answer if found, None otherwise
    """
    memory = read_memory(component)

    # Normalize the question
    question_lower = question.lower().strip()
    question_words = set(re.findall(r'\w+', question_lower))

    # Exact match first
    for stored_q, entry in memory.questions.items():
        if stored_q.lower().strip() == question_lower:
            return entry.answer

    # Fuzzy match
    best_match = None
    best_score = 0.0

    for stored_q, entry in memory.questions.items():
        stored_words = set(re.findall(r'\w+', stored_q.lower()))

        if not question_words or not stored_words:
            continue

        # Jaccard similarity
        intersection = len(question_words & stored_words)
        union = len(question_words | stored_words)
        score = intersection / union if union > 0 else 0

        if score > best_score and score >= threshold:
            best_score = score
            best_match = entry.answer

    return best_match


def record_answer(
    component: str,
    question: str,
    answer: str,
    confidence: str = "high"
) -> None:
    """
    Record an answer to a question in working memory.

    MUST be called immediately after receiving a user answer.

    Args:
        component: Component name
        question: The question that was asked
        answer: The user's answer
        confidence: How confident we are in the answer (high/medium/low)
    """
    memory = read_memory(component)
    memory.questions[question] = QAEntry(
        question=question,
        answer=answer,
        timestamp=datetime.now(timezone.utc).isoformat(),
        confidence=confidence
    )
    memory.save()


def record_decision(
    component: str,
    summary: str,
    context: str,
    options: list[str],
    chosen: str,
    rationale: str
) -> None:
    """
    Record a decision in working memory.

    Args:
        component: Component name
        summary: Brief description of the decision
        context: Why this decision was needed
        options: List of alternatives considered
        chosen: The selected option
        rationale: Why this option was chosen
    """
    memory = read_memory(component)
    memory.decisions.append(Decision(
        summary=summary,
        context=context,
        options=options,
        chosen=chosen,
        rationale=rationale,
        timestamp=datetime.now(timezone.utc).isoformat()
    ))
    memory.save()


def update_understanding(component: str, key: str, value: str) -> None:
    """
    Update the understanding section of working memory.

    Args:
        component: Component name
        key: The aspect being understood (e.g., "target_user", "tech_stack")
        value: The current understanding
    """
    memory = read_memory(component)
    memory.understanding[key] = value
    memory.save()


def add_open_question(component: str, question: str) -> None:
    """
    Add an open question that still needs to be answered.

    Args:
        component: Component name
        question: The question to track
    """
    memory = read_memory(component)
    if question not in memory.open_questions:
        memory.open_questions.append(question)
        memory.save()


def resolve_open_question(component: str, question: str) -> None:
    """
    Mark an open question as resolved.

    Args:
        component: Component name
        question: The question that was resolved
    """
    memory = read_memory(component)
    memory.open_questions = [q for q in memory.open_questions if q != question]
    memory.save()


def get_memory_context(component: str, max_questions: int = 10, max_decisions: int = 5) -> str:
    """
    Get formatted memory context for injection into prompts.

    Returns a condensed version suitable for prompt injection that
    tells Claude what it already knows.

    Args:
        component: Component name
        max_questions: Maximum number of Q&A pairs to include
        max_decisions: Maximum number of decisions to include

    Returns:
        Formatted string for prompt injection
    """
    memory = read_memory(component)

    if not memory.questions and not memory.decisions and not memory.understanding:
        return ""

    lines = [
        "## Known Context (from previous interactions)",
        "",
        "IMPORTANT: The following information has already been gathered.",
        "DO NOT ask these questions again. Reference the answers below.",
        "",
    ]

    # Include answered questions
    if memory.questions:
        lines.append("### Previously Answered Questions:")
        for q, entry in list(memory.questions.items())[-max_questions:]:
            lines.append(f"- **Q:** {entry.question}")
            lines.append(f"  **A:** {entry.answer}")
        lines.append("")

    # Include key decisions
    if memory.decisions:
        lines.append("### Previous Decisions:")
        for d in memory.decisions[-max_decisions:]:
            lines.append(f"- **{d.summary}:** Chose '{d.chosen}' because: {d.rationale[:100]}")
        lines.append("")

    # Include understanding
    if memory.understanding:
        lines.append("### Current Understanding:")
        for key, value in memory.understanding.items():
            # Truncate long values
            display = value if len(value) < 200 else value[:200] + "..."
            lines.append(f"- **{key}:** {display}")
        lines.append("")

    # Include open questions
    if memory.open_questions:
        lines.append("### Still Need to Determine:")
        for q in memory.open_questions:
            lines.append(f"- {q}")
        lines.append("")

    return "\n".join(lines)


def extract_qa_pairs(messages: list[dict]) -> list[tuple[str, str]]:
    """
    Extract question-answer pairs from conversation history.

    Used before summarization to persist Q&A to memory.

    Args:
        messages: List of conversation messages with 'role' and 'content'

    Returns:
        List of (question, answer) tuples
    """
    pairs = []

    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue

        content = msg.get("content", "")

        # Find questions (sentences ending with ?)
        # More sophisticated: look for question patterns
        questions = re.findall(r'([^.!?\n]*\?)', content)

        if not questions:
            continue

        # Check if next message is user response
        if i + 1 >= len(messages):
            continue

        next_msg = messages[i + 1]
        if next_msg.get("role") != "user":
            continue

        answer = next_msg.get("content", "").strip()

        # Take the most significant question (usually the last one)
        # Filter out rhetorical questions
        for q in reversed(questions):
            q = q.strip()
            # Skip very short questions (likely rhetorical)
            if len(q) < 15:
                continue
            # Skip meta-questions
            if any(meta in q.lower() for meta in ["does that make sense", "shall i", "should i"]):
                continue

            pairs.append((q, answer[:500]))  # Truncate long answers
            break

    return pairs


def clear_memory(component: str) -> None:
    """
    Clear all memory for a component.

    Use with caution - this deletes all recorded knowledge.

    Args:
        component: Component name
    """
    path = MEMORY_DIR / f"{component}.md"
    if path.exists():
        path.unlink()

    # Update index
    if INDEX_PATH.exists():
        try:
            index = json.loads(INDEX_PATH.read_text())
            if component in index:
                del index[component]
                INDEX_PATH.write_text(json.dumps(index, indent=2))
        except json.JSONDecodeError:
            pass


# Self-test
if __name__ == "__main__":
    print("Testing Working Memory module...")

    # Test basic operations
    test_component = "_test"

    # Clear any existing test memory
    clear_memory(test_component)

    # Record some Q&A
    record_answer(test_component, "What is the target user?", "Small business owners")
    record_answer(test_component, "What tech stack?", "Next.js + Python backend")

    # Record a decision
    record_decision(
        test_component,
        "Database choice",
        "Need to store user data and transactions",
        ["PostgreSQL", "MongoDB", "SQLite"],
        "PostgreSQL",
        "Relational data model fits our needs"
    )

    # Update understanding
    update_understanding(test_component, "core_problem", "Users need to track inventory")

    # Test check_before_asking
    existing = check_before_asking(test_component, "Who is the target user?")
    assert existing == "Small business owners", f"Expected match, got: {existing}"

    # Test fuzzy match (lower threshold for similar but not exact)
    fuzzy = check_before_asking(test_component, "What is the target user group?", threshold=0.5)
    assert fuzzy == "Small business owners", f"Fuzzy match failed, got: {fuzzy}"

    # Test get_memory_context
    context = get_memory_context(test_component)
    assert "Small business owners" in context
    assert "PostgreSQL" in context

    # Test extract_qa_pairs
    messages = [
        {"role": "assistant", "content": "What problem are you trying to solve?"},
        {"role": "user", "content": "I want to build an inventory tracker"},
        {"role": "assistant", "content": "Great! Who will use this system?"},
        {"role": "user", "content": "Warehouse managers"},
    ]
    pairs = extract_qa_pairs(messages)
    assert len(pairs) == 2

    # Verify file was created
    path = MEMORY_DIR / f"{test_component}.md"
    assert path.exists(), "Memory file not created"

    # Test parsing (round-trip)
    memory = read_memory(test_component)
    assert len(memory.questions) == 2
    assert len(memory.decisions) == 1
    assert "core_problem" in memory.understanding

    # Cleanup
    clear_memory(test_component)

    print("All tests passed!")
