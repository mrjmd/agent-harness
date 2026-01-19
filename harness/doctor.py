#!/usr/bin/env python3
"""
Brownfield Doctor - Phase 0 Health Audit System

Runs BEFORE the Architect to assess project health and recommend stabilization.
Prevents building new features on top of broken foundations.

Workflow: Audit -> Stabilize -> Baseline

Key capabilities:
- Static analysis (lint, types, dependencies)
- Dynamic analysis (existing test health)
- Documentation scanning (READMEs, CHANGELOGs, known issues)
- Code annotation extraction (TODO, FIXME, HACK, etc.)
- Project type detection (Web/API/CLI/Library)
- Test strategy recommendations (Claude-driven, contextual)
- Webhook fixture scaffolding
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

# Shared CLI module
from cli import call_doctor as _call_claude_cli

# Working Memory
try:
    from memory import (
        update_understanding,
        record_decision,
        check_before_asking,
    )
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False


# =============================================================================
# Configuration
# =============================================================================

SPECS_DIR = Path("specs")
HEALTH_REPORT_PATH = SPECS_DIR / "health_report.md"
TEST_STRATEGY_PATH = SPECS_DIR / "test_strategy.md"
MANUAL_QA_PATH = SPECS_DIR / "manual_qa_plan.md"
FEATURES_PATH = SPECS_DIR / "features.json"

# Health thresholds
HEALTH_THRESHOLDS = {
    "lint_errors": {"healthy": 0, "drifting": 20, "critical": 100},
    "type_errors": {"healthy": 0, "drifting": 10, "critical": 50},
    "test_pass_rate": {"healthy": 1.0, "drifting": 0.9, "critical": 0.5},
    "dependency_age_years": {"healthy": 1, "drifting": 2, "critical": 3},
}

# Documentation patterns to scan
DOCUMENTATION_PATTERNS = [
    "README.md", "README.txt", "README",
    "CHANGELOG.md", "CHANGELOG", "HISTORY.md",
    "CONTRIBUTING.md",
    "docs/**/*.md",
    "documentation/**/*.md",
    ".github/ISSUE_TEMPLATE/**/*.md",
    "KNOWN_ISSUES.md", "BUGS.md",
    "TODO.md", "ROADMAP.md",
]

# Code annotation patterns with priorities
CODE_ANNOTATIONS = {
    # High priority - known broken
    "FIXME": {"priority": "critical", "description": "Known bug, needs fixing"},
    "BUG": {"priority": "critical", "description": "Confirmed bug"},
    "BROKEN": {"priority": "critical", "description": "Not working"},

    # Medium priority - technical debt
    "TODO": {"priority": "high", "description": "Planned work"},
    "HACK": {"priority": "high", "description": "Workaround, needs proper fix"},
    "XXX": {"priority": "high", "description": "Danger zone, needs attention"},
    "KLUDGE": {"priority": "high", "description": "Temporary solution"},

    # Context - tribal knowledge
    "@deprecated": {"priority": "medium", "description": "Should not use"},
    "REFACTOR": {"priority": "medium", "description": "Needs cleanup"},
    "OPTIMIZE": {"priority": "medium", "description": "Performance issue"},
    "SECURITY": {"priority": "critical", "description": "Security concern"},

    # Warnings
    "WARNING": {"priority": "medium", "description": "Caution needed"},
    "DANGER": {"priority": "high", "description": "High risk area"},
    "NOTE": {"priority": "low", "description": "Important context"},
}

# External service detection patterns
EXTERNAL_SERVICES = {
    "stripe": {
        "patterns": ["stripe", "STRIPE_", "payment_intent", "@stripe/stripe-js"],
        "mock_strategy": "Use stripe-mock Docker container or record fixtures from test mode",
    },
    "sendgrid": {
        "patterns": ["sendgrid", "@sendgrid", "SENDGRID_"],
        "mock_strategy": "Mock at HTTP level or use SendGrid sandbox",
    },
    "twilio": {
        "patterns": ["twilio", "TWILIO_"],
        "mock_strategy": "Use Twilio test credentials or mock HTTP",
    },
    "auth0": {
        "patterns": ["auth0", "AUTH0_", "@auth0"],
        "mock_strategy": "Use test tenant or mock JWT validation",
    },
    "aws": {
        "patterns": ["aws-sdk", "AWS_", "@aws-sdk"],
        "mock_strategy": "Use LocalStack or mock SDK calls",
    },
    "firebase": {
        "patterns": ["firebase", "FIREBASE_"],
        "mock_strategy": "Use Firebase emulator suite",
    },
    "supabase": {
        "patterns": ["supabase", "SUPABASE_"],
        "mock_strategy": "Use local Supabase or mock client",
    },
}


# =============================================================================
# Enums & Data Classes
# =============================================================================

class HealthStatus(Enum):
    HEALTHY = "healthy"
    DRIFTING = "drifting"
    CRITICAL = "critical"


class ProjectType(Enum):
    WEB_APP = "web_app"
    API = "api"
    CLI = "cli"
    LIBRARY = "library"
    UNKNOWN = "unknown"


# Test templates for different project types
TEST_TEMPLATES = {
    ProjectType.WEB_APP: {
        "language": "typescript",
        "test_dir": "tests/e2e",
        "extension": ".spec.ts",
        "run_command": "npx playwright test",
    },
    ProjectType.API: {
        "language": "python",
        "test_dir": "tests",
        "extension": "_test.py",
        "run_command": "pytest",
    },
    ProjectType.CLI: {
        "language": "python",
        "test_dir": "tests",
        "extension": "_test.py",
        "run_command": "pytest",
    },
    ProjectType.LIBRARY: {
        "language": "python",
        "test_dir": "tests",
        "extension": "_test.py",
        "run_command": "pytest",
    },
    ProjectType.UNKNOWN: {
        "language": "typescript",
        "test_dir": "tests/e2e",
        "extension": ".spec.ts",
        "run_command": "npx playwright test",
    },
}


def get_test_template(project_type: ProjectType) -> dict:
    """
    Get test template for project type, with language detection override.

    Detects actual project language from files and returns appropriate template.
    """
    is_python = (
        Path("requirements.txt").exists() or
        Path("pyproject.toml").exists() or
        Path("setup.py").exists()
    )
    is_node = Path("package.json").exists()

    # Python-only projects always use pytest
    if is_python and not is_node:
        return TEST_TEMPLATES[ProjectType.API]

    # Node-only projects use appropriate template
    if is_node and not is_python:
        if project_type == ProjectType.LIBRARY:
            # Node libraries typically use jest
            return {
                "language": "typescript",
                "test_dir": "tests",
                "extension": ".test.ts",
                "run_command": "npm test",
            }
        return TEST_TEMPLATES.get(project_type, TEST_TEMPLATES[ProjectType.WEB_APP])

    # Mixed projects - prefer the project type detection
    return TEST_TEMPLATES.get(project_type, TEST_TEMPLATES[ProjectType.WEB_APP])


@dataclass
class LintResult:
    tool: str
    error_count: int
    warning_count: int
    output: str = ""


@dataclass
class TestResult:
    framework: str
    total: int
    passed: int
    failed: int
    skipped: int
    output: str = ""

    @property
    def pass_rate(self) -> float:
        if self.total == 0:
            return 1.0
        return self.passed / self.total


@dataclass
class DependencyInfo:
    name: str
    current_version: str
    latest_version: str = ""
    age_years: float = 0.0
    has_vulnerability: bool = False
    vulnerability_severity: str = ""


@dataclass
class AnnotationMatch:
    pattern: str
    file: str
    line: int
    text: str
    priority: str


@dataclass
class DocumentedIssue:
    source: str
    issue: str
    severity: str


@dataclass
class ExternalServiceDetection:
    name: str
    files: list
    mock_strategy: str


@dataclass
class HealthReport:
    status: HealthStatus
    project_type: ProjectType
    timestamp: str

    # Static analysis
    lint_results: list = field(default_factory=list)
    type_errors: int = 0

    # Test health
    test_result: Optional[TestResult] = None

    # Dependencies
    outdated_deps: list = field(default_factory=list)
    vulnerable_deps: list = field(default_factory=list)

    # Documentation & annotations
    documented_issues: list = field(default_factory=list)
    annotations: list = field(default_factory=list)

    # External services
    external_services: list = field(default_factory=list)

    # Routes/endpoints discovered
    routes: list = field(default_factory=list)

    # Recommendations
    recommendations: list = field(default_factory=list)


# =============================================================================
# CLI Wrapper (uses shared module with streaming + read-only tools)
# =============================================================================

def call_claude_cli(prompt_text: str, timeout: int = 120) -> str:
    """
    Call claude CLI with streaming output and read-only tool access.

    Uses the shared CLI module which provides:
    - Streaming output for real-time feedback
    - Read-only tools (Read, Glob, Grep)
    - Configurable timeout (default 2 min for analysis)
    """
    try:
        return _call_claude_cli(prompt_text, timeout=timeout)
    except subprocess.CalledProcessError as e:
        print(f"Claude CLI error: {e.stderr if hasattr(e, 'stderr') else str(e)}")
        return ""
    except subprocess.TimeoutExpired:
        print(f"Claude CLI timed out after {timeout}s")
        return ""
    except SystemExit:
        # Shared module calls sys.exit on FileNotFoundError
        print("Warning: 'claude' CLI not found, skipping AI analysis")
        return ""


def run_shell(cmd: list[str], timeout: int = 60, cwd: Path = None) -> tuple[int, str, str]:
    """Run a shell command and return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or Path.cwd()
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"


