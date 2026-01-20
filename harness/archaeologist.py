#!/usr/bin/env python3
"""
Archaeologist - Extract patterns from existing codebases.

Reads existing code and extracts conventions for the coding agent to follow.
This prevents "pattern drift" where new code deviates from established conventions.

Saves extracted patterns to specs/context/patterns.md

Usage:
    /architect scan    # Run from harness shell (recommended)

    # Or directly:
    python harness/archaeologist.py
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Working Memory
try:
    from memory import update_understanding
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False


# Output path
PATTERNS_PATH = Path("specs/context/patterns.md")

# Directories to skip
SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next",
    "__pycache__", ".pytest_cache", "coverage", ".venv", "venv",
    "playwright-report", "test-results"
}


def get_directory_structure(root: Path, max_depth: int = 3, prefix: str = "") -> str:
    """Generate a tree-like directory structure string."""
    if not root.exists():
        return ""

    lines = []
    items = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name))

    for i, item in enumerate(items):
        # Skip hidden and excluded directories
        if item.name.startswith(".") or item.name in SKIP_DIRS:
            continue

        is_last = i == len(items) - 1
        connector = "└── " if is_last else "├── "
        lines.append(f"{prefix}{connector}{item.name}")

        if item.is_dir() and max_depth > 1:
            extension = "    " if is_last else "│   "
            subtree = get_directory_structure(item, max_depth - 1, prefix + extension)
            if subtree:
                lines.append(subtree)

    return "\n".join(lines)


def find_representative_files() -> dict[str, str]:
    """
    Find 5 representative source files from the codebase.

    Tries to get a mix of:
    - Component (React/Vue)
    - Page/Route
    - API route
    - Utility/Library
    - Test
    """
    samples = {}

    # Priority patterns to look for (in order)
    patterns = [
        # React/Next.js patterns
        ("src/components/**/*.tsx", "component"),
        ("src/components/**/*.jsx", "component"),
        ("components/**/*.tsx", "component"),
        ("app/**/page.tsx", "page"),
        ("src/app/**/page.tsx", "page"),
        ("pages/**/*.tsx", "page"),
        ("app/api/**/*.ts", "api"),
        ("src/app/api/**/*.ts", "api"),
        ("pages/api/**/*.ts", "api"),
        ("src/lib/**/*.ts", "utility"),
        ("lib/**/*.ts", "utility"),
        ("src/utils/**/*.ts", "utility"),
        ("utils/**/*.ts", "utility"),
        # Tests
        ("tests/**/*.spec.ts", "test"),
        ("tests/**/*.test.ts", "test"),
        ("**/*.spec.ts", "test"),
        ("**/*.test.tsx", "test"),
        # Python patterns
        ("src/**/*.py", "python"),
        ("app/**/*.py", "python"),
        ("harness/**/*.py", "python"),
        ("lib/**/*.py", "python"),
        ("tests/**/*.py", "test"),
        ("test_*.py", "test"),
    ]

    categories_found = set()

    for glob_pattern, category in patterns:
        if len(samples) >= 5:
            break
        if category in categories_found and len(samples) >= 3:
            continue

        for path in Path(".").glob(glob_pattern):
            if len(samples) >= 5:
                break

            # Skip excluded directories
            if any(skip in path.parts for skip in SKIP_DIRS):
                continue

            # Skip very large files
            try:
                content = path.read_text(errors="ignore")
                if len(content) > 10000:  # Skip files > 10KB
                    content = content[:5000] + "\n\n... (truncated) ..."
                samples[str(path)] = content
                categories_found.add(category)
            except Exception:
                pass

    return samples


def build_extraction_prompt(context: str) -> str:
    """Build the prompt for Claude to analyze patterns."""
    return f"""Analyze this existing codebase and extract the coding patterns and strict conventions.

{context}

Output a markdown document with these sections:

## 1. Naming Conventions
- File naming (kebab-case, PascalCase, etc.)
- Function/method naming
- Variable naming
- Component naming

## 2. Import Patterns
- Import ordering (React first? Third-party? Local?)
- Absolute vs relative imports
- Path aliases used (@/, ~/)?

## 3. Component Patterns
- Functional vs class components
- Props interface naming (Props suffix?)
- Default exports vs named exports
- State management patterns
- Hook usage patterns

## 4. API Patterns
- Route structure
- Request/Response typing
- Error handling patterns
- Authentication patterns

## 5. Testing Patterns
- Test file naming
- Test structure (describe/it, test blocks)
- Mocking patterns
- Assertion style

## 6. Forbidden Patterns
List things that should NEVER be done based on what you see:
- Libraries not used
- Patterns avoided
- Anti-patterns to reject

## 7. Required Libraries
List the exact libraries that MUST be used (do not introduce new ones):
- State management: [library]
- Styling: [library]
- Forms: [library]
- etc.

