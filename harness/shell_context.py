#!/usr/bin/env python3
"""
Shell Context Builder

Builds context strings for Claude queries in the interactive shell.
Gathers project state from multiple sources to provide Claude with
relevant information for answering user questions.
"""

import json
from pathlib import Path
from typing import Optional

# Paths
SPECS_DIR = Path("specs")
FEATURES_PATH = SPECS_DIR / "features.json"
SESSION_PATH = SPECS_DIR / "session.json"
TECH_PLAN_PATH = SPECS_DIR / "gate-3-tech-plan.md"
LEARNINGS_PATH = SPECS_DIR / "learnings.json"
HEALTH_REPORT_PATH = SPECS_DIR / "health_report.md"
DEFERRED_PATH = SPECS_DIR / "deferred-scope.md"


def get_feature_summary() -> Optional[str]:
    """Get a summary of the current feature backlog status."""
    if not FEATURES_PATH.exists():
        return None

    try:
        data = json.loads(FEATURES_PATH.read_text())
        features = data.get("features", [])

        if not features:
            return None

        # Count by status
        by_status = {}
        for f in features:
            status = f.get("status", "unknown")
            by_status[status] = by_status.get(status, 0) + 1

        # Find current feature (in_progress)
        current = None
        for f in features:
            if f.get("status") == "in_progress":
                current = f
                break

        # Build summary
        lines = ["## Feature Backlog Status"]
        lines.append(f"Total features: {len(features)}")

        for status in ["passing", "failing", "in_progress", "todo"]:
            if status in by_status:
                lines.append(f"  - {status}: {by_status[status]}")

        if any(f.get("blocked") for f in features):
            blocked_count = sum(1 for f in features if f.get("blocked"))
            lines.append(f"  - blocked: {blocked_count}")

        if current:
            lines.append(f"\nCurrent feature: {current.get('id')}")
            lines.append(f"  Description: {current.get('description', '')[:100]}...")

        # List next few todo features by priority
        todos = sorted(
            [f for f in features if f.get("status") == "todo" and not f.get("blocked")],
            key=lambda x: x.get("priority", 999)
        )[:3]

        if todos:
            lines.append("\nNext features in queue:")
            for f in todos:
                lines.append(f"  - {f.get('id')}: {f.get('description', '')[:60]}...")

        return "\n".join(lines)

    except (json.JSONDecodeError, IOError):
        return None


def get_session_summary() -> Optional[str]:
    """Get a summary of the current architect session."""
    if not SESSION_PATH.exists():
        return None

    try:
        data = json.loads(SESSION_PATH.read_text())

        phase = data.get("phase", "unknown")
        product_idea = data.get("product_idea", "")

        lines = ["## Architect Session"]
        lines.append(f"Product: {product_idea}")
        lines.append(f"Current gate: {phase}")

        # Gate documents status
        gate_docs = [
            ("Gate 1 (Problem)", SPECS_DIR / "gate-1-problem-discovery.md"),
            ("Gate 2 (Solution)", SPECS_DIR / "gate-2-solution-space.md"),
            ("Gate 3 (Technical)", TECH_PLAN_PATH),
            ("Gate 4 (Edge Cases)", SPECS_DIR / "gate-4-edge-cases.md"),
        ]

        doc_status = []
        for name, path in gate_docs:
            status = "done" if path.exists() else "pending"
            doc_status.append(f"  - {name}: {status}")

        if doc_status:
            lines.append("\nGate documents:")
            lines.extend(doc_status)

        return "\n".join(lines)

    except (json.JSONDecodeError, IOError):
        return None


def get_tech_plan_summary() -> Optional[str]:
    """Get a brief summary of the technical plan."""
    if not TECH_PLAN_PATH.exists():
        return None

    try:
        content = TECH_PLAN_PATH.read_text()

        # Just note that it exists and grab first few lines
        lines = content.split("\n")[:10]
        preview = "\n".join(lines)

        return f"## Technical Plan\nFile: specs/gate-3-tech-plan.md\nPreview:\n{preview}..."

    except IOError:
        return None


def get_learnings_summary() -> Optional[str]:
    """Get a summary of captured learnings."""
    if not LEARNINGS_PATH.exists():
        return None

    try:
        data = json.loads(LEARNINGS_PATH.read_text())
        learnings = data.get("learnings", [])

        if not learnings:
            return None

        # Group by category
        by_category = {}
        for l in learnings:
            cat = l.get("category", "general")
            by_category[cat] = by_category.get(cat, 0) + 1

        lines = ["## Captured Learnings"]
        lines.append(f"Total lessons: {len(learnings)}")

        for cat, count in sorted(by_category.items()):
            lines.append(f"  - {cat}: {count}")

        # Show most recent lesson
        if learnings:
            recent = learnings[-1]
            lines.append(f"\nMost recent: {recent.get('lesson', '')[:80]}...")

        return "\n".join(lines)

    except (json.JSONDecodeError, IOError):
        return None