# =============================================================================
# Project Type Detection
# =============================================================================

def detect_project_type() -> ProjectType:
    """
    Detect what kind of project this is based on file structure and dependencies.
    """
    signals = {
        ProjectType.WEB_APP: 0,
        ProjectType.API: 0,
        ProjectType.CLI: 0,
        ProjectType.LIBRARY: 0,
    }

    # Check for web app signals
    web_dirs = ["src/pages", "src/app", "src/routes", "pages", "app", "src/components"]
    web_deps = ["react", "vue", "angular", "svelte", "next", "nuxt", "@angular/core"]

    for d in web_dirs:
        if Path(d).exists():
            signals[ProjectType.WEB_APP] += 2

    # Check for API signals
    api_patterns = ["src/api", "routes/api", "api/", "src/routes", "src/controllers"]
    api_deps = ["express", "fastify", "koa", "hapi", "flask", "django", "fastapi"]

    for d in api_patterns:
        if Path(d).exists():
            signals[ProjectType.API] += 2

    # Check for CLI signals
    cli_patterns = ["bin/", "src/cli", "cli/"]
    cli_deps = ["commander", "yargs", "inquirer", "meow", "oclif", "click", "typer"]

    for d in cli_patterns:
        if Path(d).exists():
            signals[ProjectType.CLI] += 3

    # Check package.json for dependency signals
    if Path("package.json").exists():
        try:
            pkg = json.loads(Path("package.json").read_text())
            all_deps = {
                **pkg.get("dependencies", {}),
                **pkg.get("devDependencies", {})
            }

            for dep in web_deps:
                if dep in all_deps:
                    signals[ProjectType.WEB_APP] += 2

            for dep in api_deps:
                if dep in all_deps:
                    signals[ProjectType.API] += 2

            for dep in cli_deps:
                if dep in all_deps:
                    signals[ProjectType.CLI] += 2

            # Check for bin field (CLI)
            if "bin" in pkg:
                signals[ProjectType.CLI] += 3

            # Check for main/exports without bin (library)
            if ("main" in pkg or "exports" in pkg) and "bin" not in pkg:
                signals[ProjectType.LIBRARY] += 1

        except (json.JSONDecodeError, IOError):
            pass

    # Check requirements.txt / pyproject.toml for Python projects
    if Path("requirements.txt").exists() or Path("pyproject.toml").exists():
        try:
            content = ""
            if Path("requirements.txt").exists():
                content = Path("requirements.txt").read_text()
            elif Path("pyproject.toml").exists():
                content = Path("pyproject.toml").read_text()

            for dep in api_deps:
                if dep in content.lower():
                    signals[ProjectType.API] += 2

            for dep in cli_deps:
                if dep in content.lower():
                    signals[ProjectType.CLI] += 2

        except IOError:
            pass

    # Return highest scoring type
    max_score = max(signals.values())
    if max_score == 0:
        return ProjectType.UNKNOWN

    for ptype, score in signals.items():
        if score == max_score:
            return ptype

    return ProjectType.UNKNOWN


# =============================================================================
# Static Analysis
# =============================================================================

def discover_lint_command() -> Optional[list[str]]:
    """
    Discover the lint command from package.json scripts.

    Looks for common lint script names and returns the npm command to run it.
    Returns None if no lint script is found.
    """
    package_json = Path("package.json")
    if not package_json.exists():
        return None

    try:
        pkg = json.loads(package_json.read_text())
        scripts = pkg.get("scripts", {})

        # Priority order for lint script names
        lint_names = ["lint", "eslint", "lint:check", "lint:all", "check:lint"]

        for name in lint_names:
            if name in scripts:
                # Check if the script already outputs JSON
                script_cmd = scripts[name]
                if "--format=json" in script_cmd or "-f json" in script_cmd:
                    return ["npm", "run", name]
                else:
                    return ["npm", "run", name, "--", "--format=json"]

        # Fallback: check if any script contains "eslint" in the command
        for name, cmd in scripts.items():
            if "eslint" in cmd.lower() and "lint" in name.lower():
                return ["npm", "run", name, "--", "--format=json"]

        return None
    except (json.JSONDecodeError, KeyError):
        return None


def audit_lint_health() -> list[LintResult]:
    """Run available linters and collect results."""
    results = []

    # Try ESLint - discover command dynamically
    if Path("package.json").exists():
        lint_cmd = discover_lint_command()
        if lint_cmd:
            code, stdout, stderr = run_shell(lint_cmd, timeout=120)
            if code != -1:  # Command exists
                error_count = stdout.count('"severity":2') + stderr.count('"severity":2')
                warning_count = stdout.count('"severity":1') + stderr.count('"severity":1')
                results.append(LintResult(
                    tool="eslint",
                    error_count=error_count,
                    warning_count=warning_count,
                    output=(stdout + stderr)[:2000]
                ))

    # Try TypeScript
    if Path("tsconfig.json").exists():
        code, stdout, stderr = run_shell(["npx", "tsc", "--noEmit"], timeout=120)
        if code != -1:
            # Count "error TS" occurrences
            error_count = len(re.findall(r"error TS\d+", stdout + stderr))
            results.append(LintResult(
                tool="typescript",
                error_count=error_count,
                warning_count=0,
                output=(stdout + stderr)[:2000]
            ))

    # Try Python linters
    if Path("requirements.txt").exists() or Path("pyproject.toml").exists():
        # Ruff
        code, stdout, stderr = run_shell(["ruff", "check", ".", "--format=json"], timeout=60)
        if code != -1:
            try:
                issues = json.loads(stdout) if stdout else []
                results.append(LintResult(
                    tool="ruff",
                    error_count=len(issues),
                    warning_count=0,
                    output=stdout[:2000]
                ))
            except json.JSONDecodeError:
                pass

    return results


def audit_type_health() -> int:
    """Run type checker and count errors."""
    # TypeScript
    if Path("tsconfig.json").exists():
        code, stdout, stderr = run_shell(["npx", "tsc", "--noEmit"], timeout=120)
        return len(re.findall(r"error TS\d+", stdout + stderr))

    # Python mypy
    if Path("pyproject.toml").exists() or Path("mypy.ini").exists():
        code, stdout, stderr = run_shell(["mypy", ".", "--no-error-summary"], timeout=120)
        return len(re.findall(r": error:", stdout + stderr))

    return 0


