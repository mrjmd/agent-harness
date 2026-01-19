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


class FileScopeViolation(Exception):
    """Raised when a file modification violates the feature's file scope."""
    pass


def validate_file_scope(modified_files: list[str], feature: dict) -> None:
    """
    Validate that modified files comply with the feature's file_scope restrictions.

    Args:
        modified_files: List of file paths that were modified
        feature: The feature dict which may contain file_scope

    Raises:
        FileScopeViolation: If any file violates scope restrictions

    File scope format in feature:
        {
            "file_scope": {
                "create": ["src/new_file.ts"],     # Files that should be created
                "modify": ["src/existing.ts"],     # Files that may be modified
                "forbidden": [".env*", "package.json"]  # Files that must not be touched
            }
        }
    """
    file_scope = feature.get("file_scope")
    if not file_scope:
        return  # No restrictions defined

    violations = []

    # Get scope lists
    allowed_create = set(file_scope.get("create", []))
    allowed_modify = set(file_scope.get("modify", []))
    forbidden_patterns = file_scope.get("forbidden", [])

    # All allowed files
    all_allowed = allowed_create | allowed_modify

    for file_path in modified_files:
        path = Path(file_path)
        if not path.exists():
            continue  # Deleted files are OK

        # Check forbidden patterns first (glob-style matching)
        for forbidden_pattern in forbidden_patterns:
            if _matches_pattern(file_path, forbidden_pattern):
                violations.append(
                    f"FORBIDDEN: '{file_path}' matches forbidden pattern '{forbidden_pattern}'"
                )
                break
        else:
            # Only check if file is in allowed list when scope is defined
            if all_allowed and file_path not in all_allowed:
                # Check if it matches any glob pattern in allowed lists
                matched = False
                for allowed in all_allowed:
                    if _matches_pattern(file_path, allowed):
                        matched = True
                        break

                if not matched:
                    violations.append(
                        f"OUT OF SCOPE: '{file_path}' is not in the allowed file list"
                    )

    if violations:
        error_msg = "File scope violations detected:\n" + "\n".join(
            f"  - {v}" for v in violations
        )
        raise FileScopeViolation(error_msg)