def get_health_summary() -> Optional[str]:
    """Get a summary of project health status."""
    if not HEALTH_REPORT_PATH.exists():
        return None

    try:
        content = HEALTH_REPORT_PATH.read_text()

        # Extract status line
        status = "unknown"
        if "HEALTHY" in content:
            status = "healthy"
        elif "DRIFTING" in content:
            status = "drifting"
        elif "CRITICAL" in content:
            status = "critical"

        # Extract key metrics from markdown tables if present
        lines = ["## Project Health"]
        lines.append(f"Status: {status.upper()}")
        lines.append(f"Report: specs/health_report.md")

        return "\n".join(lines)

    except IOError:
        return None


def get_deferred_summary() -> Optional[str]:
    """Get a summary of deferred scope items."""
    if not DEFERRED_PATH.exists():
        return None

    try:
        content = DEFERRED_PATH.read_text()

        # Count items (each starts with "### ")
        item_count = content.count("### ")

        if item_count == 0:
            return None

        return f"## Deferred Scope\n{item_count} items deferred to future versions\nFile: specs/deferred-scope.md"

    except IOError:
        return None


def get_codebase_hint() -> str:
    """Get a hint about the codebase structure."""
    hints = []

    # Detect project type
    if Path("package.json").exists():
        hints.append("Node.js project (package.json present)")

        # Check for common frameworks
        try:
            pkg = json.loads(Path("package.json").read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

            if "next" in deps:
                hints.append("Framework: Next.js")
            elif "react" in deps:
                hints.append("Framework: React")
            elif "vue" in deps:
                hints.append("Framework: Vue")

            if "playwright" in deps or "@playwright/test" in deps:
                hints.append("Testing: Playwright")
            elif "jest" in deps:
                hints.append("Testing: Jest")
        except (json.JSONDecodeError, IOError):
            pass

    if Path("requirements.txt").exists() or Path("pyproject.toml").exists():
        hints.append("Python project")

        if Path("pytest.ini").exists() or Path("pyproject.toml").exists():
            hints.append("Testing: pytest")

    # Key directories
    key_dirs = [
        ("src/app", "Next.js App Router"),
        ("src/pages", "Next.js Pages Router"),
        ("src/components", "Components directory"),
        ("tests/e2e", "E2E tests"),
        ("specs", "Specification files"),
        ("harness", "Agent harness"),
    ]

    found_dirs = []
    for dir_path, description in key_dirs:
        if Path(dir_path).exists():
            found_dirs.append(f"  - {dir_path}/ ({description})")

    if found_dirs:
        hints.append("\nKey directories:")
        hints.extend(found_dirs)

    if not hints:
        return "## Codebase\nNo specific project structure detected."

    return "## Codebase Structure\n" + "\n".join(hints)


def build_project_context() -> str:
    """
    Build a complete context string for Claude queries.

    Gathers information from multiple sources to give Claude
    understanding of the current project state.
    """
    context_parts = []

    # Feature backlog status
    feature_summary = get_feature_summary()
    if feature_summary:
        context_parts.append(feature_summary)

    # Architect session status
    session_summary = get_session_summary()
    if session_summary:
        context_parts.append(session_summary)

    # Health status
    health_summary = get_health_summary()
    if health_summary:
        context_parts.append(health_summary)

    # Learnings
    learnings_summary = get_learnings_summary()
    if learnings_summary:
        context_parts.append(learnings_summary)

    # Deferred scope
    deferred_summary = get_deferred_summary()
    if deferred_summary:
        context_parts.append(deferred_summary)

    # Codebase structure
    codebase_hint = get_codebase_hint()
    if codebase_hint:
        context_parts.append(codebase_hint)

    if not context_parts:
        return "No project context available. This may be a new project."

    return "\n\n".join(context_parts)


def get_quick_status() -> dict:
    """
    Get a quick status dict for display in the shell prompt or status command.

    Returns a dict with key metrics that can be displayed briefly.
    """
    status = {
        "features_total": 0,
        "features_passing": 0,
        "features_failing": 0,
        "features_todo": 0,
        "current_feature": None,
        "architect_phase": None,
        "health_status": None,
    }

    # Features
    if FEATURES_PATH.exists():
        try:
            data = json.loads(FEATURES_PATH.read_text())
            features = data.get("features", [])
            status["features_total"] = len(features)
            status["features_passing"] = sum(1 for f in features if f.get("status") == "passing")
            status["features_failing"] = sum(1 for f in features if f.get("status") == "failing")
            status["features_todo"] = sum(1 for f in features if f.get("status") == "todo")

            for f in features:
                if f.get("status") == "in_progress":
                    status["current_feature"] = f.get("id")
                    break
        except (json.JSONDecodeError, IOError):
            pass

    # Architect session
    if SESSION_PATH.exists():
        try:
            data = json.loads(SESSION_PATH.read_text())
            status["architect_phase"] = data.get("phase")
        except (json.JSONDecodeError, IOError):
            pass

    # Health
    if HEALTH_REPORT_PATH.exists():
        try:
            content = HEALTH_REPORT_PATH.read_text()
            if "CRITICAL" in content:
                status["health_status"] = "critical"
            elif "DRIFTING" in content:
                status["health_status"] = "drifting"
            elif "HEALTHY" in content:
                status["health_status"] = "healthy"
        except IOError:
            pass

    return status


if __name__ == "__main__":
    # Test the context builder
    print("=== Project Context ===\n")
    print(build_project_context())
    print("\n=== Quick Status ===")
    print(json.dumps(get_quick_status(), indent=2))