def audit_test_health() -> Optional[TestResult]:
    """Run existing test suite and measure health."""
    # Try npm test
    if Path("package.json").exists():
        try:
            pkg = json.loads(Path("package.json").read_text())
            if "test" in pkg.get("scripts", {}):
                code, stdout, stderr = run_shell(["npm", "test", "--", "--passWithNoTests"], timeout=300)

                # Parse Jest/Vitest output
                output = stdout + stderr

                # Look for summary line: "Tests: X passed, Y failed, Z total"
                match = re.search(r"(\d+) passed.*?(\d+) failed.*?(\d+) total", output)
                if match:
                    passed, failed, total = int(match.group(1)), int(match.group(2)), int(match.group(3))
                    return TestResult(
                        framework="jest/vitest",
                        total=total,
                        passed=passed,
                        failed=failed,
                        skipped=total - passed - failed,
                        output=output[:3000]
                    )

                # Playwright format
                match = re.search(r"(\d+) passed.*?(\d+) failed", output)
                if match:
                    passed, failed = int(match.group(1)), int(match.group(2))
                    return TestResult(
                        framework="playwright",
                        total=passed + failed,
                        passed=passed,
                        failed=failed,
                        skipped=0,
                        output=output[:3000]
                    )

        except (json.JSONDecodeError, IOError):
            pass

    # Try pytest
    if Path("pytest.ini").exists() or Path("pyproject.toml").exists():
        code, stdout, stderr = run_shell(["pytest", "--tb=no", "-q"], timeout=300)
        output = stdout + stderr

        # Parse pytest output: "X passed, Y failed"
        match = re.search(r"(\d+) passed", output)
        passed = int(match.group(1)) if match else 0

        match = re.search(r"(\d+) failed", output)
        failed = int(match.group(1)) if match else 0

        if passed + failed > 0:
            return TestResult(
                framework="pytest",
                total=passed + failed,
                passed=passed,
                failed=failed,
                skipped=0,
                output=output[:3000]
            )

    return None


# =============================================================================
# Dependency Analysis
# =============================================================================

def audit_dependencies() -> tuple[list[DependencyInfo], list[DependencyInfo]]:
    """Check for outdated and vulnerable dependencies."""
    outdated = []
    vulnerable = []

    # npm outdated
    if Path("package.json").exists():
        code, stdout, stderr = run_shell(["npm", "outdated", "--json"], timeout=60)
        if stdout:
            try:
                data = json.loads(stdout)
                for name, info in data.items():
                    outdated.append(DependencyInfo(
                        name=name,
                        current_version=info.get("current", ""),
                        latest_version=info.get("latest", "")
                    ))
            except json.JSONDecodeError:
                pass

        # npm audit
        code, stdout, stderr = run_shell(["npm", "audit", "--json"], timeout=60)
        if stdout:
            try:
                data = json.loads(stdout)
                for vuln_id, vuln in data.get("vulnerabilities", {}).items():
                    vulnerable.append(DependencyInfo(
                        name=vuln_id,
                        current_version="",
                        has_vulnerability=True,
                        vulnerability_severity=vuln.get("severity", "unknown")
                    ))
            except json.JSONDecodeError:
                pass

    # pip-audit for Python
    if Path("requirements.txt").exists():
        code, stdout, stderr = run_shell(["pip-audit", "--format=json"], timeout=60)
        if stdout:
            try:
                data = json.loads(stdout)
                for vuln in data:
                    vulnerable.append(DependencyInfo(
                        name=vuln.get("name", ""),
                        current_version=vuln.get("version", ""),
                        has_vulnerability=True,
                        vulnerability_severity=vuln.get("vulns", [{}])[0].get("severity", "unknown")
                    ))
            except json.JSONDecodeError:
                pass

    return outdated, vulnerable


# =============================================================================
# Documentation & Annotation Scanning
# =============================================================================

def scan_documentation() -> list[DocumentedIssue]:
    """Scan documentation files for known issues."""
    issues = []

    # Patterns that indicate problems
    issue_patterns = [
        (r"(?i)known\s+(?:issues?|bugs?|problems?)", "critical"),
        (r"(?i)(?:currently\s+)?broken", "critical"),
        (r"(?i)not\s+(?:yet\s+)?(?:implemented|working)", "high"),
        (r"(?i)work\s*(?:ing)?\s*in\s*progress|WIP", "medium"),
        (r"(?i)deprecated", "medium"),
        (r"(?i)todo|fixme", "high"),
        (r"(?i)breaking\s+change", "high"),
        (r"(?i)security\s+(?:issue|vulnerability|concern)", "critical"),
    ]

    # Find documentation files
    for pattern in DOCUMENTATION_PATTERNS:
        if "*" in pattern:
            # Glob pattern
            for doc_file in Path(".").glob(pattern):
                if doc_file.is_file():
                    issues.extend(_scan_doc_file(doc_file, issue_patterns))
        else:
            doc_file = Path(pattern)
            if doc_file.exists() and doc_file.is_file():
                issues.extend(_scan_doc_file(doc_file, issue_patterns))

    return issues


def _scan_doc_file(file_path: Path, patterns: list) -> list[DocumentedIssue]:
    """Scan a single documentation file for issues."""
    issues = []
    try:
        content = file_path.read_text(errors="ignore")
        lines = content.split("\n")

        for i, line in enumerate(lines):
            for pattern, severity in patterns:
                if re.search(pattern, line):
                    # Get some context
                    context_start = max(0, i - 1)
                    context_end = min(len(lines), i + 2)
                    context = "\n".join(lines[context_start:context_end])

                    issues.append(DocumentedIssue(
                        source=f"{file_path}:{i+1}",
                        issue=context[:200],
                        severity=severity
                    ))
                    break  # One match per line

    except IOError:
        pass

    return issues


def scan_code_annotations() -> list[AnnotationMatch]:
    """Scan code files for TODO, FIXME, HACK, etc."""
    annotations = []

    # File extensions to scan
    extensions = [".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs", ".java", ".rb"]

    # Directories to skip
    skip_dirs = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", "venv", "harness"}

    for ext in extensions:
        for file_path in Path(".").rglob(f"*{ext}"):
            # Skip excluded directories
            if any(skip in file_path.parts for skip in skip_dirs):
                continue

            try:
                content = file_path.read_text(errors="ignore")
                lines = content.split("\n")

                for i, line in enumerate(lines):
                    for pattern, info in CODE_ANNOTATIONS.items():
                        # Match pattern as word boundary
                        if re.search(rf"\b{re.escape(pattern)}\b", line, re.IGNORECASE):
                            annotations.append(AnnotationMatch(
                                pattern=pattern,
                                file=str(file_path),
                                line=i + 1,
                                text=line.strip()[:100],
                                priority=info["priority"]
                            ))
                            break  # One match per line

            except IOError:
                pass

    return annotations


# =============================================================================
# External Service Detection
# =============================================================================

def detect_external_services() -> list[ExternalServiceDetection]:
    """Detect external service integrations that need mocking."""
    detected = []

    # File extensions to scan
    extensions = [".ts", ".tsx", ".js", ".jsx", ".py", ".env", ".env.example"]
    skip_dirs = {"node_modules", ".git", "dist", "build", "harness"}

    for service_name, service_info in EXTERNAL_SERVICES.items():
        files_found = set()

        for ext in extensions:
            for file_path in Path(".").rglob(f"*{ext}"):
                if any(skip in file_path.parts for skip in skip_dirs):
                    continue

                try:
                    content = file_path.read_text(errors="ignore").lower()
                    for pattern in service_info["patterns"]:
                        if pattern.lower() in content:
                            files_found.add(str(file_path))
                            break
                except IOError:
                    pass

        if files_found:
            detected.append(ExternalServiceDetection(
                name=service_name,
                files=sorted(files_found),
                mock_strategy=service_info["mock_strategy"]
            ))

    return detected


