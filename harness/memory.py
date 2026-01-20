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

# Import attempt journal for archive search (optional)
try:
    import sys
    sys.path.insert(0, str(Path(__file__).parent / "coding"))
    from attempt_journal import (
        load_archived_journal,
        list_archived_journals,
        AttemptJournal,
    )
    ATTEMPT_JOURNAL_AVAILABLE = True
except ImportError:
    ATTEMPT_JOURNAL_AVAILABLE = False

# Import learnings for search (optional)
LEARNINGS_PATH = Path("specs/learnings.json")


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


# =============================================================================
# Archive Search Functions
# =============================================================================

@dataclass
class HistoricalMatch:
    """A match from historical search."""
    source: str  # "learnings" or "archive"
    feature_id: str
    similarity_score: float
    lesson_or_resolution: str
    original_context: str = ""


def _compute_text_similarity(text1: str, text2: str) -> float:
    """
    Compute similarity between two text strings using word overlap.

    Returns a score between 0 and 1.
    """
    if not text1 or not text2:
        return 0.0

    # Extract words
    words1 = set(re.findall(r'\b[a-zA-Z]{3,}\b', text1.lower()))
    words2 = set(re.findall(r'\b[a-zA-Z]{3,}\b', text2.lower()))

    if not words1 or not words2:
        return 0.0

    # Jaccard similarity
    intersection = len(words1 & words2)
    union = len(words1 | words2)

    return intersection / union if union > 0 else 0.0


def search_learnings(
    query: str,
    max_results: int = 5,
    min_similarity: float = 0.2
) -> list[HistoricalMatch]:
    """
    Search learnings.json for relevant lessons.

    Args:
        query: Search query (error message, approach description, etc.)
        max_results: Maximum number of results to return
        min_similarity: Minimum similarity score to include

    Returns:
        List of HistoricalMatch objects sorted by similarity
    """
    if not LEARNINGS_PATH.exists():
        return []

    try:
        data = json.loads(LEARNINGS_PATH.read_text())
        learnings = data.get("learnings", [])
    except (json.JSONDecodeError, KeyError):
        return []

    matches = []

    for learning in learnings:
        lesson = learning.get("lesson", "")
        context = learning.get("context", "")
        feature_id = learning.get("feature_id", "unknown")

        # Combine lesson and context for matching
        combined_text = f"{lesson} {context}"
        similarity = _compute_text_similarity(query, combined_text)

        if similarity >= min_similarity:
            matches.append(HistoricalMatch(
                source="learnings",
                feature_id=feature_id,
                similarity_score=similarity,
                lesson_or_resolution=lesson,
                original_context=context
            ))

    # Sort by similarity and return top results
    matches.sort(key=lambda m: m.similarity_score, reverse=True)
    return matches[:max_results]


def search_archive(
    error_query: str = "",
    approach_query: str = "",
    max_results: int = 5,
    min_similarity: float = 0.2
) -> list[HistoricalMatch]:
    """
    Search archived attempt journals for similar problems.

    Args:
        error_query: Error message to search for
        approach_query: Approach description to search for
        max_results: Maximum number of results to return
        min_similarity: Minimum similarity score to include

    Returns:
        List of HistoricalMatch objects sorted by similarity
    """
    if not ATTEMPT_JOURNAL_AVAILABLE:
        return []

    matches = []

    for feature_id in list_archived_journals():
        journal = load_archived_journal(feature_id)
        if not journal:
            continue

        # Search through attempts
        for attempt in journal.attempts:
            # Match against error signatures
            if error_query and attempt.verification:
                error_sig = attempt.verification.error_signature or ""
                similarity = _compute_text_similarity(error_query, error_sig)

                if similarity >= min_similarity:
                    # If this error was eventually resolved, find the resolution
                    resolution = _find_resolution_for_error(journal, attempt)

                    matches.append(HistoricalMatch(
                        source="archive",
                        feature_id=feature_id,
                        similarity_score=similarity,
                        lesson_or_resolution=resolution or f"Error occurred in feature {feature_id}",
                        original_context=error_sig[:200]
                    ))

            # Match against approach summaries
            if approach_query and attempt.approach_summary:
                similarity = _compute_text_similarity(approach_query, attempt.approach_summary)

                if similarity >= min_similarity:
                    # Check if this approach worked
                    worked = attempt.verification.passed if attempt.verification else False

                    matches.append(HistoricalMatch(
                        source="archive",
                        feature_id=feature_id,
                        similarity_score=similarity,
                        lesson_or_resolution=f"Approach {'succeeded' if worked else 'failed'}: {attempt.approach_summary[:100]}",
                        original_context=f"Feature: {feature_id}, Attempt: {attempt.attempt_num}"
                    ))

    # Sort by similarity and return top results
    matches.sort(key=lambda m: m.similarity_score, reverse=True)
    return matches[:max_results]


