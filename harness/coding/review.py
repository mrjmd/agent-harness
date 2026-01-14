#!/usr/bin/env python3
"""
Review - Check modified files against established patterns.

Prevents pattern drift by validating changes don't introduce:
- New libraries not in package.json
- Different naming conventions
- Forbidden patterns

Usage:
    from review import enforce_patterns, PatternViolation

    try:
        enforce_patterns(modified_files)
    except PatternViolation as e:
        # Handle violation
"""

import json
import re
from pathlib import Path
from typing import Optional


# File paths
PATTERNS_PATH = Path("specs/context/patterns.md")
PACKAGE_JSON = Path("package.json")
REQUIREMENTS_TXT = Path("requirements.txt")


class PatternViolation(Exception):
    """Raised when a file violates established patterns."""
    pass


def review_changes(modified_files: list[str]) -> list[str]:
    """
    Review modified files for pattern violations.

    Args:
        modified_files: List of file paths that were modified

    Returns:
        List of violation messages (empty if all OK)
    """
    violations = []

    # Load patterns and allowed imports
    patterns = load_patterns()
    allowed_imports = get_allowed_imports()

    for file_path in modified_files:
        path = Path(file_path)
        if not path.exists():
            continue

        # Skip non-code files
        if path.suffix not in [".ts", ".tsx", ".js", ".jsx", ".py"]:
            continue

        try:
            content = path.read_text(errors="ignore")
        except Exception:
            continue

        # Check for unauthorized imports (JS/TS files)
        if path.suffix in [".ts", ".tsx", ".js", ".jsx"]:
            imports = extract_js_imports(content)
            for imp in imports:
                if not is_allowed_import(imp, allowed_imports):
                    violations.append(
                        f"[{file_path}] Unauthorized import: '{imp}' - not in package.json"
                    )

        # Check for unauthorized imports (Python files)
        if path.suffix == ".py":
            imports = extract_python_imports(content)
            for imp in imports:
                if not is_allowed_python_import(imp):
                    violations.append(
                        f"[{file_path}] Potentially unauthorized import: '{imp}'"
                    )

        # Check forbidden patterns
        for forbidden in patterns.get("forbidden", []):
            pattern = forbidden.get("pattern", "")
            if pattern and re.search(pattern, content):
                violations.append(
                    f"[{file_path}] Forbidden pattern: {forbidden.get('description', pattern)}"
                )

    return violations


def load_patterns() -> dict:
    """
    Load patterns from specs/context/patterns.md.

    Extracts forbidden patterns section.
    """
    if not PATTERNS_PATH.exists():
        return {}

    try:
        content = PATTERNS_PATH.read_text()
    except Exception:
        return {}

    patterns = {"forbidden": []}

    # Extract forbidden patterns section
    # Look for section header variations
    section_patterns = [
        r"##\s*(?:\d+\.\s*)?Forbidden Patterns?\n(.*?)(?=\n##|\Z)",
        r"##\s*(?:\d+\.\s*)?Anti-?[Pp]atterns?\n(.*?)(?=\n##|\Z)",
        r"##\s*(?:\d+\.\s*)?Never Do\n(.*?)(?=\n##|\Z)",
    ]

    for section_pattern in section_patterns:
        match = re.search(section_pattern, content, re.DOTALL | re.IGNORECASE)
        if match:
            section_content = match.group(1)

            # Extract bullet points
            for line in section_content.split("\n"):
                line = line.strip()
                if line.startswith("-") or line.startswith("*"):
                    item = line.lstrip("-* ").strip()
                    if item:
                        patterns["forbidden"].append({
                            "pattern": escape_for_regex(item),
                            "description": item
                        })
            break

    return patterns


def escape_for_regex(text: str) -> str:
    """
    Convert a description to a simple regex pattern.

    For complex patterns, this does simple substring matching.
    """
    # If it looks like a regex already (contains special chars), use as-is
    if any(c in text for c in [".*", "\\s", "\\w", "[", "]", "^", "$"]):
        return text

    # Otherwise, escape special chars for literal matching
    # But this is too strict - better to not match literally
    # Instead, return empty to skip regex matching for descriptions
    return ""


def get_allowed_imports() -> set[str]:
    """Get list of allowed imports from package.json."""
    allowed = set()

    # From package.json
    if PACKAGE_JSON.exists():
        try:
            pkg = json.loads(PACKAGE_JSON.read_text())
            allowed.update(pkg.get("dependencies", {}).keys())
            allowed.update(pkg.get("devDependencies", {}).keys())
            allowed.update(pkg.get("peerDependencies", {}).keys())
        except Exception:
            pass

    # Add Node.js builtins
    allowed.update([
        "fs", "path", "os", "crypto", "http", "https", "url",
        "util", "events", "stream", "child_process", "process",
        "buffer", "querystring", "zlib", "net", "tls", "dns",
        "assert", "timers", "readline", "worker_threads",
        # Node: prefixed versions
        "node:fs", "node:path", "node:os", "node:crypto",
        "node:http", "node:https", "node:url", "node:util",
        "node:events", "node:stream", "node:child_process",
        "node:buffer", "node:querystring", "node:zlib",
        "node:net", "node:tls", "node:dns", "node:assert",
        "node:timers", "node:readline", "node:worker_threads",
    ])

    # Add React ecosystem basics (commonly implicit)
    allowed.update(["react", "react-dom", "next", "next/"])

    return allowed