# =============================================================================
# Route Discovery
# =============================================================================

def discover_routes() -> list[dict]:
    """Discover API routes and page routes from code."""
    routes = []

    # Next.js App Router
    app_dir = Path("src/app") if Path("src/app").exists() else Path("app")
    if app_dir.exists():
        for route_file in app_dir.rglob("*/route.ts"):
            route_path = "/" + "/".join(route_file.parent.relative_to(app_dir).parts)
            routes.append({"path": route_path, "type": "api", "file": str(route_file)})

        for page_file in app_dir.rglob("*/page.tsx"):
            route_path = "/" + "/".join(page_file.parent.relative_to(app_dir).parts)
            route_path = route_path.replace("/page", "")
            routes.append({"path": route_path or "/", "type": "page", "file": str(page_file)})

    # Next.js Pages Router
    pages_dir = Path("src/pages") if Path("src/pages").exists() else Path("pages")
    if pages_dir.exists():
        for page_file in pages_dir.rglob("*.tsx"):
            if page_file.name.startswith("_"):
                continue
            route_path = "/" + str(page_file.relative_to(pages_dir)).replace(".tsx", "").replace("index", "")
            routes.append({"path": route_path.rstrip("/") or "/", "type": "page", "file": str(page_file)})

        for api_file in (pages_dir / "api").rglob("*.ts") if (pages_dir / "api").exists() else []:
            route_path = "/api/" + str(api_file.relative_to(pages_dir / "api")).replace(".ts", "")
            routes.append({"path": route_path, "type": "api", "file": str(api_file)})

    # Express/Fastify routes (basic detection)
    for route_file in Path(".").rglob("**/routes/**/*.ts"):
        if "node_modules" in str(route_file):
            continue
        routes.append({"path": f"/{route_file.stem}", "type": "api", "file": str(route_file)})

    # Python Flask/FastAPI routes
    for py_file in Path(".").rglob("**/*.py"):
        if any(skip in str(py_file) for skip in ["venv", ".venv", "__pycache__"]):
            continue
        try:
            content = py_file.read_text(errors="ignore")
            # Flask: @app.route("/path")
            for match in re.finditer(r'@\w+\.(?:route|get|post|put|delete)\(["\']([^"\']+)["\']', content):
                routes.append({"path": match.group(1), "type": "api", "file": str(py_file)})
        except IOError:
            pass

    return routes


# =============================================================================
# Health Status Calculation
# =============================================================================

def calculate_health_status(report: HealthReport) -> HealthStatus:
    """Calculate overall health status from report data."""
    critical_flags = 0
    drifting_flags = 0

    # Check lint errors
    total_lint_errors = sum(r.error_count for r in report.lint_results)
    if total_lint_errors >= HEALTH_THRESHOLDS["lint_errors"]["critical"]:
        critical_flags += 1
    elif total_lint_errors >= HEALTH_THRESHOLDS["lint_errors"]["drifting"]:
        drifting_flags += 1

    # Check type errors
    if report.type_errors >= HEALTH_THRESHOLDS["type_errors"]["critical"]:
        critical_flags += 1
    elif report.type_errors >= HEALTH_THRESHOLDS["type_errors"]["drifting"]:
        drifting_flags += 1

    # Check test pass rate
    if report.test_result:
        if report.test_result.pass_rate < HEALTH_THRESHOLDS["test_pass_rate"]["critical"]:
            critical_flags += 1
        elif report.test_result.pass_rate < HEALTH_THRESHOLDS["test_pass_rate"]["drifting"]:
            drifting_flags += 1

    # Check vulnerabilities
    high_vulns = sum(1 for v in report.vulnerable_deps if v.vulnerability_severity in ["high", "critical"])
    if high_vulns > 0:
        critical_flags += 1

    # Check critical annotations (SECURITY, BROKEN, etc.)
    critical_annotations = sum(1 for a in report.annotations if a.priority == "critical")
    if critical_annotations > 0:
        critical_flags += 1

    # Check documented critical issues
    critical_docs = sum(1 for d in report.documented_issues if d.severity == "critical")
    if critical_docs > 0:
        critical_flags += 1

    if critical_flags > 0:
        return HealthStatus.CRITICAL
    elif drifting_flags > 0:
        return HealthStatus.DRIFTING
    return HealthStatus.HEALTHY


# =============================================================================
# Report Generation
# =============================================================================

def generate_health_report(report: HealthReport) -> str:
    """Generate markdown health report."""
    lines = [
        "# Project Health Report",
        "",
        f"**Generated:** {report.timestamp}",
        f"**Project Type:** {report.project_type.value}",
        f"**Overall Status:** {_status_emoji(report.status)} {report.status.value.upper()}",
        "",
        "---",
        "",
    ]

    # Static Analysis
    lines.extend([
        "## Static Analysis",
        "",
        "| Tool | Errors | Warnings |",
        "|------|--------|----------|",
    ])
    for lint in report.lint_results:
        lines.append(f"| {lint.tool} | {lint.error_count} | {lint.warning_count} |")

    if report.type_errors > 0:
        lines.append(f"| TypeScript | {report.type_errors} | - |")
    lines.append("")

    # Test Health
    lines.extend(["## Test Health", ""])
    if report.test_result:
        tr = report.test_result
        lines.extend([
            f"**Framework:** {tr.framework}",
            f"**Pass Rate:** {tr.pass_rate:.1%}",
            "",
            f"| Passed | Failed | Skipped | Total |",
            f"|--------|--------|---------|-------|",
            f"| {tr.passed} | {tr.failed} | {tr.skipped} | {tr.total} |",
            "",
        ])
    else:
        lines.append("*No test suite detected or tests failed to run.*\n")

    # Dependencies
    if report.outdated_deps or report.vulnerable_deps:
        lines.extend(["## Dependencies", ""])

        if report.vulnerable_deps:
            lines.extend([
                "### Vulnerabilities",
                "",
                "| Package | Severity |",
                "|---------|----------|",
            ])
            for dep in report.vulnerable_deps[:10]:
                lines.append(f"| {dep.name} | {dep.vulnerability_severity} |")
            lines.append("")

        if report.outdated_deps:
            lines.extend([
                "### Outdated",
                "",
                f"*{len(report.outdated_deps)} outdated packages detected.*",
                "",
            ])

    # Documented Issues
    if report.documented_issues:
        lines.extend([
            "## Documented Issues",
            "",
            "| Source | Issue | Severity |",
            "|--------|-------|----------|",
        ])
        for issue in report.documented_issues[:15]:
            escaped_issue = issue.issue.replace("|", "\\|").replace("\n", " ")[:80]
            lines.append(f"| {issue.source} | {escaped_issue} | {issue.severity} |")
        lines.append("")

    # Code Annotations
    if report.annotations:
        lines.extend([
            "## Code Annotations",
            "",
        ])

        # Group by pattern
        by_pattern = {}
        for ann in report.annotations:
            if ann.pattern not in by_pattern:
                by_pattern[ann.pattern] = []
            by_pattern[ann.pattern].append(ann)

        lines.extend([
            "| Pattern | Count | Priority | Sample |",
            "|---------|-------|----------|--------|",
        ])
        for pattern, anns in sorted(by_pattern.items(), key=lambda x: -len(x[1])):
            sample = anns[0]
            lines.append(f"| {pattern} | {len(anns)} | {sample.priority} | {sample.file}:{sample.line} |")
        lines.append("")

    # External Services
    if report.external_services:
        lines.extend([
            "## External Services Detected",
            "",
            "| Service | Files | Mock Strategy |",
            "|---------|-------|---------------|",
        ])
        for svc in report.external_services:
            files_str = ", ".join(svc.files[:3])
            if len(svc.files) > 3:
                files_str += f" (+{len(svc.files) - 3} more)"
            lines.append(f"| {svc.name} | {files_str} | {svc.mock_strategy[:50]}... |")
        lines.append("")

    # Routes
    if report.routes:
        lines.extend([
            "## Discovered Routes",
            "",
            "| Path | Type | File |",
            "|------|------|------|",
        ])
        for route in report.routes[:20]:
            lines.append(f"| {route['path']} | {route['type']} | {route['file']} |")
        if len(report.routes) > 20:
            lines.append(f"| ... | ... | *({len(report.routes) - 20} more routes)* |")
        lines.append("")

    # Recommendations
    lines.extend([
        "## Recommendations",
        "",
    ])
    for i, rec in enumerate(report.recommendations, 1):
        lines.append(f"{i}. {rec}")
    lines.append("")

    return "\n".join(lines)


