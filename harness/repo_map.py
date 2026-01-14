#!/usr/bin/env python3
"""
Repository Map Generator

Generates a compressed map of the codebase structure for brownfield safety.

Instead of full context wipe between features, we inject a repository map
that shows the codebase structure. This prevents Feature B from accidentally
breaking Feature A because it didn't know Feature A existed.
"""

import subprocess
import json
from pathlib import Path
from collections import defaultdict
from typing import Optional


# File patterns to include in the map
CODE_PATTERNS = [
    "**/*.ts",
    "**/*.tsx",
    "**/*.js",
    "**/*.jsx",
    "**/*.py",
    "**/*.json",
    "**/*.yaml",
    "**/*.yml",
    "**/*.md",
    "**/*.css",
    "**/*.scss",
]

# Directories to exclude
EXCLUDE_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".next",
    "__pycache__",
    ".pytest_cache",
    "coverage",
    ".venv",
    "venv",
    "playwright-report",
    "test-results",
}


def generate_repo_map(
    root: Path = None,
    max_depth: int = 5,
    include_exports: bool = True
) -> str:
    """
    Generate a compressed map of the repository structure.

    Args:
        root: Root directory to map (defaults to cwd)
        max_depth: Maximum directory depth to traverse
        include_exports: Whether to extract exported symbols from files

    Returns:
        Formatted string representation of the codebase
    """
    if root is None:
        root = Path.cwd()

    # Try external tool first (if available)
    external_map = try_external_tool(root)
    if external_map:
        return external_map

    # Fallback to built-in implementation
    return generate_builtin_map(root, max_depth, include_exports)


def try_external_tool(root: Path) -> Optional[str]:
    """
    Try to use an external repo mapping tool like 'repomap'.

    Returns None if not available.
    """
    try:
        result = subprocess.run(
            ["repomap", "--format", "tree", str(root)],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def generate_builtin_map(root: Path, max_depth: int, include_exports: bool) -> str:
    """Built-in repository map generator."""

    tree = defaultdict(lambda: defaultdict(list))
    file_count = 0

    for pattern in CODE_PATTERNS:
        for file_path in root.glob(pattern):
            # Skip excluded directories
            if any(excluded in file_path.parts for excluded in EXCLUDE_DIRS):
                continue

            # Skip if too deep
            rel_path = file_path.relative_to(root)
            if len(rel_path.parts) > max_depth:
                continue

            file_count += 1

            # Group by directory
            dir_path = str(rel_path.parent) if rel_path.parent != Path(".") else "."
            file_name = rel_path.name

            # Extract exports if requested
            if include_exports and file_path.suffix in [".ts", ".tsx", ".js", ".jsx"]:
                exports = extract_exports(file_path)
                if exports:
                    file_name = f"{file_name} [{', '.join(exports[:5])}{'...' if len(exports) > 5 else ''}]"

            tree[dir_path][file_path.suffix].append(file_name)

    # Format output
    lines = [f"Repository Map ({file_count} files)"]
    lines.append("=" * 40)

    for dir_path in sorted(tree.keys()):
        lines.append(f"\n📁 {dir_path}/")
        files_by_ext = tree[dir_path]

        for ext in sorted(files_by_ext.keys()):
            for file_name in sorted(files_by_ext[ext]):
                lines.append(f"   {file_name}")

    return "\n".join(lines)


def extract_exports(file_path: Path) -> list[str]:
    """
    Extract exported symbols from a TypeScript/JavaScript file.

    Simple regex-based extraction for common patterns.
    """
    try:
        content = file_path.read_text(errors="ignore")
    except Exception:
        return []

    exports = []
    import re

    # export function name
    for match in re.finditer(r"export\s+(?:async\s+)?function\s+(\w+)", content):
        exports.append(match.group(1))

    # export const name
    for match in re.finditer(r"export\s+const\s+(\w+)", content):
        exports.append(match.group(1))

    # export class name
    for match in re.finditer(r"export\s+class\s+(\w+)", content):
        exports.append(match.group(1))

    # export interface name
    for match in re.finditer(r"export\s+interface\s+(\w+)", content):
        exports.append(match.group(1))

    # export type name
    for match in re.finditer(r"export\s+type\s+(\w+)", content):
        exports.append(match.group(1))

    # export default
    for match in re.finditer(r"export\s+default\s+(?:function\s+)?(\w+)", content):
        exports.append(f"default:{match.group(1)}")

    return exports


def get_file_summary(file_path: Path, max_lines: int = 5) -> str:
    """
    Get a brief summary of a file's contents.

    Returns the first few non-empty, non-comment lines.
    """
    try:
        lines = file_path.read_text(errors="ignore").split("\n")
    except Exception:
        return ""

    summary_lines = []
    for line in lines:
        stripped = line.strip()
        # Skip empty lines and comments
        if not stripped:
            continue
        if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("/*"):
            continue
        if stripped.startswith("*"):  # JSDoc continuation
            continue

        summary_lines.append(stripped[:80])
        if len(summary_lines) >= max_lines:
            break

    return "\n".join(summary_lines)


def build_feature_context(
    feature: dict,
    constitution_path: Path = None,
    learnings_path: Path = None
) -> str:
    """
    Build complete context for a feature including:
    1. Constitution
    2. Feature spec
    3. Repository map
    4. Relevant learnings
    """
    if constitution_path is None:
        constitution_path = Path(".claude/CLAUDE.md")
    if learnings_path is None:
        learnings_path = Path("specs/learnings.json")

    sections = []

    # 1. Constitution
    if constitution_path.exists():
        sections.append("# CONSTITUTION")
        sections.append(constitution_path.read_text())

    # 2. Repository Map
    sections.append("\n# CODEBASE STRUCTURE")
    sections.append(generate_repo_map())

    # 3. Current Feature
    sections.append("\n# YOUR CURRENT FEATURE")
    sections.append(json.dumps(feature, indent=2))

    # 4. Relevant Learnings
    if learnings_path.exists():
        learnings = load_relevant_learnings(learnings_path, feature)
        if learnings:
            sections.append("\n# LESSONS FROM PREVIOUS FEATURES")
            sections.append(learnings)

    sections.append("\n---")
    sections.append("\nBegin working on this feature. Write the test first, then implement.")

    return "\n".join(sections)


def load_relevant_learnings(learnings_path: Path, feature: dict, max_items: int = 10) -> str:
    """Load relevant learnings from previous features."""
    try:
        data = json.loads(learnings_path.read_text())
        learnings = data.get("learnings", [])
    except Exception:
        return ""

    if not learnings:
        return ""

    # Get most recent learnings
    recent = learnings[-max_items:]

    lines = []
    for learning in recent:
        lesson = learning.get("lesson", "")
        context = learning.get("context", "")
        if lesson:
            lines.append(f"- {lesson}")
            if context:
                lines.append(f"  Context: {context}")

    return "\n".join(lines)


# Test the module
if __name__ == "__main__":
    print("Testing repository map generator...")
    print()

    repo_map = generate_repo_map()
    print(repo_map)