Be specific. Give examples from the actual code. The coding agent will use this to maintain consistency."""


def extract_patterns() -> str:
    """
    Main extraction function.

    Reads various sources of context and sends to Claude for analysis.
    """
    context_parts = []

    # 1. Read CLAUDE.md for high-level context
    claude_md = Path(".claude/CLAUDE.md")
    if claude_md.exists():
        content = claude_md.read_text()[:2000]
        context_parts.append(f"## Project Constitution (.claude/CLAUDE.md)\n\n{content}")

    # 2. Read package.json for dependencies
    pkg_json = Path("package.json")
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            deps = list(pkg.get("dependencies", {}).keys())
            dev_deps = list(pkg.get("devDependencies", {}).keys())
            scripts = list(pkg.get("scripts", {}).keys())

            context_parts.append("## Dependencies (package.json)\n")
            context_parts.append(f"**Runtime:** {', '.join(deps) if deps else 'none'}")
            context_parts.append(f"**Dev:** {', '.join(dev_deps) if dev_deps else 'none'}")
            context_parts.append(f"**Scripts:** {', '.join(scripts) if scripts else 'none'}")
        except json.JSONDecodeError:
            pass

    # 3. Read requirements.txt for Python projects
    req_txt = Path("requirements.txt")
    if req_txt.exists():
        content = req_txt.read_text()
        context_parts.append(f"## Python Dependencies (requirements.txt)\n\n```\n{content[:1000]}\n```")

    # 4. Read src/ structure
    for src_dir in ["src", "app", "lib", "harness"]:
        src = Path(src_dir)
        if src.exists():
            structure = get_directory_structure(src, max_depth=4)
            if structure:
                context_parts.append(f"## Source Structure ({src_dir}/)\n\n```\n{structure}\n```")
                break

    # 5. Read tsconfig.json for path aliases
    tsconfig = Path("tsconfig.json")
    if tsconfig.exists():
        try:
            content = tsconfig.read_text()
            # Extract just the paths section
            if '"paths"' in content:
                context_parts.append(f"## TypeScript Config (relevant parts)\n\n```json\n{content[:1500]}\n```")
        except Exception:
            pass

    # 6. Read representative files
    samples = find_representative_files()
    if samples:
        context_parts.append("## Sample Source Files\n")
        for path, content in samples.items():
            # Truncate long files
            if len(content) > 1500:
                content = content[:1500] + "\n\n... (truncated) ..."
            context_parts.append(f"### {path}\n\n```\n{content}\n```\n")

    if not context_parts:
        return "No codebase context found."

    # Build full context
    full_context = "\n\n".join(context_parts)

    # Build extraction prompt
    extraction_prompt = build_extraction_prompt(full_context)

    # Call Claude CLI
    print("Analyzing codebase patterns...")
    try:
        result = subprocess.run(
            ["claude", "--print", extraction_prompt, "--dangerously-skip-permissions"],
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
        sys.exit(1)


def save_patterns(content: str) -> None:
    """Save extracted patterns to specs/context/patterns.md."""
    PATTERNS_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Add header
    header = """# Codebase Patterns

> Auto-generated by archaeologist.py
> These patterns are IMMUTABLE. The coding agent must follow them exactly.

---

"""
    PATTERNS_PATH.write_text(header + content)
    print(f"Saved patterns to {PATTERNS_PATH}")

    # Update working memory with key patterns
    if MEMORY_AVAILABLE:
        update_understanding("archaeologist", "patterns_extracted", "true")
        update_understanding("archaeologist", "patterns_path", str(PATTERNS_PATH))
        # Extract and record key sections if they exist
        if "## Naming Conventions" in content:
            section_start = content.find("## Naming Conventions")
            section_end = content.find("##", section_start + 5)
            if section_end == -1:
                section_end = len(content)
            naming = content[section_start:section_end].strip()[:300]
            update_understanding("archaeologist", "naming_conventions", naming)


def has_python_files(directory: Path = Path(".")) -> bool:
    """Check if directory contains Python files (excluding __pycache__)."""
    for py_file in directory.glob("**/*.py"):
        if "__pycache__" not in py_file.parts and ".venv" not in py_file.parts:
            return True
    return False


def run_extraction() -> int:
    """CLI entry point."""
    print("=" * 60)
    print("ARCHAEOLOGIST - Pattern Extraction")
    print("=" * 60)

    # Check for existing codebase
    has_src = Path("src").exists() or Path("app").exists() or Path("lib").exists() or Path("harness").exists()
    has_pkg = Path("package.json").exists() or Path("requirements.txt").exists()
    has_python = has_python_files()

    if not has_src and not has_pkg and not has_python:
        print("\nNo existing codebase detected.")
        print("Expected: src/, app/, lib/, or harness/ directory, package.json, requirements.txt, or Python files")
        return 1

    print("\nDetected codebase. Extracting patterns...")

    try:
        patterns = extract_patterns()
        save_patterns(patterns)

        print("\n" + "=" * 60)
        print("EXTRACTION COMPLETE")
        print("=" * 60)
        print(f"\nPatterns saved to: {PATTERNS_PATH}")
        print("\nThe coding agent will use these patterns to maintain consistency.")
        return 0

    except Exception as e:
        print(f"\nExtraction failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(run_extraction())