def _status_emoji(status: HealthStatus) -> str:
    return {
        HealthStatus.HEALTHY: "\U0001F7E2",   # Green circle
        HealthStatus.DRIFTING: "\U0001F7E1",  # Yellow circle
        HealthStatus.CRITICAL: "\U0001F534",   # Red circle
    }.get(status, "?")


# =============================================================================
# Stabilization Task Generation
# =============================================================================

def generate_stabilization_tasks(report: HealthReport) -> list[dict]:
    """Generate features.json tasks for stabilization."""
    tasks = []
    priority = 0

    # Critical lint errors
    total_lint = sum(r.error_count for r in report.lint_results)
    if total_lint >= HEALTH_THRESHOLDS["lint_errors"]["drifting"]:
        tasks.append({
            "id": "stabilize-001-lint-errors",
            "description": f"Fix {total_lint} linting errors to establish clean baseline",
            "acceptance_criteria": "npm run lint exits with code 0",
            "edge_cases": [
                {"id": "partial-fix", "description": "Some errors remain", "expected_behavior": "Re-run until all fixed"},
                {"id": "new-errors", "description": "Fixing one reveals others", "expected_behavior": "Continue until clean"},
                {"id": "unfixable", "description": "Some require config changes", "expected_behavior": "Update eslint config if needed"},
            ],
            "priority": priority,
            "status": "todo",
            "constraints": {"behavior_change": False}
        })
        priority += 1

    # Type errors
    if report.type_errors >= HEALTH_THRESHOLDS["type_errors"]["drifting"]:
        tasks.append({
            "id": "stabilize-002-type-errors",
            "description": f"Fix {report.type_errors} TypeScript errors",
            "acceptance_criteria": "npx tsc --noEmit exits with code 0",
            "edge_cases": [
                {"id": "any-escape", "description": "Tempted to use 'any'", "expected_behavior": "Use proper types, 'any' only as last resort"},
                {"id": "missing-types", "description": "Third-party types missing", "expected_behavior": "Install @types/* packages"},
                {"id": "strict-mode", "description": "Errors from strict settings", "expected_behavior": "Fix properly, don't disable strict"},
            ],
            "priority": priority,
            "status": "todo",
            "constraints": {"behavior_change": False}
        })
        priority += 1

    # Failing tests
    if report.test_result and report.test_result.failed > 0:
        tasks.append({
            "id": "stabilize-003-failing-tests",
            "description": f"Fix {report.test_result.failed} failing tests",
            "acceptance_criteria": "npm test passes with 100% pass rate",
            "edge_cases": [
                {"id": "flaky", "description": "Test passes sometimes", "expected_behavior": "Fix race condition or add retry"},
                {"id": "outdated", "description": "Test tests old behavior", "expected_behavior": "Update test to match current behavior"},
                {"id": "env-dependent", "description": "Fails in CI only", "expected_behavior": "Make test environment-agnostic"},
            ],
            "priority": priority,
            "status": "todo",
            "constraints": {"behavior_change": False}
        })
        priority += 1

    # Critical security annotations
    security_anns = [a for a in report.annotations if a.pattern == "SECURITY" or (a.pattern == "HACK" and "auth" in a.text.lower())]
    if security_anns:
        tasks.append({
            "id": "stabilize-004-security-issues",
            "description": f"Address {len(security_anns)} security-related code annotations",
            "acceptance_criteria": "No SECURITY or auth-related HACK annotations remain",
            "edge_cases": [
                {"id": "false-positive", "description": "Annotation is outdated", "expected_behavior": "Remove annotation after verification"},
                {"id": "complex-fix", "description": "Requires architecture change", "expected_behavior": "Document and create separate feature"},
                {"id": "third-party", "description": "Issue in dependency", "expected_behavior": "Update dependency or document workaround"},
            ],
            "priority": priority,
            "status": "todo",
            "constraints": {"behavior_change": False}
        })
        priority += 1

    # High vulnerabilities
    high_vulns = [v for v in report.vulnerable_deps if v.vulnerability_severity in ["high", "critical"]]
    if high_vulns:
        tasks.append({
            "id": "stabilize-005-vulnerabilities",
            "description": f"Address {len(high_vulns)} high/critical dependency vulnerabilities",
            "acceptance_criteria": "npm audit shows no high/critical vulnerabilities",
            "edge_cases": [
                {"id": "breaking-update", "description": "Fix requires major version bump", "expected_behavior": "Update and fix breaking changes"},
                {"id": "no-fix", "description": "No patched version available", "expected_behavior": "Document risk and monitor for fix"},
                {"id": "dev-only", "description": "Vulnerability in devDependency", "expected_behavior": "Lower priority but still fix"},
            ],
            "priority": priority,
            "status": "todo",
            "constraints": {"behavior_change": False}
        })

    return tasks


# =============================================================================
# Test Strategy Recommendation
# =============================================================================

def recommend_test_strategy(report: HealthReport) -> str:
    """Use Claude to recommend test strategy based on project analysis."""

    # Build context for Claude
    context = f"""
Project Type: {report.project_type.value}

Existing Test Framework: {report.test_result.framework if report.test_result else "None detected"}

External Services: {", ".join(s.name for s in report.external_services) or "None"}

Routes Discovered: {len(report.routes)} ({sum(1 for r in report.routes if r['type'] == 'api')} API, {sum(1 for r in report.routes if r['type'] == 'page')} pages)

Current Test Health: {f"{report.test_result.pass_rate:.0%} pass rate" if report.test_result else "No tests"}
"""

    prompt = f"""Based on this project analysis, recommend a test strategy.

{context}

Provide recommendations for:
1. Which test framework(s) to use (and why)
2. What types of tests are most important for this project
3. How to handle the external service integrations
4. What baseline tests to write first
5. Any setup steps required

Be specific and actionable. Consider what already exists.
Format as markdown."""

    strategy = call_claude_cli(prompt, timeout=60)

    if not strategy:
        # Fallback to basic recommendations
        strategy = _fallback_test_strategy(report)

    return strategy


def _fallback_test_strategy(report: HealthReport) -> str:
    """Generate basic test strategy without Claude."""
    lines = [
        "# Test Strategy Recommendations",
        "",
        f"## Project Type: {report.project_type.value}",
        "",
    ]

    if report.project_type == ProjectType.WEB_APP:
        lines.extend([
            "### Recommended Approach",
            "- **E2E Tests**: Playwright for critical user journeys",
            "- **Component Tests**: Vitest + Testing Library for UI components",
            "- **API Tests**: Supertest for API routes",
            "",
        ])
    elif report.project_type == ProjectType.API:
        lines.extend([
            "### Recommended Approach",
            "- **API Tests**: Supertest or similar HTTP testing",
            "- **Contract Tests**: Validate against OpenAPI spec if available",
            "- **Integration Tests**: Test with mocked external services",
            "",
        ])

    if report.external_services:
        lines.extend([
            "### External Service Mocking",
            "",
        ])
        for svc in report.external_services:
            lines.append(f"- **{svc.name}**: {svc.mock_strategy}")
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# Webhook Fixture Scaffolding
# =============================================================================