def _find_resolution_for_error(journal: 'AttemptJournal', error_attempt) -> Optional[str]:
    """Find how an error was eventually resolved in a journal."""
    error_hash = error_attempt.verification.error_hash if error_attempt.verification else None

    if not error_hash:
        return None

    # Look for a passing attempt after this error
    for attempt in journal.attempts:
        if attempt.attempt_num > error_attempt.attempt_num:
            if attempt.verification and attempt.verification.passed:
                return f"Resolved by: {attempt.approach_summary[:150]}"

    # If feature completed successfully, the last approach worked
    if journal.final_status == "passing" and journal.attempts:
        last = journal.attempts[-1]
        if last.approach_summary:
            return f"Eventually resolved by: {last.approach_summary[:150]}"

    return None


def search_similar_problems(
    current_error: str = "",
    current_approach: str = "",
    search_learnings_flag: bool = True,
    search_archive_flag: bool = True,
    max_results: int = 5
) -> list[HistoricalMatch]:
    """
    Find similar problems from past experience.

    Searches both learnings.json and archived attempt journals.

    Args:
        current_error: Current error message to match
        current_approach: Current approach being tried
        search_learnings_flag: Whether to search learnings.json
        search_archive_flag: Whether to search archived journals
        max_results: Maximum total results to return

    Returns:
        List of HistoricalMatch objects with solutions/lessons
    """
    all_matches = []

    # Combine error and approach for comprehensive search
    combined_query = f"{current_error} {current_approach}".strip()

    if search_learnings_flag and combined_query:
        learning_matches = search_learnings(combined_query, max_results)
        all_matches.extend(learning_matches)

    if search_archive_flag:
        archive_matches = search_archive(
            error_query=current_error,
            approach_query=current_approach,
            max_results=max_results
        )
        all_matches.extend(archive_matches)

    # Deduplicate by lesson content
    seen_lessons = set()
    unique_matches = []
    for match in sorted(all_matches, key=lambda m: m.similarity_score, reverse=True):
        lesson_key = match.lesson_or_resolution[:100]
        if lesson_key not in seen_lessons:
            seen_lessons.add(lesson_key)
            unique_matches.append(match)

    return unique_matches[:max_results]


def format_historical_matches(matches: list[HistoricalMatch]) -> str:
    """
    Format historical matches for prompt injection.

    Args:
        matches: List of HistoricalMatch objects

    Returns:
        Formatted string for prompt injection
    """
    if not matches:
        return ""

    lines = ["## Similar Problems from Past Experience", ""]

    for i, match in enumerate(matches, 1):
        source_label = "Learning" if match.source == "learnings" else "Past Feature"
        lines.append(f"### {i}. {source_label} (similarity: {match.similarity_score:.2f})")
        lines.append(f"**From:** {match.feature_id}")
        lines.append(f"**Lesson:** {match.lesson_or_resolution}")
        if match.original_context:
            lines.append(f"**Context:** {match.original_context[:100]}")
        lines.append("")

    return "\n".join(lines)


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
