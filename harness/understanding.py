#!/usr/bin/env python3
"""
Codebase Understanding Engine

Extracts problem/solution/technical context from existing codebases.
Used by the Architect to adapt questioning for brownfield projects.

Key insight: Well-documented projects have answers to Gates 1-3 already
in their README, code structure, and patterns. We should infer these
and validate with the user rather than asking from scratch.

Usage:
    from understanding import extract_codebase_understanding, CodebaseUnderstanding

    understanding = extract_codebase_understanding()
    if understanding.confidence_problem >= 0.8:
        # Skip or simplify Gate 1
        print(f"Inferred problem: {understanding.inferred_problem}")
"""

import json
import re
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Shared CLI module
try:
    from cli import call_architect as _call_claude_cli
    CLI_AVAILABLE = True
except ImportError:
    CLI_AVAILABLE = False
    def _call_claude_cli(prompt: str) -> str:
        return ""


# =============================================================================
# Configuration
# =============================================================================

SPECS_DIR = Path("specs")
UNDERSTANDING_PATH = SPECS_DIR / "codebase_understanding.json"

# Confidence thresholds for adaptive gates
CONFIDENCE_THRESHOLDS = {
    "skip_gate": 0.8,    # Can skip gate with brief validation
    "assist_gate": 0.5,  # Present inference, ask for corrections
    "full_gate": 0.0,    # No inference, ask open-ended questions
}


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class Evidence:
    """Evidence supporting an inference."""
    source: str  # File path or source type
    text: str  # Relevant excerpt
    relevance: float  # 0.0-1.0


@dataclass
class CodebaseUnderstanding:
    """Inferred understanding of an existing codebase."""

    # Gate 1: Problem Discovery
    inferred_problem: str = ""  # "Task management for developers"
    inferred_users: list = field(default_factory=list)  # ["developers", "teams"]
    confidence_problem: float = 0.0  # 0.0-1.0

    # Gate 2: Solution Space
    inferred_approach: str = ""  # "Web app with real-time sync"
    apparent_alternatives_rejected: list = field(default_factory=list)
    confidence_approach: float = 0.0

    # Gate 3: Technical Design
    detected_stack: dict = field(default_factory=dict)  # {"frontend": "Next.js", ...}
    detected_patterns: list = field(default_factory=list)  # ["App Router", ...]
    confidence_technical: float = 0.0

    # Sources
    evidence: list = field(default_factory=list)  # Where each inference came from

    # Metadata
    analyzed_at: str = ""
    user_validated: bool = False
    user_corrections: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.analyzed_at:
            self.analyzed_at = datetime.now(timezone.utc).isoformat()

    def save(self) -> None:
        """Save understanding to specs/codebase_understanding.json."""
        SPECS_DIR.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        # Convert Evidence objects to dicts
        data["evidence"] = [asdict(e) if hasattr(e, "__dict__") else e for e in self.evidence]
        UNDERSTANDING_PATH.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls) -> Optional["CodebaseUnderstanding"]:
        """Load understanding from specs/codebase_understanding.json."""
        if not UNDERSTANDING_PATH.exists():
            return None
        try:
            data = json.loads(UNDERSTANDING_PATH.read_text())
            # Convert evidence dicts back to Evidence objects
            evidence_list = []
            for e in data.get("evidence", []):
                if isinstance(e, dict):
                    evidence_list.append(Evidence(**e))
                else:
                    evidence_list.append(e)
            data["evidence"] = evidence_list
            return cls(**data)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            print(f"Warning: Could not load codebase understanding: {e}")
            return None


# =============================================================================
# Evidence Extraction
# =============================================================================

def _extract_readme() -> tuple[str, float]:
    """
    Extract content from README files.

    Returns:
        Tuple of (content, relevance_score)
    """
    readme_patterns = ["README.md", "README.txt", "README", "readme.md"]
    for pattern in readme_patterns:
        readme = Path(pattern)
        if readme.exists():
            try:
                content = readme.read_text()[:5000]  # Limit size
                # Higher relevance if README has project description sections
                relevance = 0.5
                if any(h in content.lower() for h in ["## about", "## description", "## overview", "## what is"]):
                    relevance = 0.8
                if any(h in content.lower() for h in ["## features", "## getting started"]):
                    relevance = 0.9
                return content, relevance
            except IOError:
                pass
    return "", 0.0