def scaffold_webhook_fixtures(report: HealthReport) -> None:
    """Create fixture directory structure for webhook testing."""

    fixtures_dir = Path("tests/fixtures/webhooks")

    for service in report.external_services:
        service_dir = fixtures_dir / service.name
        service_dir.mkdir(parents=True, exist_ok=True)

        # Create README with recording instructions
        readme_path = service_dir / "README.md"
        if not readme_path.exists():
            readme_content = f"""# {service.name.title()} Webhook Fixtures

## Recording Instructions

{_get_recording_instructions(service.name)}

## Usage

Place recorded webhook payloads in this directory as JSON files.
Name them descriptively: `event_type.json` (e.g., `payment_intent.succeeded.json`)

## Files

Add your recorded fixtures here.
"""
            readme_path.write_text(readme_content)

    # Detect project type for language-aware test file
    project_type = detect_project_type()
    template = get_test_template(project_type)

    # Create base test file - language aware
    test_dir = Path("tests/webhooks")
    test_dir.mkdir(parents=True, exist_ok=True)

    if template["language"] == "python":
        baseline_path = test_dir / "test_baseline.py"
        if not baseline_path.exists():
            baseline_content = '''"""
Baseline Webhook Tests - Python/pytest version

Usage: pytest tests/webhooks/test_baseline.py
"""
import json
import os
from pathlib import Path
import pytest
import requests

FIXTURES_DIR = Path("tests/fixtures/webhooks")
BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")


def load_fixtures(service: str) -> list[dict]:
    """Load fixture files for a service."""
    service_dir = FIXTURES_DIR / service
    if not service_dir.exists():
        return []

    fixtures = []
    for file in service_dir.glob("*.json"):
        with open(file) as f:
            fixtures.append({
                "name": file.stem,
                "payload": json.load(f)
            })
    return fixtures


class TestWebhookBaseline:
    """Baseline webhook tests - add your webhook endpoint tests here."""

    @pytest.mark.skip(reason="placeholder - add webhook tests after recording fixtures")
    def test_placeholder(self):
        pass

    # Example:
    # @pytest.fixture
    # def stripe_fixtures(self):
    #     return load_fixtures("stripe")
    #
    # def test_stripe_webhook(self, stripe_fixtures):
    #     for fixture in stripe_fixtures:
    #         response = requests.post(
    #             f"{BASE_URL}/api/webhooks/stripe",
    #             json=fixture["payload"],
    #             headers={"stripe-signature": "test_signature"}
    #         )
    #         assert response.ok
'''
            baseline_path.write_text(baseline_content)
    else:
        # TypeScript/Playwright version
        baseline_path = test_dir / "baseline.spec.ts"
        if not baseline_path.exists():
            baseline_content = '''import { test, expect } from "@playwright/test";
import { readFileSync, readdirSync } from "fs";
import { join } from "path";

const FIXTURES_DIR = "tests/fixtures/webhooks";

// Dynamically load all fixture files
function loadFixtures(service: string): Array<{ name: string; payload: unknown }> {
  const serviceDir = join(FIXTURES_DIR, service);
  try {
    const files = readdirSync(serviceDir).filter(f => f.endsWith(".json"));
    return files.map(file => ({
      name: file.replace(".json", ""),
      payload: JSON.parse(readFileSync(join(serviceDir, file), "utf-8"))
    }));
  } catch {
    return [];
  }
}

test.describe("Webhook Baseline Tests", () => {
  // Add your webhook endpoint tests here
  // Example:
  //
  // const stripeFixtures = loadFixtures("stripe");
  // for (const fixture of stripeFixtures) {
  //   test(`handles stripe ${fixture.name}`, async ({ request }) => {
  //     const response = await request.post("/api/webhooks/stripe", {
  //       data: fixture.payload,
  //       headers: {
  //         "stripe-signature": "test_signature"
  //       }
  //     });
  //     expect(response.ok()).toBeTruthy();
  //   });
  // }

  test.skip("placeholder - add webhook tests after recording fixtures", () => {
    // Remove this test once you have real fixtures
  });
});
'''
            baseline_path.write_text(baseline_content)

    print(f"Created fixture structure at {fixtures_dir}")
    print(f"Created baseline test at {baseline_path}")


def _get_recording_instructions(service: str) -> str:
    """Get recording instructions for a specific service."""
    instructions = {
        "stripe": """
1. Go to Stripe Dashboard > Developers > Webhooks
2. Select your endpoint or create a test endpoint
3. Click "Send test webhook" for each event type you handle
4. Copy the payload from the webhook log
5. Save as `event_type.json` (e.g., `payment_intent.succeeded.json`)

Alternatively, use Stripe CLI:
```bash
stripe listen --forward-to localhost:3000/api/webhooks/stripe
stripe trigger payment_intent.succeeded
```
""",
        "sendgrid": """
1. Use SendGrid's Event Webhook documentation for payload formats
2. Set up a temporary endpoint to capture real events
3. Or use SendGrid's test payloads from their API docs
""",
        "auth0": """
1. Go to Auth0 Dashboard > Monitoring > Logs
2. Trigger authentication events in your test environment
3. Copy the hook payloads from the logs
""",
    }
    return instructions.get(service, """
1. Trigger real webhook events in your test/staging environment
2. Capture the payloads using logging or a tool like ngrok
3. Save the raw JSON payloads to this directory
""")


# =============================================================================
# CLI Commands
# =============================================================================

def cmd_diagnose() -> int:
    """Run full health audit and generate report."""
    print("Running health diagnosis...")
    print("")

    # Detect project type
    print("  Detecting project type...")
    project_type = detect_project_type()
    print(f"    -> {project_type.value}")

    # Run static analysis
    print("  Running static analysis...")
    lint_results = audit_lint_health()
    type_errors = audit_type_health()
    print(f"    -> {sum(r.error_count for r in lint_results)} lint errors, {type_errors} type errors")

    # Run tests
    print("  Checking test health...")
    test_result = audit_test_health()
    if test_result:
        print(f"    -> {test_result.pass_rate:.0%} pass rate ({test_result.passed}/{test_result.total})")
    else:
        print("    -> No tests detected")

    # Check dependencies
    print("  Auditing dependencies...")
    outdated, vulnerable = audit_dependencies()
    print(f"    -> {len(outdated)} outdated, {len(vulnerable)} vulnerable")

    # Scan documentation
    print("  Scanning documentation...")
    doc_issues = scan_documentation()
    print(f"    -> {len(doc_issues)} documented issues found")

    # Scan code annotations
    print("  Scanning code annotations...")
    annotations = scan_code_annotations()
    print(f"    -> {len(annotations)} annotations found")

    # Detect external services
    print("  Detecting external services...")
    external_services = detect_external_services()
    print(f"    -> {len(external_services)} services detected")

    # Discover routes
    print("  Discovering routes...")
    routes = discover_routes()
    print(f"    -> {len(routes)} routes found")

    # Build report
    report = HealthReport(
        status=HealthStatus.HEALTHY,  # Will be recalculated
        project_type=project_type,
        timestamp=datetime.now(timezone.utc).isoformat(),
        lint_results=lint_results,
        type_errors=type_errors,
        test_result=test_result,
        outdated_deps=outdated,
        vulnerable_deps=vulnerable,
        documented_issues=doc_issues,
        annotations=annotations,
        external_services=external_services,
        routes=routes,
    )

    # Calculate status
    report.status = calculate_health_status(report)

    # Generate recommendations
    report.recommendations = _generate_recommendations(report)

    # Write report
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    report_content = generate_health_report(report)
    HEALTH_REPORT_PATH.write_text(report_content)

    print("")
    print(f"{_status_emoji(report.status)} Overall Status: {report.status.value.upper()}")
    print(f"Report saved to: {HEALTH_REPORT_PATH}")

    # Update working memory with diagnosis findings
    if MEMORY_AVAILABLE:
        update_understanding("doctor", "project_type", project_type.value)
        update_understanding("doctor", "health_status", report.status.value)
        update_understanding("doctor", "lint_errors", str(sum(r.error_count for r in lint_results)))
        update_understanding("doctor", "type_errors", str(type_errors))
        if test_result:
            update_understanding("doctor", "test_pass_rate", f"{test_result.pass_rate:.0%}")
        update_understanding("doctor", "external_services", ", ".join(s.name for s in external_services) if external_services else "none")
        update_understanding("doctor", "route_count", str(len(routes)))

    if report.status == HealthStatus.CRITICAL:
        print("")
        print("RECOMMENDATION: Run 'python harness/doctor.py stabilize' to generate fix tasks.")

    return 0 if report.status == HealthStatus.HEALTHY else 1