def _matches_pattern(file_path: str, pattern: str) -> bool:
    """
    Check if a file path matches a glob-style pattern.

    Supports:
    - Exact matches: "src/file.ts"
    - Wildcards: "*.env", ".env*"
    - Directory globs: "src/**/*.ts"
    """
    import fnmatch

    # Direct match
    if file_path == pattern:
        return True

    # Glob match
    if fnmatch.fnmatch(file_path, pattern):
        return True

    # Also check just the filename for patterns like "*.env"
    filename = Path(file_path).name
    if fnmatch.fnmatch(filename, pattern):
        return True

    return False


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
    Returns a regex pattern that can be used with re.search().
    """
    # If it looks like a regex already (contains special chars), use as-is
    regex_indicators = [".*", "\\s", "\\w", "\\d", "[", "]", "^", "$", "(?:", "\\b"]
    if any(indicator in text for indicator in regex_indicators):
        return text

    # Try to extract a code pattern from the description
    # Look for backtick-quoted code: `console.log`
    backtick_match = re.search(r'`([^`]+)`', text)
    if backtick_match:
        code_pattern = backtick_match.group(1)
        # Escape regex special chars for literal matching
        return re.escape(code_pattern)

    # Look for quoted strings: "console.log" or 'console.log'
    quote_match = re.search(r'["\']([^"\']+)["\']', text)
    if quote_match:
        code_pattern = quote_match.group(1)
        return re.escape(code_pattern)

    # Look for common code-like patterns at start or end
    # e.g., "No direct database queries" -> extract nothing useful
    # e.g., "console.log statements" -> extract "console.log"
    code_patterns = [
        r'\b(console\.\w+)\b',  # console.log, console.error, etc
        r'\b(debugger)\b',       # debugger statements
        r'\b(eval\s*\()',        # eval() calls
        r'\b(innerHTML\s*=)',    # innerHTML assignments
        r'\b(document\.write)',  # document.write
        r'\b(TODO|FIXME|XXX)\b', # Code annotations
    ]

    for pattern in code_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return re.escape(match.group(1))

    # For descriptive text without clear code patterns, try to extract key terms
    # Split on common words and look for technical terms
    words = text.split()
    # Filter to technical-looking words (contains . or _ or is camelCase)
    technical = [w.strip('.,()') for w in words
                 if '.' in w or '_' in w or (w[0].islower() and any(c.isupper() for c in w[1:]))]
    if technical:
        # Use the first technical term as the pattern
        return re.escape(technical[0])

    # Fallback: return empty string (no pattern to check)
    # This is better than trying to match arbitrary descriptions
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


def _strip_js_comments_and_strings(content: str) -> str:
    """
    Remove comments and string literals from JS/TS code to avoid false positives.

    This is a simplified approach that handles most common cases.
    """
    result = []
    i = 0
    n = len(content)

    while i < n:
        # Single-line comment
        if i + 1 < n and content[i:i+2] == '//':
            # Skip to end of line
            while i < n and content[i] != '\n':
                i += 1
            continue

        # Multi-line comment
        if i + 1 < n and content[i:i+2] == '/*':
            i += 2
            while i + 1 < n and content[i:i+2] != '*/':
                i += 1
            i += 2  # Skip */
            continue

        # Template literal (backtick strings) - skip entire thing
        if content[i] == '`':
            result.append(' ')  # Keep a space to maintain spacing
            i += 1
            while i < n:
                if content[i] == '\\' and i + 1 < n:
                    i += 2  # Skip escaped char
                    continue
                if content[i] == '`':
                    i += 1
                    break
                i += 1
            continue

        # Regular strings - but preserve import/require statements
        # Only skip strings that aren't part of import/require
        if content[i] in '"\'':
            # Check if this might be part of an import statement
            # Look back for 'from' or 'require' or 'import'
            lookback = content[max(0, i-20):i].lower()
            if 'from' in lookback or 'require' in lookback or 'import' in lookback:
                result.append(content[i])
                i += 1
                continue

            # Not an import, skip this string
            quote = content[i]
            result.append(' ')
            i += 1
            while i < n:
                if content[i] == '\\' and i + 1 < n:
                    i += 2
                    continue
                if content[i] == quote:
                    i += 1
                    break
                i += 1
            continue

        result.append(content[i])
        i += 1

    return ''.join(result)


def extract_js_imports(content: str) -> list[str]:
    """
    Extract import statements from JS/TS file content.

    Filters out imports in comments and non-import string literals.
    """
    # Pre-process to remove comments (but preserve import strings)
    processed = _strip_js_comments_and_strings(content)
    imports = []

    # ES6 imports: import ... from 'package'
    for match in re.finditer(r'import\s+.*?\s+from\s+[\'"]([^\'"]+)[\'"]', processed):
        imports.append(match.group(1))

    # Side-effect imports: import 'package'
    for match in re.finditer(r'import\s+[\'"]([^\'"]+)[\'"]', processed):
        imports.append(match.group(1))

    # Dynamic imports: import('package')
    for match in re.finditer(r'import\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)', processed):
        imports.append(match.group(1))

    # require()
    for match in re.finditer(r'require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)', processed):
        imports.append(match.group(1))

    return imports


def _strip_python_comments_and_strings(content: str) -> str:
    """
    Remove comments and docstrings from Python code to avoid false positives.

    Preserves the line structure so ^ and $ anchors still work.
    """
    lines = content.split('\n')
    result = []
    in_multiline_string = False
    multiline_quote = None

    for line in lines:
        if in_multiline_string:
            # Look for end of multiline string
            end_pos = line.find(multiline_quote)
            if end_pos != -1:
                in_multiline_string = False
                # Replace the multiline content with spaces
                result.append(' ' * len(line))
            else:
                result.append(' ' * len(line))
            continue

        # Check for # comment (after handling strings)
        new_line = []
        i = 0
        n = len(line)

        while i < n:
            # Triple-quoted string (start of multiline)
            if i + 2 < n and line[i:i+3] in ('"""', "'''"):
                quote = line[i:i+3]
                # Look for end on same line
                end_pos = line.find(quote, i + 3)
                if end_pos != -1:
                    # String ends on same line - skip it
                    new_line.append(' ' * (end_pos - i + 3))
                    i = end_pos + 3
                else:
                    # Multiline string starts
                    in_multiline_string = True
                    multiline_quote = quote
                    new_line.append(' ' * (n - i))
                    break
                continue

            # Single/double quoted string
            if line[i] in '"\'':
                quote = line[i]
                new_line.append(line[i])
                i += 1
                while i < n:
                    if line[i] == '\\' and i + 1 < n:
                        new_line.append(line[i:i+2])
                        i += 2
                        continue
                    new_line.append(line[i])
                    if line[i] == quote:
                        i += 1
                        break
                    i += 1
                continue

            # Comment - stop processing this line
            if line[i] == '#':
                break

            new_line.append(line[i])
            i += 1

        result.append(''.join(new_line))

    return '\n'.join(result)


def extract_python_imports(content: str) -> list[str]:
    """
    Extract import statements from Python file content.

    Filters out imports in comments and docstrings.
    """
    # Pre-process to remove comments and docstrings
    processed = _strip_python_comments_and_strings(content)
    imports = []

    # import package
    for match in re.finditer(r'^import\s+(\w+)', processed, re.MULTILINE):
        imports.append(match.group(1))

    # from package import ...
    for match in re.finditer(r'^from\s+(\w+)', processed, re.MULTILINE):
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