def _extract_package_json() -> tuple[dict, float]:
    """
    Extract project info from package.json.

    Returns:
        Tuple of (parsed_data, relevance_score)
    """
    pkg_path = Path("package.json")
    if not pkg_path.exists():
        return {}, 0.0

    try:
        data = json.loads(pkg_path.read_text())
        relevance = 0.3  # Base relevance
        if data.get("description"):
            relevance += 0.2
        if data.get("keywords"):
            relevance += 0.1
        return data, min(relevance, 1.0)
    except (json.JSONDecodeError, IOError):
        return {}, 0.0


def _extract_pyproject() -> tuple[dict, float]:
    """
    Extract project info from pyproject.toml.

    Returns:
        Tuple of (parsed_data, relevance_score)
    """
    pyproject_path = Path("pyproject.toml")
    if not pyproject_path.exists():
        return {}, 0.0

    try:
        # Simple TOML parsing (basic key-value extraction)
        content = pyproject_path.read_text()
        data = {}

        # Extract project name and description
        name_match = re.search(r'^name\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
        if name_match:
            data["name"] = name_match.group(1)

        desc_match = re.search(r'^description\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
        if desc_match:
            data["description"] = desc_match.group(1)

        relevance = 0.3 if data.get("name") else 0.1
        if data.get("description"):
            relevance += 0.2

        return data, relevance
    except IOError:
        return {}, 0.0


def _extract_test_descriptions() -> tuple[list[str], float]:
    """
    Extract test descriptions from test files.

    Test descriptions often describe features in plain English.

    Returns:
        Tuple of (descriptions, relevance_score)
    """
    descriptions = []
    test_patterns = ["tests/**/*.py", "tests/**/*.ts", "tests/**/*.spec.ts", "test/**/*.py", "test/**/*.ts"]

    for pattern in test_patterns:
        for test_file in Path(".").glob(pattern):
            if "node_modules" in str(test_file):
                continue
            try:
                content = test_file.read_text(errors="ignore")

                # Extract pytest descriptions
                pytest_descs = re.findall(r'def test_([a-z_]+)', content)
                descriptions.extend([d.replace("_", " ") for d in pytest_descs])

                # Extract Playwright/Jest descriptions
                js_descs = re.findall(r"(?:test|it)\(['\"]([^'\"]+)['\"]", content)
                descriptions.extend(js_descs)

                # Extract describe blocks
                describe_descs = re.findall(r"describe\(['\"]([^'\"]+)['\"]", content)
                descriptions.extend(describe_descs)

            except IOError:
                pass

    if not descriptions:
        return [], 0.0

    # Deduplicate and limit
    descriptions = list(set(descriptions))[:50]
    relevance = min(0.3 + len(descriptions) * 0.01, 0.8)
    return descriptions, relevance


def _extract_api_routes() -> tuple[list[dict], float]:
    """
    Extract API route definitions.

    Routes reveal what the app does functionally.

    Returns:
        Tuple of (routes, relevance_score)
    """
    routes = []

    # Next.js App Router
    app_dir = Path("src/app") if Path("src/app").exists() else Path("app")
    if app_dir.exists():
        for route_file in app_dir.rglob("*/route.ts"):
            route_path = "/" + "/".join(route_file.parent.relative_to(app_dir).parts)
            routes.append({"path": route_path, "type": "api", "file": str(route_file)})

    # Express/Flask/FastAPI
    for py_file in Path(".").rglob("**/*.py"):
        if any(skip in str(py_file) for skip in ["venv", ".venv", "__pycache__", "node_modules"]):
            continue
        try:
            content = py_file.read_text(errors="ignore")
            for match in re.finditer(r'@\w+\.(?:route|get|post|put|delete)\(["\']([^"\']+)["\']', content):
                routes.append({"path": match.group(1), "type": "api", "file": str(py_file)})
        except IOError:
            pass

    if not routes:
        return [], 0.0

    relevance = min(0.4 + len(routes) * 0.02, 0.9)
    return routes, relevance


def _extract_database_schema() -> tuple[str, float]:
    """
    Extract database schema information.

    The data model reveals the domain model.

    Returns:
        Tuple of (schema_summary, relevance_score)
    """
    schema_info = []

    # Prisma schema
    prisma_path = Path("prisma/schema.prisma")
    if prisma_path.exists():
        try:
            content = prisma_path.read_text()
            # Extract model names
            models = re.findall(r'model\s+(\w+)\s*\{', content)
            schema_info.append(f"Prisma models: {', '.join(models)}")

            # Extract field samples
            for model in models[:5]:
                model_match = re.search(rf'model\s+{model}\s*\{{([^}}]+)\}}', content, re.DOTALL)
                if model_match:
                    fields = re.findall(r'^\s*(\w+)\s+\w+', model_match.group(1), re.MULTILINE)
                    if fields:
                        schema_info.append(f"  {model}: {', '.join(fields[:10])}")
        except IOError:
            pass

    # Django models
    for models_file in Path(".").rglob("**/models.py"):
        if any(skip in str(models_file) for skip in ["venv", ".venv", "node_modules"]):
            continue
        try:
            content = models_file.read_text(errors="ignore")
            classes = re.findall(r'class\s+(\w+)\(models\.Model\)', content)
            if classes:
                schema_info.append(f"Django models: {', '.join(classes)}")
        except IOError:
            pass

    # SQLAlchemy models
    for model_file in Path(".").rglob("**/*.py"):
        if any(skip in str(model_file) for skip in ["venv", ".venv", "node_modules"]):
            continue
        try:
            content = model_file.read_text(errors="ignore")
            if "declarative_base" in content or "Base =" in content:
                classes = re.findall(r'class\s+(\w+)\([^)]*Base\)', content)
                if classes:
                    schema_info.append(f"SQLAlchemy models: {', '.join(classes)}")
        except IOError:
            pass

    if not schema_info:
        return "", 0.0

    return "\n".join(schema_info), 0.8


def _detect_tech_stack() -> dict:
    """
    Detect the technical stack from dependencies and file structure.

    Returns:
        Dict of {"category": "technology", ...}
    """
    stack = {}

    # Package.json dependencies
    if Path("package.json").exists():
        try:
            pkg = json.loads(Path("package.json").read_text())
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

            # Frontend framework
            if "next" in all_deps:
                stack["frontend"] = "Next.js"
            elif "react" in all_deps:
                stack["frontend"] = "React"
            elif "vue" in all_deps:
                stack["frontend"] = "Vue.js"
            elif "@angular/core" in all_deps:
                stack["frontend"] = "Angular"
            elif "svelte" in all_deps:
                stack["frontend"] = "Svelte"

            # CSS
            if "tailwindcss" in all_deps:
                stack["css"] = "Tailwind CSS"
            elif "styled-components" in all_deps:
                stack["css"] = "styled-components"
            elif "@emotion/react" in all_deps:
                stack["css"] = "Emotion"

            # State management
            if "zustand" in all_deps:
                stack["state"] = "Zustand"
            elif "redux" in all_deps or "@reduxjs/toolkit" in all_deps:
                stack["state"] = "Redux"
            elif "jotai" in all_deps:
                stack["state"] = "Jotai"

            # Database/ORM
            if "@prisma/client" in all_deps:
                stack["database"] = "Prisma"
            elif "mongoose" in all_deps:
                stack["database"] = "MongoDB/Mongoose"
            elif "pg" in all_deps:
                stack["database"] = "PostgreSQL"

            # Auth
            if "next-auth" in all_deps:
                stack["auth"] = "NextAuth"
            elif "@auth0/auth0-react" in all_deps:
                stack["auth"] = "Auth0"
            elif "@clerk/nextjs" in all_deps:
                stack["auth"] = "Clerk"

            # Testing
            if "@playwright/test" in all_deps:
                stack["testing"] = "Playwright"
            elif "jest" in all_deps:
                stack["testing"] = "Jest"
            elif "vitest" in all_deps:
                stack["testing"] = "Vitest"

        except (json.JSONDecodeError, IOError):
            pass

    # Python dependencies
    if Path("requirements.txt").exists() or Path("pyproject.toml").exists():
        try:
            content = ""
            if Path("requirements.txt").exists():
                content = Path("requirements.txt").read_text()
            elif Path("pyproject.toml").exists():
                content = Path("pyproject.toml").read_text()

            if "fastapi" in content.lower():
                stack["backend"] = "FastAPI"
            elif "flask" in content.lower():
                stack["backend"] = "Flask"
            elif "django" in content.lower():
                stack["backend"] = "Django"

            if "sqlalchemy" in content.lower():
                stack["orm"] = "SQLAlchemy"
            if "pytest" in content.lower():
                stack["testing"] = "pytest"

        except IOError:
            pass

    return stack


def _detect_patterns() -> list[str]:
    """
    Detect architectural patterns from code structure.

    Returns:
        List of detected pattern names
    """
    patterns = []

    # Next.js patterns
    if Path("src/app").exists() or Path("app").exists():
        patterns.append("App Router")
        if list(Path(".").glob("**/loading.tsx")) or list(Path(".").glob("**/loading.ts")):
            patterns.append("Streaming/Suspense")
        if list(Path(".").glob("**/error.tsx")) or list(Path(".").glob("**/error.ts")):
            patterns.append("Error Boundaries")

    if Path("src/pages").exists() or Path("pages").exists():
        patterns.append("Pages Router")

    # Server Components vs Client Components
    for tsx_file in Path(".").rglob("**/*.tsx"):
        if "node_modules" in str(tsx_file):
            continue
        try:
            content = tsx_file.read_text(errors="ignore")[:500]
            if '"use client"' in content or "'use client'" in content:
                if "Client Components" not in patterns:
                    patterns.append("Client Components")
            if '"use server"' in content or "'use server'" in content:
                if "Server Actions" not in patterns:
                    patterns.append("Server Actions")
        except IOError:
            pass
        if len(patterns) >= 10:
            break

    # API patterns
    if Path("src/api").exists() or Path("api").exists():
        patterns.append("REST API")

    if list(Path(".").glob("**/graphql.ts")) or list(Path(".").glob("**/graphql.py")):
        patterns.append("GraphQL")

    # Middleware
    if list(Path(".").glob("**/middleware.ts")) or list(Path(".").glob("**/middleware.py")):
        patterns.append("Middleware")

    return list(set(patterns))


# =============================================================================
# Understanding Extraction
# =============================================================================

def _build_understanding_prompt(evidence: dict) -> str:
    """Build the prompt for Claude to infer understanding."""
    parts = ["You are analyzing an existing codebase to understand its purpose and design decisions.", ""]

    # README
    if evidence.get("readme"):
        parts.append("## README Content")
        parts.append(evidence["readme"][:3000])
        parts.append("")

    # Package/project info
    if evidence.get("package_json"):
        pkg = evidence["package_json"]
        parts.append("## Project Info (package.json)")
        parts.append(f"Name: {pkg.get('name', 'unknown')}")
        parts.append(f"Description: {pkg.get('description', 'none')}")
        if pkg.get("keywords"):
            parts.append(f"Keywords: {', '.join(pkg['keywords'])}")
        parts.append("")

    if evidence.get("pyproject"):
        pyp = evidence["pyproject"]
        parts.append("## Project Info (pyproject.toml)")
        parts.append(f"Name: {pyp.get('name', 'unknown')}")
        parts.append(f"Description: {pyp.get('description', 'none')}")
        parts.append("")

    # Test descriptions
    if evidence.get("test_descriptions"):
        parts.append("## Test Descriptions (features in plain English)")
        parts.append("\n".join(f"- {d}" for d in evidence["test_descriptions"][:20]))
        parts.append("")

    # API routes
    if evidence.get("api_routes"):
        parts.append("## API Routes")
        for route in evidence["api_routes"][:15]:
            parts.append(f"- {route['path']} ({route['type']})")
        parts.append("")

    # Database schema
    if evidence.get("database_schema"):
        parts.append("## Database Schema")
        parts.append(evidence["database_schema"])
        parts.append("")

    # Tech stack
    if evidence.get("tech_stack"):
        parts.append("## Detected Tech Stack")
        for category, tech in evidence["tech_stack"].items():
            parts.append(f"- {category}: {tech}")
        parts.append("")

    # Patterns
    if evidence.get("patterns"):
        parts.append("## Detected Patterns")
        parts.append(", ".join(evidence["patterns"]))
        parts.append("")

    # Task
    parts.append("""## Task

Based on the evidence above, infer the following about this project.
For each inference, rate your confidence (0.0-1.0) based on how much evidence supports it.

Respond in JSON format:
```json
{
  "problem": {
    "statement": "Brief description of the problem this project solves",
    "users": ["user type 1", "user type 2"],
    "confidence": 0.85
  },
  "approach": {
    "statement": "Brief description of the solution approach",
    "alternatives_rejected": ["why not X", "why not Y"],
    "confidence": 0.75
  },
  "technical": {
    "summary": "Brief technical architecture summary",
    "key_decisions": ["decision 1", "decision 2"],
    "confidence": 0.9
  }
}
```

IMPORTANT:
- Be specific, not generic (not "manages data" but "tracks tasks for development teams")
- Only include what you can actually infer from the evidence
- Lower confidence if evidence is sparse or ambiguous
- If you truly cannot infer something, set confidence to 0.0
""")

    return "\n".join(parts)


def extract_codebase_understanding() -> CodebaseUnderstanding:
    """
    Extract understanding of the codebase from available evidence.

    Scans README, package.json, tests, routes, and schema to infer
    the problem, approach, and technical design.

    Returns:
        CodebaseUnderstanding with inferences and confidence scores
    """
    understanding = CodebaseUnderstanding()
    evidence_dict = {}
    evidence_list = []

    print("  Extracting codebase understanding...")

    # Gather evidence
    readme_content, readme_relevance = _extract_readme()
    if readme_content:
        evidence_dict["readme"] = readme_content
        evidence_list.append(Evidence(source="README.md", text=readme_content[:200], relevance=readme_relevance))
        print(f"    -> README: {len(readme_content)} chars, relevance {readme_relevance:.2f}")

    pkg_data, pkg_relevance = _extract_package_json()
    if pkg_data:
        evidence_dict["package_json"] = pkg_data
        evidence_list.append(Evidence(source="package.json", text=str(pkg_data)[:200], relevance=pkg_relevance))
        print(f"    -> package.json: found, relevance {pkg_relevance:.2f}")

    pyp_data, pyp_relevance = _extract_pyproject()
    if pyp_data:
        evidence_dict["pyproject"] = pyp_data
        evidence_list.append(Evidence(source="pyproject.toml", text=str(pyp_data)[:200], relevance=pyp_relevance))
        print(f"    -> pyproject.toml: found, relevance {pyp_relevance:.2f}")

    test_descs, test_relevance = _extract_test_descriptions()
    if test_descs:
        evidence_dict["test_descriptions"] = test_descs
        evidence_list.append(Evidence(source="tests", text="; ".join(test_descs[:5]), relevance=test_relevance))
        print(f"    -> Tests: {len(test_descs)} descriptions, relevance {test_relevance:.2f}")

    routes, routes_relevance = _extract_api_routes()
    if routes:
        evidence_dict["api_routes"] = routes
        evidence_list.append(Evidence(source="routes", text=str(routes[:3]), relevance=routes_relevance))
        print(f"    -> Routes: {len(routes)} found, relevance {routes_relevance:.2f}")

    schema, schema_relevance = _extract_database_schema()
    if schema:
        evidence_dict["database_schema"] = schema
        evidence_list.append(Evidence(source="schema", text=schema[:200], relevance=schema_relevance))
        print(f"    -> Schema: found, relevance {schema_relevance:.2f}")

    # Detect stack and patterns (no Claude needed)
    tech_stack = _detect_tech_stack()
    if tech_stack:
        evidence_dict["tech_stack"] = tech_stack
        understanding.detected_stack = tech_stack
        understanding.confidence_technical = 0.9  # High confidence for tech detection
        print(f"    -> Tech stack: {len(tech_stack)} components detected")

    patterns = _detect_patterns()
    if patterns:
        evidence_dict["patterns"] = patterns
        understanding.detected_patterns = patterns
        print(f"    -> Patterns: {len(patterns)} detected")

    # If we have minimal evidence, return low-confidence understanding
    if not readme_content and not pkg_data and not test_descs:
        print("    -> Insufficient evidence for inference")
        understanding.evidence = evidence_list
        return understanding

    # Use Claude to infer problem and approach
    if CLI_AVAILABLE:
        prompt = _build_understanding_prompt(evidence_dict)
        print("    -> Analyzing with Claude...")

        try:
            response = _call_claude_cli(prompt)

            # Extract JSON from response
            json_match = re.search(r'```json\n(.*?)```', response, re.DOTALL)
            if json_match:
                inferred = json.loads(json_match.group(1))

                # Problem inference
                if "problem" in inferred:
                    problem = inferred["problem"]
                    understanding.inferred_problem = problem.get("statement", "")
                    understanding.inferred_users = problem.get("users", [])
                    understanding.confidence_problem = float(problem.get("confidence", 0.0))

                # Approach inference
                if "approach" in inferred:
                    approach = inferred["approach"]
                    understanding.inferred_approach = approach.get("statement", "")
                    understanding.apparent_alternatives_rejected = approach.get("alternatives_rejected", [])
                    understanding.confidence_approach = float(approach.get("confidence", 0.0))

                # Technical inference
                if "technical" in inferred:
                    technical = inferred["technical"]
                    # Confidence already set from stack detection, but can be refined
                    if technical.get("confidence", 0) > understanding.confidence_technical:
                        understanding.confidence_technical = float(technical.get("confidence", 0))

                print(f"    -> Inference complete: problem={understanding.confidence_problem:.2f}, approach={understanding.confidence_approach:.2f}, technical={understanding.confidence_technical:.2f}")

        except (json.JSONDecodeError, ValueError) as e:
            print(f"    -> Inference failed: {e}")
    else:
        print("    -> CLI not available, using evidence-only understanding")

    understanding.evidence = evidence_list
    return understanding


def get_understanding_context_for_gate(
    understanding: CodebaseUnderstanding,
    gate: int
) -> tuple[str, str]:
    """
    Get context string for a specific gate based on understanding confidence.

    Returns:
        Tuple of (context_message, questioning_mode)
        questioning_mode is one of: "skip", "validate", "full"
    """
    if gate == 1:  # Problem Discovery
        confidence = understanding.confidence_problem
        if confidence >= CONFIDENCE_THRESHOLDS["skip_gate"]:
            context = f"""Based on the codebase, this appears to be:

**Problem:** {understanding.inferred_problem}
**Target Users:** {', '.join(understanding.inferred_users) if understanding.inferred_users else 'Not identified'}

Is this understanding accurate? What would you add or correct?"""
            return context, "validate"

        elif confidence >= CONFIDENCE_THRESHOLDS["assist_gate"]:
            context = f"""I found some context about the project:

**Possible Problem:** {understanding.inferred_problem or 'Could not determine'}
**Possible Users:** {', '.join(understanding.inferred_users) if understanding.inferred_users else 'Not identified'}

This inference has low confidence ({confidence:.0%}). Can you help clarify what problem this project solves?"""
            return context, "validate"

        else:
            return "I found limited context about the project's purpose. What problem does this project solve?", "full"

    elif gate == 2:  # Solution Space
        confidence = understanding.confidence_approach
        if confidence >= CONFIDENCE_THRESHOLDS["skip_gate"]:
            context = f"""The codebase shows a clear solution approach:

**Approach:** {understanding.inferred_approach}

Were other approaches considered? Any trade-offs I should know about?"""
            return context, "validate"

        elif confidence >= CONFIDENCE_THRESHOLDS["assist_gate"]:
            context = f"""Based on the code structure, the solution approach appears to be:

**Possible Approach:** {understanding.inferred_approach or 'Could not determine'}

This is a low-confidence inference ({confidence:.0%}). What approach did you choose and why?"""
            return context, "validate"

        else:
            return "I couldn't determine the solution approach from the codebase. What approach did you choose?", "full"

    elif gate == 3:  # Technical Design
        confidence = understanding.confidence_technical
        stack_str = ", ".join(f"{k}: {v}" for k, v in understanding.detected_stack.items()) if understanding.detected_stack else "Not detected"
        patterns_str = ", ".join(understanding.detected_patterns) if understanding.detected_patterns else "None detected"

        if confidence >= CONFIDENCE_THRESHOLDS["skip_gate"]:
            context = f"""Detected technical stack:

**Stack:** {stack_str}
**Patterns:** {patterns_str}

Validating against codebase... All confirmed.

Any technical decisions I should know about that aren't visible in the code?"""
            return context, "validate"

        elif confidence >= CONFIDENCE_THRESHOLDS["assist_gate"]:
            context = f"""I detected some technical details:

**Stack:** {stack_str}
**Patterns:** {patterns_str}

Confidence: {confidence:.0%}. Are these correct? What else should I know?"""
            return context, "validate"

        else:
            context = f"""I detected minimal technical details:

**Stack:** {stack_str}

What is the technical architecture of this project?"""
            return context, "full"

    return "", "full"


# =============================================================================
# CLI
# =============================================================================

def main():
    """CLI entry point for testing."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Codebase Understanding Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="extract",
        choices=["extract", "show"],
        help="Command: extract (analyze codebase) or show (display saved understanding)"
    )

    args = parser.parse_args()

    if args.command == "show":
        understanding = CodebaseUnderstanding.load()
        if not understanding:
            print("No saved understanding found. Run 'extract' first.")
            return 1

        print("=" * 60)
        print("CODEBASE UNDERSTANDING")
        print("=" * 60)
        print(f"\nAnalyzed at: {understanding.analyzed_at}")
        print(f"User validated: {understanding.user_validated}")
        print(f"\n## Problem (confidence: {understanding.confidence_problem:.0%})")
        print(f"  {understanding.inferred_problem or 'Not inferred'}")
        print(f"  Users: {', '.join(understanding.inferred_users) if understanding.inferred_users else 'Not identified'}")
        print(f"\n## Approach (confidence: {understanding.confidence_approach:.0%})")
        print(f"  {understanding.inferred_approach or 'Not inferred'}")
        print(f"\n## Technical (confidence: {understanding.confidence_technical:.0%})")
        print(f"  Stack: {understanding.detected_stack}")
        print(f"  Patterns: {understanding.detected_patterns}")
        return 0

    else:  # extract
        understanding = extract_codebase_understanding()
        understanding.save()

        print(f"\n{'=' * 60}")
        print("UNDERSTANDING EXTRACTED")
        print("=" * 60)
        print(f"Saved to: {UNDERSTANDING_PATH}")
        print(f"\nConfidence scores:")
        print(f"  Problem: {understanding.confidence_problem:.0%}")
        print(f"  Approach: {understanding.confidence_approach:.0%}")
        print(f"  Technical: {understanding.confidence_technical:.0%}")

        avg_confidence = (understanding.confidence_problem + understanding.confidence_approach + understanding.confidence_technical) / 3
        if avg_confidence >= CONFIDENCE_THRESHOLDS["skip_gate"]:
            print("\nRecommendation: HIGH confidence - Gates 1-3 can be streamlined")
        elif avg_confidence >= CONFIDENCE_THRESHOLDS["assist_gate"]:
            print("\nRecommendation: MEDIUM confidence - Present inferences for validation")
        else:
            print("\nRecommendation: LOW confidence - Use standard questioning")

        return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