def _generate_recommendations(report: HealthReport) -> list[str]:
    """Generate actionable recommendations from report."""
    recs = []

    total_lint = sum(r.error_count for r in report.lint_results)
    if total_lint > 0:
        recs.append(f"Fix {total_lint} lint errors to establish clean baseline")

    if report.type_errors > 0:
        recs.append(f"Fix {report.type_errors} type errors before adding features")

    if report.test_result and report.test_result.failed > 0:
        recs.append(f"Fix {report.test_result.failed} failing tests to enable regression fence")

    if not report.test_result:
        recs.append("Set up test infrastructure - no test suite detected")

    high_vulns = [v for v in report.vulnerable_deps if v.vulnerability_severity in ["high", "critical"]]
    if high_vulns:
        recs.append(f"Address {len(high_vulns)} high/critical security vulnerabilities")

    critical_anns = [a for a in report.annotations if a.priority == "critical"]
    if critical_anns:
        recs.append(f"Review {len(critical_anns)} critical code annotations (FIXME, SECURITY, etc.)")

    if report.external_services:
        recs.append(f"Set up mocking for {len(report.external_services)} external services before testing")

    if not recs:
        recs.append("Project is healthy - ready for feature development!")

    return recs


def cmd_stabilize() -> int:
    """Generate stabilization tasks from health report."""
    if not HEALTH_REPORT_PATH.exists():
        print("No health report found. Running diagnosis first...")
        cmd_diagnose()

    # Re-run analysis to get fresh data
    print("Analyzing project for stabilization tasks...")

    report = HealthReport(
        status=HealthStatus.HEALTHY,
        project_type=detect_project_type(),
        timestamp=datetime.now(timezone.utc).isoformat(),
        lint_results=audit_lint_health(),
        type_errors=audit_type_health(),
        test_result=audit_test_health(),
        outdated_deps=[],
        vulnerable_deps=audit_dependencies()[1],
        annotations=scan_code_annotations(),
    )
    report.status = calculate_health_status(report)

    tasks = generate_stabilization_tasks(report)

    if not tasks:
        print("No stabilization tasks needed - project is healthy!")
        return 0

    print(f"Generated {len(tasks)} stabilization tasks:")
    for task in tasks:
        print(f"  - [{task['id']}] {task['description'][:60]}...")

    # Load or create features.json
    if FEATURES_PATH.exists():
        data = json.loads(FEATURES_PATH.read_text())
    else:
        data = {"features": []}

    # Add stabilization tasks at the beginning
    existing_ids = {f["id"] for f in data["features"]}
    for task in tasks:
        if task["id"] not in existing_ids:
            data["features"].insert(0, task)

    # Save
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    FEATURES_PATH.write_text(json.dumps(data, indent=2))

    print(f"\nStabilization tasks added to {FEATURES_PATH}")
    print("Run 'python harness/coding/loop.py' to start fixing issues.")

    return 0


def cmd_baseline() -> int:
    """Generate test strategy and baseline test recommendations."""
    if not HEALTH_REPORT_PATH.exists():
        print("No health report found. Running diagnosis first...")
        cmd_diagnose()

    # Load report data by re-running analysis
    report = HealthReport(
        status=HealthStatus.HEALTHY,
        project_type=detect_project_type(),
        timestamp=datetime.now(timezone.utc).isoformat(),
        test_result=audit_test_health(),
        external_services=detect_external_services(),
        routes=discover_routes(),
    )

    print("Generating test strategy recommendations...")
    strategy = recommend_test_strategy(report)

    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    TEST_STRATEGY_PATH.write_text(strategy)

    print(f"Test strategy saved to: {TEST_STRATEGY_PATH}")

    return 0


def cmd_fixtures() -> int:
    """Scaffold webhook fixture structure."""
    print("Detecting external services...")
    services = detect_external_services()

    if not services:
        print("No external services detected that need webhook fixtures.")
        return 0

    print(f"Found {len(services)} services: {', '.join(s.name for s in services)}")

    # Create mock report for scaffolding
    report = HealthReport(
        status=HealthStatus.HEALTHY,
        project_type=ProjectType.API,
        timestamp=datetime.now(timezone.utc).isoformat(),
        external_services=services,
    )

    scaffold_webhook_fixtures(report)

    return 0


# =============================================================================
# Manual QA Commands
# =============================================================================

def prompt_for_additional_routes() -> list[dict]:
    """Interactively prompt user for additional business-critical routes."""
    print("\n" + "=" * 50)
    print("ADD BUSINESS-CRITICAL ROUTES")
    print("=" * 50)
    print("Enter routes that are critical but weren't auto-detected.")
    print("Format: /path [api|page]")
    print("Examples:")
    print("  /api/checkout")
    print("  /dashboard/settings page")
    print("Enter empty line when done.\n")

    additional = []
    while True:
        try:
            line = input("Route: ").strip()
            if not line:
                break

            # Parse input
            parts = line.split()
            path = parts[0] if parts else ""
            route_type = "page"  # default

            # Determine type
            if len(parts) > 1 and parts[1].lower() in ("api", "page"):
                route_type = parts[1].lower()
            elif "/api" in path.lower():
                route_type = "api"

            if path.startswith("/"):
                additional.append({
                    "path": path,
                    "type": route_type,
                    "source": "user"
                })
                print(f"  Added: {path} ({route_type})")
            else:
                print("  Skipped: Route must start with /")

        except (EOFError, KeyboardInterrupt):
            print("\nDone adding routes.")
            break

    return additional


def generate_manual_qa_plan(report: HealthReport, user_routes: list[dict]) -> None:
    """Generate a checklist combining auto-detected and user-specified routes."""
    lines = [
        "# Manual QA Plan",
        "",
        "Verify the following routes work manually.",
        "Check the box `[x]` if it works. Leave `[ ]` if broken or untested.",
        "",
    ]

    # Auto-detected routes
    auto_routes = report.routes or []
    if auto_routes:
        lines.append("## Auto-Detected Routes")
        lines.append("")
        api_routes = [r for r in auto_routes if r.get("type") == "api"]
        page_routes = [r for r in auto_routes if r.get("type") == "page"]

        if api_routes:
            lines.append("### API Endpoints")
            for route in api_routes:
                lines.append(f"- [ ] `{route['path']}` (Source: `{route.get('file', 'unknown')}`)")
            lines.append("")

        if page_routes:
            lines.append("### Pages")
            for route in page_routes:
                lines.append(f"- [ ] `{route['path']}` (Source: `{route.get('file', 'unknown')}`)")
            lines.append("")

    # User-specified critical routes
    if user_routes:
        lines.append("## Business-Critical Routes (User-Specified)")
        lines.append("")
        for route in user_routes:
            lines.append(f"- [ ] `{route['path']}` ({route['type']}) - **CRITICAL**")
        lines.append("")

    # External services
    if report.external_services:
        lines.append("## External Service Integration")
        lines.append("")
        for svc in report.external_services:
            lines.append(f"- [ ] **{svc.name}**: Verify webhook/API connection works")
        lines.append("")

    lines.append("---")
    lines.append("After verification, run `python harness/doctor.py solidify` to generate baseline tests.")

    MANUAL_QA_PATH.write_text("\n".join(lines))
    print(f"\nGenerated Manual QA Plan: {MANUAL_QA_PATH}")