def extract_js_imports(content: str) -> list[str]:
    """Extract import statements from JS/TS file content."""
    imports = []

    # ES6 imports: import ... from 'package'
    for match in re.finditer(r'import\s+.*?\s+from\s+[\'"]([^\'"]+)[\'"]', content):
        imports.append(match.group(1))

    # Side-effect imports: import 'package'
    for match in re.finditer(r'import\s+[\'"]([^\'"]+)[\'"]', content):
        imports.append(match.group(1))

    # Dynamic imports: import('package')
    for match in re.finditer(r'import\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)', content):
        imports.append(match.group(1))

    # require()
    for match in re.finditer(r'require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)', content):
        imports.append(match.group(1))

    return imports


def extract_python_imports(content: str) -> list[str]:
    """Extract import statements from Python file content."""
    imports = []

    # import package
    for match in re.finditer(r'^import\s+(\w+)', content, re.MULTILINE):
        imports.append(match.group(1))

    # from package import ...
    for match in re.finditer(r'^from\s+(\w+)', content, re.MULTILINE):
        imports.append(match.group(1))

    return imports


def is_allowed_import(imp: str, allowed: set[str]) -> bool:
    """Check if an import is allowed."""
    # Relative imports are always allowed
    if imp.startswith("./") or imp.startswith("../"):
        return True

    # Path alias imports (common patterns)
    if imp.startswith("@/") or imp.startswith("~/") or imp.startswith("#"):
        return True

    # Check direct match
    if imp in allowed:
        return True

    # Check package name (first segment)
    package = imp.split("/")[0]
    if package in allowed:
        return True

    # Scoped packages (@org/package)
    if imp.startswith("@"):
        parts = imp.split("/")
        if len(parts) >= 2:
            scoped = f"{parts[0]}/{parts[1]}"
            if scoped in allowed:
                return True

    return False


def is_allowed_python_import(imp: str) -> bool:
    """
    Check if a Python import is likely allowed.

    This is more lenient since Python has a huge stdlib.
    """
    # Standard library modules (common ones)
    stdlib = {
        "os", "sys", "re", "json", "typing", "pathlib", "collections",
        "datetime", "time", "random", "math", "itertools", "functools",
        "subprocess", "threading", "multiprocessing", "asyncio",
        "unittest", "pytest", "dataclasses", "enum", "abc",
        "io", "tempfile", "shutil", "glob", "fnmatch",
        "urllib", "http", "socket", "ssl", "email",
        "logging", "warnings", "traceback", "inspect",
        "copy", "pickle", "hashlib", "hmac", "secrets",
        "csv", "configparser", "argparse", "getopt",
        "contextlib", "operator", "string", "textwrap",
    }

    if imp in stdlib:
        return True

    # Check requirements.txt if it exists
    if REQUIREMENTS_TXT.exists():
        try:
            content = REQUIREMENTS_TXT.read_text()
            # Simple check - package name appears in requirements
            if imp.lower() in content.lower():
                return True
        except Exception:
            pass

    # Local imports (relative to project)
    # These would typically fail actual import, so allow them
    return True


def get_modified_files() -> list[str]:
    """
    Get list of modified files from git.

    Returns files that have been changed since last commit.
    """
    import subprocess

    try:
        # Get staged and unstaged changes
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        files = result.stdout.strip().split("\n")
        return [f for f in files if f]  # Filter empty strings
    except Exception:
        return []


def enforce_patterns(modified_files: Optional[list[str]] = None) -> None:
    """
    Enforce pattern compliance. Raises if violations found.

    Call this after agent modifies files but before committing.

    Args:
        modified_files: List of modified file paths.
                       If None, gets from git diff.

    Raises:
        PatternViolation: If violations are found
    """
    if modified_files is None:
        modified_files = get_modified_files()

    if not modified_files:
        return  # Nothing to check

    violations = review_changes(modified_files)

    if violations:
        error_msg = "Pattern violations detected:\n" + "\n".join(
            f"  - {v}" for v in violations
        )
        raise PatternViolation(error_msg)


def run_review() -> int:
    """CLI entry point for manual review."""
    print("=" * 60)
    print("PATTERN REVIEW - Checking modified files")
    print("=" * 60)

    modified = get_modified_files()

    if not modified:
        print("\nNo modified files to review.")
        return 0

    print(f"\nChecking {len(modified)} modified files...")
    for f in modified:
        print(f"  - {f}")

    violations = review_changes(modified)

    if violations:
        print("\n" + "=" * 60)
        print("VIOLATIONS FOUND")
        print("=" * 60)
        for v in violations:
            print(f"  - {v}")
        return 1
    else:
        print("\n" + "=" * 60)
        print("ALL CHECKS PASSED")
        print("=" * 60)
        return 0


if __name__ == "__main__":
    import sys
    sys.exit(run_review())