def cmd_qa(interactive: bool = True) -> int:
    """Generate manual QA verification checklist.

    Args:
        interactive: If True, prompt for additional routes. If False, auto-detect only.
    """
    if not HEALTH_REPORT_PATH.exists():
        print("No health report found. Running diagnosis first...")
        cmd_diagnose()

    print("\nDiscovering routes...")
    routes = discover_routes()
    services = detect_external_services()

    report = HealthReport(
        status=HealthStatus.HEALTHY,
        project_type=detect_project_type(),
        timestamp=datetime.now(timezone.utc).isoformat(),
        routes=routes,
        external_services=services,
    )

    # Show what was auto-detected
    print(f"\nAuto-detected {len(routes)} routes:")
    for route in routes[:10]:  # Show first 10
        print(f"  {route.get('type', '?'):5} {route['path']}")
    if len(routes) > 10:
        print(f"  ... and {len(routes) - 10} more")

    if services:
        print(f"\nDetected {len(services)} external services:")
        for svc in services:
            print(f"  - {svc.name}")

    # Prompt for additional routes only if interactive and TTY available
    if interactive and sys.stdin.isatty():
        user_routes = prompt_for_additional_routes()
    else:
        user_routes = []
        if not interactive:
            print("(Non-interactive mode: skipping route prompts)")

    # Generate the plan
    generate_manual_qa_plan(report, user_routes)

    print("\nNext steps:")
    print("  1. Open specs/manual_qa_plan.md")
    print("  2. Manually test each route")
    print("  3. Check [x] the boxes that work")
    print("  4. Run: python harness/doctor.py solidify")

    return 0


def parse_verified_routes() -> tuple[list[dict], list[dict]]:
    """
    Parse manual_qa_plan.md for verified (checked) routes.

    Returns:
        Tuple of (verified_routes, verified_services)
    """
    if not MANUAL_QA_PATH.exists():
        return [], []

    content = MANUAL_QA_PATH.read_text()
    verified_routes = []
    verified_services = []

    for line in content.split("\n"):
        # Match checked boxes: - [x] or - [X]
        if re.match(r"^-\s*\[x\]", line, re.IGNORECASE):
            # Extract route path (in backticks)
            path_match = re.search(r"`([^`]+)`", line)
            if path_match:
                path = path_match.group(1)
                if path.startswith("/"):
                    # Determine type from context
                    route_type = "api" if "/api" in path.lower() else "page"
                    # Check if explicitly marked
                    if "(api)" in line.lower():
                        route_type = "api"
                    elif "(page)" in line.lower():
                        route_type = "page"

                    verified_routes.append({"path": path, "type": route_type})

            # Check for service name (bold text)
            svc_match = re.search(r"\*\*([^*:]+)\*\*:", line)
            if svc_match:
                verified_services.append({"name": svc_match.group(1).strip()})

    return verified_routes, verified_services


def cmd_solidify() -> int:
    """Generate baseline tests from verified manual QA results."""
    verified_routes, verified_services = parse_verified_routes()

    if not verified_routes and not verified_services:
        print("No verified routes found in specs/manual_qa_plan.md")
        print("\nTo use this command:")
        print("  1. Run: python harness/doctor.py qa")
        print("  2. Manually test each route")
        print("  3. Check [x] the boxes that work")
        print("  4. Run this command again")
        return 1

    print(f"Found {len(verified_routes)} verified routes, {len(verified_services)} verified services")

    # Detect project type and get appropriate test template
    project_type = detect_project_type()
    template = get_test_template(project_type)

    print(f"Project type: {project_type.value}")
    print(f"Generating {template['language']} tests...")

    # Generate baseline test via Claude
    routes_json = json.dumps(verified_routes, indent=2)

    # Language-specific prompts
    if template["language"] == "python":
        prompt = f"""Generate a pytest test file for these verified working routes.

VERIFIED ROUTES:
{routes_json}

Requirements:
- File will be: {template['test_dir']}/test_baseline_verified.py
- Use pytest and requests library
- For each route, write a simple smoke test
- API routes: use requests.get() and check for status < 400
- Use pytest fixtures for base URL configuration (from environment or default)
- Just verify the route is reachable and doesn't error
- Group tests in classes logically (TestAPIHealth, TestAPIEndpoints, etc.)

Output ONLY the Python code, no markdown code blocks or explanations."""

        output_path = Path(f"{template['test_dir']}/test_baseline_verified.py")
    else:
        prompt = f"""Generate a Playwright test file for these verified working routes.

VERIFIED ROUTES:
{routes_json}

Requirements:
- File will be: {template['test_dir']}/baseline_verified.spec.ts
- For each route, write a simple smoke test
- API routes: use request.get() and check for status < 400
- Page routes: use page.goto() and check for no crash (check title or body exists)
- DO NOT test authentication flows or fill forms
- Just verify the route is reachable and doesn't error
- Group tests logically (API vs Pages)

Output ONLY the TypeScript code, no markdown code blocks or explanations."""

        output_path = Path(f"{template['test_dir']}/baseline_verified.spec.ts")

    try:
        print("\nGenerating baseline tests via Claude...")
        test_code = call_claude_cli(prompt, timeout=120)

        # Clean up code block markers if present
        test_code = re.sub(r"^```\w*\n?", "", test_code)
        test_code = re.sub(r"\n?```$", "", test_code)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(test_code)

        print(f"\nGenerated baseline tests: {output_path}")
        print("\nNext steps:")
        print("  1. Review the generated tests")
        print(f"  2. Run: {template['run_command']} {output_path}")
        print("  3. Fix any failures, then commit as your 'Golden Spike'")

        return 0
    except Exception as e:
        print(f"Error generating tests: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Brownfield Doctor - Phase 0 Health Audit System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python harness/doctor.py              # Auto-detect and recommend action
  python harness/doctor.py diagnose     # Full health audit
  python harness/doctor.py stabilize    # Generate fix tasks
  python harness/doctor.py baseline     # Generate test strategy
  python harness/doctor.py fixtures     # Scaffold webhook fixtures
  python harness/doctor.py qa           # Generate manual QA checklist (interactive)
  python harness/doctor.py solidify     # Generate baseline tests from verified QA
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    subparsers.add_parser("diagnose", help="Run full health audit")
    subparsers.add_parser("stabilize", help="Generate stabilization tasks")
    subparsers.add_parser("baseline", help="Generate test strategy recommendations")
    subparsers.add_parser("fixtures", help="Scaffold webhook fixture structure")

    qa_parser = subparsers.add_parser("qa", help="Generate manual QA verification checklist")
    qa_parser.add_argument(
        "-y", "--no-interactive",
        action="store_true",
        help="Skip interactive prompts, use auto-detection only"
    )

    subparsers.add_parser("solidify", help="Generate baseline tests from verified QA results")

    args = parser.parse_args()

    if args.command == "diagnose":
        return cmd_diagnose()
    elif args.command == "stabilize":
        return cmd_stabilize()
    elif args.command == "baseline":
        return cmd_baseline()
    elif args.command == "fixtures":
        return cmd_fixtures()
    elif args.command == "qa":
        interactive = not getattr(args, "no_interactive", False)
        return cmd_qa(interactive=interactive)
    elif args.command == "solidify":
        return cmd_solidify()
    else:
        # Default: run diagnose and recommend action
        result = cmd_diagnose()

        # If critical, prompt for stabilization
        if HEALTH_REPORT_PATH.exists():
            content = HEALTH_REPORT_PATH.read_text()
            if "CRITICAL" in content:
                print("")
                response = input("Project is critical. Generate stabilization tasks? [Y/n]: ").strip().lower()
                if response != "n":
                    return cmd_stabilize()

        return result


if __name__ == "__main__":
    sys.exit(main())
