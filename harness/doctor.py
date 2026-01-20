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
KNOWN_ISSUES_PATH = SPECS_DIR / "known_issues.json"
DOCTOR_STATE_PATH = SPECS_DIR / "doctor_state.json"

# Phase ordering for guided flow
PHASE_ORDER = ["assess", "verify", "protect", "coverage", "fix"]

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
class KnownIssueValidation:
    """Validation result for a known issue."""
    still_present: bool
    verified_at: str
    notes: str = ""


@dataclass
class KnownIssue:
    """A known issue discovered from docs or code annotations."""
    id: str
    source: str  # File:line where issue was found
    source_type: str  # "documentation" or "annotation"
    raw_text: str  # Original text describing the issue
    category: str  # "bug", "technical_debt", "security", "performance", "deprecation"
    severity: str  # "critical", "high", "medium", "low"
    status: str  # "unvalidated", "validated", "potentially_resolved", "resolved"
    validation: Optional[KnownIssueValidation] = None
    related_code: list = field(default_factory=list)  # Files that might be affected
    related_annotations: list = field(default_factory=list)  # Cross-referenced annotations
    discovered_at: str = ""
    last_validated: str = ""

    def __post_init__(self):
        if not self.discovered_at:
            self.discovered_at = datetime.now(timezone.utc).isoformat()


@dataclass
class KnownIssuesRegistry:
    """Registry of all known issues in the project."""
    issues: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    last_scan: str = ""

    def add_issue(self, issue: KnownIssue) -> None:
        """Add an issue to the registry, avoiding duplicates."""
        # Check for duplicates by source
        for existing in self.issues:
            if existing.source == issue.source and existing.raw_text[:50] == issue.raw_text[:50]:
                return  # Duplicate, skip
        self.issues.append(issue)

    def update_summary(self) -> None:
        """Update the summary statistics."""
        self.summary = {
            "total": len(self.issues),
            "by_severity": {
                "critical": sum(1 for i in self.issues if i.severity == "critical"),
                "high": sum(1 for i in self.issues if i.severity == "high"),
                "medium": sum(1 for i in self.issues if i.severity == "medium"),
                "low": sum(1 for i in self.issues if i.severity == "low"),
            },
            "by_status": {
                "validated": sum(1 for i in self.issues if i.status == "validated"),
                "unvalidated": sum(1 for i in self.issues if i.status == "unvalidated"),
                "potentially_resolved": sum(1 for i in self.issues if i.status == "potentially_resolved"),
                "resolved": sum(1 for i in self.issues if i.status == "resolved"),
            },
            "by_category": {
                "bug": sum(1 for i in self.issues if i.category == "bug"),
                "technical_debt": sum(1 for i in self.issues if i.category == "technical_debt"),
                "security": sum(1 for i in self.issues if i.category == "security"),
                "performance": sum(1 for i in self.issues if i.category == "performance"),
                "deprecation": sum(1 for i in self.issues if i.category == "deprecation"),
            }
        }
        self.last_scan = datetime.now(timezone.utc).isoformat()

    def save(self) -> None:
        """Save registry to specs/known_issues.json."""
        SPECS_DIR.mkdir(parents=True, exist_ok=True)
        self.update_summary()
        data = {
            "issues": [asdict(i) for i in self.issues],
            "summary": self.summary,
            "last_scan": self.last_scan,
        }
        KNOWN_ISSUES_PATH.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls) -> "KnownIssuesRegistry":
        """Load registry from specs/known_issues.json."""
        if not KNOWN_ISSUES_PATH.exists():
            return cls()
        try:
            data = json.loads(KNOWN_ISSUES_PATH.read_text())
            registry = cls(
                summary=data.get("summary", {}),
                last_scan=data.get("last_scan", ""),
            )
            for issue_data in data.get("issues", []):
                # Handle validation field
                validation_data = issue_data.pop("validation", None)
                validation = None
                if validation_data and isinstance(validation_data, dict):
                    validation = KnownIssueValidation(**validation_data)
                issue_data["validation"] = validation
                registry.issues.append(KnownIssue(**issue_data))
            return registry
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            print(f"Warning: Could not load known issues registry: {e}")
            return cls()


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


@dataclass
class DoctorPhase:
    """State tracking for a single phase in the doctor flow."""
    completed: bool = False
    completed_at: str = ""
    status: str = ""  # For assess phase: HEALTHY/DRIFTING/CRITICAL
    routes_verified: int = 0  # For verify phase
    routes_total: int = 0
    tests_generated: int = 0  # For protect phase
    tasks_generated: int = 0  # For fix phase
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class DoctorState:
    """
    Tracks progress through the doctor guided workflow phases.

    Phases: assess -> verify -> protect -> coverage -> fix -> complete
    """
    current_phase: str = "assess"
    started_at: str = ""
    phases: dict = field(default_factory=lambda: {
        "assess": DoctorPhase(),
        "verify": DoctorPhase(),
        "protect": DoctorPhase(),
        "coverage": DoctorPhase(),
        "fix": DoctorPhase(),
    })

    def __post_init__(self):
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()
        # Convert dict phases back to DoctorPhase objects if loaded from JSON
        if self.phases and isinstance(list(self.phases.values())[0], dict):
            self.phases = {
                name: DoctorPhase(**data) for name, data in self.phases.items()
            }

    def save(self) -> None:
        """Save state to specs/doctor_state.json."""
        SPECS_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "current_phase": self.current_phase,
            "started_at": self.started_at,
            "phases": {name: asdict(phase) for name, phase in self.phases.items()},
        }
        DOCTOR_STATE_PATH.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls) -> Optional["DoctorState"]:
        """Load state from specs/doctor_state.json, or None if not exists."""
        if not DOCTOR_STATE_PATH.exists():
            return None
        try:
            data = json.loads(DOCTOR_STATE_PATH.read_text())
            return cls(
                current_phase=data.get("current_phase", "assess"),
                started_at=data.get("started_at", ""),
                phases=data.get("phases", {}),
            )
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            print(f"Warning: Could not load doctor state: {e}")
            return None

    def mark_phase_complete(self, phase: str, **kwargs) -> None:
        """Mark a phase as completed with optional metadata."""
        if phase in self.phases:
            self.phases[phase].completed = True
            self.phases[phase].completed_at = datetime.now(timezone.utc).isoformat()
            for key, value in kwargs.items():
                if hasattr(self.phases[phase], key):
                    setattr(self.phases[phase], key, value)
            # Advance to next phase
            idx = PHASE_ORDER.index(phase)
            if idx + 1 < len(PHASE_ORDER):
                self.current_phase = PHASE_ORDER[idx + 1]
            else:
                self.current_phase = "complete"
            self.save()

    def skip_phase(self, phase: str, reason: str) -> None:
        """Skip a phase with a reason."""
        if phase in self.phases:
            self.phases[phase].skipped = True
            self.phases[phase].skip_reason = reason
            self.phases[phase].completed = True
            self.phases[phase].completed_at = datetime.now(timezone.utc).isoformat()
            # Advance to next phase
            idx = PHASE_ORDER.index(phase)
            if idx + 1 < len(PHASE_ORDER):
                self.current_phase = PHASE_ORDER[idx + 1]
            else:
                self.current_phase = "complete"
            self.save()

    def is_complete(self) -> bool:
        """Check if all phases are done."""
        return self.current_phase == "complete"


def get_phase_display_name(phase: str) -> str:
    """Get human-readable name for a phase."""
    names = {
        "assess": "ASSESS (Health Audit)",
        "verify": "VERIFY (Manual QA)",
        "protect": "PROTECT (Baseline Tests)",
        "coverage": "COVERAGE (Test Strategy)",
        "fix": "FIX (Stabilization Tasks)",
        "complete": "COMPLETE",
    }
    return names.get(phase, phase.upper())


def can_skip_phase(phase: str, state: DoctorState) -> tuple[bool, str]:
    """
    Check if a phase can be skipped and provide the reason.

    Returns (can_skip, reason_message).
    """
    if phase == "verify":
        # Check if we have good test coverage
        if HEALTH_REPORT_PATH.exists():
            content = HEALTH_REPORT_PATH.read_text()
            # Look for test pass rate info
            if "Pass Rate: 100%" in content or "Pass Rate: 9" in content:
                # 90%+ pass rate
                return True, "High test coverage detected - verification via existing tests"
        return False, "Low or unknown test coverage - manual verification recommended"

    if phase == "protect":
        # Can skip if baseline tests already exist
        baseline_path = Path("tests/e2e/baseline_verified.spec.ts")
        if baseline_path.exists():
            return True, "Baseline tests already exist"
        return False, "No baseline tests - protection recommended before making changes"

    if phase == "coverage":
        # Coverage planning is optional
        return True, "Test strategy planning is optional"

    if phase == "fix":
        # Check if project is healthy
        if HEALTH_REPORT_PATH.exists():
            content = HEALTH_REPORT_PATH.read_text()
            if "## Status: HEALTHY" in content:
                return True, "Project is healthy - no stabilization needed"
        return False, "Issues detected that should be addressed"

    return False, ""


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


def parse_test_output(output: str, framework_hint: str = "") -> Optional[TestResult]:
    """
    Parse test output from various frameworks.

    Supports: Jest, Vitest, Playwright, Mocha, Jasmine, RSpec, pytest, unittest
    """
    # Jest/Vitest: "Tests: X passed, Y failed, Z total"
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

    # Playwright: "X passed" and optionally "Y failed"
    match = re.search(r"(\d+) passed", output)
    if match:
        passed = int(match.group(1))
        failed_match = re.search(r"(\d+) failed", output)
        failed = int(failed_match.group(1)) if failed_match else 0
        skipped_match = re.search(r"(\d+) skipped", output)
        skipped = int(skipped_match.group(1)) if skipped_match else 0

        if "playwright" in output.lower() or framework_hint == "playwright":
            return TestResult(
                framework="playwright",
                total=passed + failed + skipped,
                passed=passed,
                failed=failed,
                skipped=skipped,
                output=output[:3000]
            )

    # Mocha: "X passing", "Y failing", "Z pending"
    passing_match = re.search(r"(\d+) passing", output)
    failing_match = re.search(r"(\d+) failing", output)
    pending_match = re.search(r"(\d+) pending", output)
    if passing_match or failing_match:
        passed = int(passing_match.group(1)) if passing_match else 0
        failed = int(failing_match.group(1)) if failing_match else 0
        pending = int(pending_match.group(1)) if pending_match else 0
        return TestResult(
            framework="mocha",
            total=passed + failed + pending,
            passed=passed,
            failed=failed,
            skipped=pending,
            output=output[:3000]
        )

    # Jasmine: "X specs, Y failures"
    match = re.search(r"(\d+) specs?,\s*(\d+) failures?", output)
    if match:
        total, failed = int(match.group(1)), int(match.group(2))
        return TestResult(
            framework="jasmine",
            total=total,
            passed=total - failed,
            failed=failed,
            skipped=0,
            output=output[:3000]
        )

    # RSpec: "X examples, Y failures"
    match = re.search(r"(\d+) examples?,\s*(\d+) failures?", output)
    if match:
        total, failed = int(match.group(1)), int(match.group(2))
        pending_match = re.search(r"(\d+) pending", output)
        pending = int(pending_match.group(1)) if pending_match else 0
        return TestResult(
            framework="rspec",
            total=total,
            passed=total - failed - pending,
            failed=failed,
            skipped=pending,
            output=output[:3000]
        )

    # pytest: "X passed, Y failed" or "X passed"
    passed_match = re.search(r"(\d+) passed", output)
    failed_match = re.search(r"(\d+) failed", output)
    error_match = re.search(r"(\d+) error", output)
    skipped_match = re.search(r"(\d+) skipped", output)
    if passed_match or failed_match:
        passed = int(passed_match.group(1)) if passed_match else 0
        failed = int(failed_match.group(1)) if failed_match else 0
        errors = int(error_match.group(1)) if error_match else 0
        skipped = int(skipped_match.group(1)) if skipped_match else 0
        return TestResult(
            framework="pytest",
            total=passed + failed + errors + skipped,
            passed=passed,
            failed=failed + errors,
            skipped=skipped,
            output=output[:3000]
        )

    # Python unittest: "Ran X tests" and "OK" or "FAILED (failures=Y)"
    match = re.search(r"Ran (\d+) tests?", output)
    if match:
        total = int(match.group(1))
        if "OK" in output:
            return TestResult(
                framework="unittest",
                total=total,
                passed=total,
                failed=0,
                skipped=0,
                output=output[:3000]
            )
        fail_match = re.search(r"failures?=(\d+)", output)
        error_match = re.search(r"errors?=(\d+)", output)
        failed = int(fail_match.group(1)) if fail_match else 0
        errors = int(error_match.group(1)) if error_match else 0
        return TestResult(
            framework="unittest",
            total=total,
            passed=total - failed - errors,
            failed=failed + errors,
            skipped=0,
            output=output[:3000]
        )

    # Go test: "ok" or "FAIL" per package, "--- PASS:" / "--- FAIL:"
    pass_matches = re.findall(r"--- PASS:", output)
    fail_matches = re.findall(r"--- FAIL:", output)
    if pass_matches or fail_matches:
        passed = len(pass_matches)
        failed = len(fail_matches)
        return TestResult(
            framework="go test",
            total=passed + failed,
            passed=passed,
            failed=failed,
            skipped=0,
            output=output[:3000]
        )

    return None


def audit_test_health() -> Optional[TestResult]:
    """Run existing test suite and measure health."""
    # Try npm test
    if Path("package.json").exists():
        try:
            pkg = json.loads(Path("package.json").read_text())
            if "test" in pkg.get("scripts", {}):
                test_script = pkg["scripts"]["test"]

                # Detect framework from script
                framework_hint = ""
                if "playwright" in test_script.lower():
                    framework_hint = "playwright"
                elif "mocha" in test_script.lower():
                    framework_hint = "mocha"
                elif "jasmine" in test_script.lower():
                    framework_hint = "jasmine"

                code, stdout, stderr = run_shell(["npm", "test", "--", "--passWithNoTests"], timeout=300)
                output = stdout + stderr

                result = parse_test_output(output, framework_hint)
                if result:
                    return result

        except (json.JSONDecodeError, IOError):
            pass

    # Try pytest
    if Path("pytest.ini").exists() or Path("pyproject.toml").exists() or list(Path(".").glob("**/test_*.py")):
        code, stdout, stderr = run_shell(["pytest", "--tb=no", "-q"], timeout=300)
        output = stdout + stderr

        result = parse_test_output(output, "pytest")
        if result:
            return result

    # Try RSpec for Ruby projects
    if Path("Gemfile").exists() and (Path("spec").exists() or Path(".rspec").exists()):
        code, stdout, stderr = run_shell(["bundle", "exec", "rspec", "--format", "progress"], timeout=300)
        output = stdout + stderr

        result = parse_test_output(output, "rspec")
        if result:
            return result

    # Try Go tests
    if Path("go.mod").exists():
        code, stdout, stderr = run_shell(["go", "test", "./...", "-v"], timeout=300)
        output = stdout + stderr

        result = parse_test_output(output, "go")
        if result:
            return result

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
# Known Issues Registry
# =============================================================================

def _categorize_issue(text: str, pattern: str) -> tuple[str, str]:
    """
    Categorize an issue based on its text and source pattern.

    Returns:
        Tuple of (category, severity)
    """
    text_lower = text.lower()

    # Security-related keywords
    security_keywords = ["security", "auth", "password", "token", "csrf", "xss", "injection", "vulnerability"]
    if any(kw in text_lower for kw in security_keywords) or pattern == "SECURITY":
        return ("security", "critical")

    # Bug-related patterns
    if pattern in ("FIXME", "BUG", "BROKEN"):
        return ("bug", "high")

    # Known/broken issues from docs
    if "known issue" in text_lower or "broken" in text_lower or "not working" in text_lower:
        return ("bug", "high")

    # Performance
    if "slow" in text_lower or "performance" in text_lower or "optimize" in text_lower or pattern == "OPTIMIZE":
        return ("performance", "medium")

    # Deprecation
    if "deprecated" in text_lower or pattern == "@deprecated":
        return ("deprecation", "medium")

    # Technical debt (HACK, TODO, KLUDGE, etc.)
    if pattern in ("HACK", "KLUDGE", "XXX", "REFACTOR"):
        return ("technical_debt", "high")

    if pattern == "TODO":
        return ("technical_debt", "medium")

    if pattern == "NOTE" or pattern == "WARNING":
        return ("technical_debt", "low")

    # Default
    return ("technical_debt", "medium")


def _find_related_code(issue_text: str, source_file: str) -> list[str]:
    """
    Find code files that might be related to an issue.

    Searches for file references, class names, and function names mentioned in the issue.
    """
    related = []

    # Extract file paths mentioned in the text (e.g., "src/auth/cookies.ts")
    file_refs = re.findall(r'[\w/]+\.(?:ts|tsx|js|jsx|py|go|rs|java|rb)', issue_text)
    for ref in file_refs:
        if Path(ref).exists():
            related.append(ref)

    # If source is a code file, add its directory as potentially related
    source_path = Path(source_file.split(":")[0])
    if source_path.suffix in [".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs", ".java", ".rb"]:
        related.append(str(source_path))

    # Extract potential identifiers (PascalCase or snake_case names)
    identifiers = re.findall(r'\b([A-Z][a-zA-Z]+|[a-z]+_[a-z_]+)\b', issue_text)
    skip_dirs = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", "venv", "harness"}
    extensions = [".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs", ".java", ".rb"]

    for ident in identifiers[:5]:  # Limit to avoid excessive searching
        for ext in extensions:
            for file_path in Path(".").rglob(f"*{ext}"):
                if any(skip in file_path.parts for skip in skip_dirs):
                    continue
                try:
                    content = file_path.read_text(errors="ignore")
                    if ident in content and str(file_path) not in related:
                        related.append(str(file_path))
                        break
                except IOError:
                    pass

    return list(set(related))[:10]  # Dedupe and limit


def _cross_reference_issues(
    doc_issues: list[DocumentedIssue],
    annotations: list[AnnotationMatch]
) -> dict[str, list[str]]:
    """
    Cross-reference documented issues with code annotations.

    Returns:
        Dict mapping doc issue source to list of related annotation sources
    """
    cross_refs = {}

    for doc_issue in doc_issues:
        related_annotations = []
        doc_text_lower = doc_issue.issue.lower()

        # Extract keywords from the doc issue
        doc_words = set(re.findall(r'\b[a-z]{4,}\b', doc_text_lower))

        for ann in annotations:
            ann_text_lower = ann.text.lower()
            ann_words = set(re.findall(r'\b[a-z]{4,}\b', ann_text_lower))

            # Check for word overlap
            overlap = len(doc_words & ann_words)
            if overlap >= 2:  # At least 2 common words
                related_annotations.append(f"{ann.pattern} at {ann.file}:{ann.line}")

        if related_annotations:
            cross_refs[doc_issue.source] = related_annotations

    return cross_refs


def scan_and_register_issues() -> KnownIssuesRegistry:
    """
    Scan the codebase for known issues and register them.

    This combines documentation scanning and code annotation scanning,
    then cross-references them to build a comprehensive issue registry.

    Returns:
        KnownIssuesRegistry with all discovered issues
    """
    registry = KnownIssuesRegistry()
    issue_counter = 1

    print("  Scanning for known issues...")

    # Scan documentation for issues
    doc_issues = scan_documentation()
    print(f"    -> {len(doc_issues)} documented issues found")

    # Scan code annotations
    annotations = scan_code_annotations()
    print(f"    -> {len(annotations)} code annotations found")

    # Cross-reference
    cross_refs = _cross_reference_issues(doc_issues, annotations)
    print(f"    -> {len(cross_refs)} cross-references found")

    # Register documented issues
    for doc_issue in doc_issues:
        category, severity = _categorize_issue(doc_issue.issue, "")
        # Override severity from doc_issue if set
        if doc_issue.severity == "critical":
            severity = "critical"
        elif doc_issue.severity == "high" and severity not in ["critical"]:
            severity = "high"

        issue = KnownIssue(
            id=f"ki-{issue_counter:03d}",
            source=doc_issue.source,
            source_type="documentation",
            raw_text=doc_issue.issue,
            category=category,
            severity=severity,
            status="unvalidated",
            related_code=_find_related_code(doc_issue.issue, doc_issue.source),
            related_annotations=cross_refs.get(doc_issue.source, []),
        )
        registry.add_issue(issue)
        issue_counter += 1

    # Register code annotations as issues (prioritize critical ones)
    critical_patterns = {"SECURITY", "FIXME", "BUG", "BROKEN", "HACK", "XXX", "DANGER"}
    for ann in annotations:
        if ann.pattern in critical_patterns or ann.priority in ["critical", "high"]:
            category, severity = _categorize_issue(ann.text, ann.pattern)

            issue = KnownIssue(
                id=f"ki-{issue_counter:03d}",
                source=f"{ann.file}:{ann.line}",
                source_type="annotation",
                raw_text=ann.text,
                category=category,
                severity=severity,
                status="unvalidated",
                related_code=[ann.file],
            )
            registry.add_issue(issue)
            issue_counter += 1

    registry.update_summary()
    print(f"    -> {len(registry.issues)} total issues registered")

    return registry


def validate_issues_with_tests(registry: KnownIssuesRegistry) -> KnownIssuesRegistry:
    """
    Validate known issues by running related tests.

    For each issue:
    - Find tests related to the affected code
    - Run those tests
    - Mark as "validated" if tests fail
    - Mark as "potentially_resolved" if tests pass

    Args:
        registry: The issues registry to validate

    Returns:
        Updated registry with validation status
    """
    if not registry.issues:
        return registry

    print("  Validating issues with tests...")

    # Get list of all test files
    test_files = []
    for pattern in ["tests/**/*.py", "tests/**/*.ts", "tests/**/*.spec.ts", "test/**/*.py", "test/**/*.ts"]:
        test_files.extend([str(f) for f in Path(".").glob(pattern)])

    if not test_files:
        print("    -> No test files found, skipping validation")
        return registry

    # Map code files to test files
    code_to_tests = {}
    for test_file in test_files:
        test_path = Path(test_file)
        test_name = test_path.stem.replace("test_", "").replace("_test", "").replace(".spec", "")

        # Match test file to source file
        for ext in [".ts", ".tsx", ".js", ".jsx", ".py"]:
            potential_sources = [
                f"src/{test_name}{ext}",
                f"src/**/{test_name}{ext}",
                f"app/{test_name}{ext}",
                f"app/**/{test_name}{ext}",
            ]
            for pattern in potential_sources:
                for src in Path(".").glob(pattern):
                    code_to_tests.setdefault(str(src), []).append(test_file)

    validated_count = 0
    resolved_count = 0

    for issue in registry.issues:
        # Find tests related to this issue
        related_tests = []
        for code_file in issue.related_code:
            if code_file in code_to_tests:
                related_tests.extend(code_to_tests[code_file])

        if not related_tests:
            continue

        # Run the related tests
        related_tests = list(set(related_tests))[:5]  # Limit to 5 tests

        # Detect test framework and run
        all_passed = True
        for test_file in related_tests:
            if test_file.endswith(".py"):
                code, stdout, stderr = run_shell(["pytest", test_file, "-v", "--tb=short"], timeout=60)
            elif test_file.endswith(".ts") or test_file.endswith(".spec.ts"):
                code, stdout, stderr = run_shell(["npx", "playwright", "test", test_file], timeout=120)
            else:
                continue

            if code != 0:
                all_passed = False
                break

        # Update validation status
        now = datetime.now(timezone.utc).isoformat()
        if all_passed:
            issue.status = "potentially_resolved"
            issue.validation = KnownIssueValidation(
                still_present=False,
                verified_at=now,
                notes=f"Related tests pass: {', '.join(related_tests[:3])}"
            )
            resolved_count += 1
        else:
            issue.status = "validated"
            issue.validation = KnownIssueValidation(
                still_present=True,
                verified_at=now,
                notes=f"Related tests fail: {', '.join(related_tests[:3])}"
            )
            validated_count += 1

        issue.last_validated = now

    print(f"    -> {validated_count} issues validated (confirmed), {resolved_count} potentially resolved")

    registry.update_summary()
    return registry


def store_issues_in_memory(registry: KnownIssuesRegistry) -> None:
    """
    Store validated issues in working memory for architect/loop access.

    This makes the issues available to other components without them
    needing to parse the full registry.
    """
    if not MEMORY_AVAILABLE:
        return

    # Store high-level summary
    update_understanding("doctor", "known_issues_total", str(registry.summary.get("total", 0)))
    update_understanding("doctor", "known_issues_critical", str(registry.summary.get("by_severity", {}).get("critical", 0)))
    update_understanding("doctor", "known_issues_high", str(registry.summary.get("by_severity", {}).get("high", 0)))

    # Store high-priority issue summaries
    high_priority = [i for i in registry.issues if i.severity in ("critical", "high")][:5]
    if high_priority:
        issue_summaries = "; ".join([
            f"[{i.id}] {i.category}: {i.raw_text[:60]}..."
            for i in high_priority
        ])
        update_understanding("doctor", "high_priority_issues", issue_summaries)

    # Store areas of concern (directories with most issues)
    areas = {}
    for issue in registry.issues:
        for code_file in issue.related_code:
            parts = Path(code_file).parts
            if len(parts) >= 2:
                area = f"{parts[0]}/{parts[1]}"
            elif len(parts) == 1:
                area = parts[0]
            else:
                continue
            areas[area] = areas.get(area, 0) + 1

    top_areas = sorted(areas.items(), key=lambda x: x[1], reverse=True)[:5]
    if top_areas:
        update_understanding("doctor", "areas_of_concern", ", ".join([a[0] for a in top_areas]))


def cmd_issues() -> int:
    """Scan for known issues and create the registry."""
    print("Scanning for known issues...")
    print("")

    # Scan and register
    registry = scan_and_register_issues()

    # Optionally validate with tests
    if registry.issues and (Path("pytest.ini").exists() or Path("playwright.config.ts").exists()):
        print("")
        response = input("Run tests to validate issues? (y/N): ").strip().lower()
        if response == "y":
            registry = validate_issues_with_tests(registry)

    # Save registry
    registry.save()
    print(f"\nKnown issues registry saved to {KNOWN_ISSUES_PATH}")

    # Store in memory
    if MEMORY_AVAILABLE:
        store_issues_in_memory(registry)
        print("High-priority issues stored in working memory")

    # Display summary
    print("\n" + "=" * 50)
    print("KNOWN ISSUES SUMMARY")
    print("=" * 50)
    print(f"Total issues: {registry.summary.get('total', 0)}")
    print(f"\nBy severity:")
    for sev, count in registry.summary.get("by_severity", {}).items():
        if count > 0:
            print(f"  {sev}: {count}")
    print(f"\nBy status:")
    for status, count in registry.summary.get("by_status", {}).items():
        if count > 0:
            print(f"  {status}: {count}")
    print("")

    return 0


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

    # Scan and register known issues
    print("  Registering known issues...")
    issues_registry = scan_and_register_issues()
    issues_registry.save()
    print(f"    -> Saved to {KNOWN_ISSUES_PATH}")

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

        # Store known issues summary in memory
        store_issues_in_memory(issues_registry)

    if report.status == HealthStatus.CRITICAL:
        print("")
        print("RECOMMENDATION: Run '/doctor stabilize' to generate fix tasks.")

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
    print("Run '/loop' to start fixing issues.")

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
    lines.append("After verification, run `/doctor solidify` to generate baseline tests.")

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
    print("  4. Run: /doctor solidify")

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
        print("  1. Run: /doctor qa")
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


# =============================================================================
# Guided Workflow Commands
# =============================================================================

def cmd_status() -> int:
    """Show current doctor workflow progress."""
    state = DoctorState.load()

    print("\n" + "=" * 50)
    print("  DOCTOR - Brownfield Stabilization Status")
    print("=" * 50)

    if state is None:
        print("\nNo doctor state found.")
        print("Run 'doctor' with no arguments to start the guided flow.")
        return 0

    print(f"\nStarted: {state.started_at[:19].replace('T', ' ')}")
    print(f"Current Phase: {get_phase_display_name(state.current_phase)}")
    print("\nProgress:")

    for phase in PHASE_ORDER:
        phase_data = state.phases.get(phase, DoctorPhase())
        name = get_phase_display_name(phase)

        if phase_data.completed:
            if phase_data.skipped:
                print(f"  ⊘ {name} - Skipped ({phase_data.skip_reason})")
            else:
                status_extra = ""
                if phase == "assess" and phase_data.status:
                    status_extra = f" ({phase_data.status})"
                elif phase == "verify" and phase_data.routes_total > 0:
                    status_extra = f" ({phase_data.routes_verified}/{phase_data.routes_total} routes)"
                elif phase == "protect" and phase_data.tests_generated > 0:
                    status_extra = f" ({phase_data.tests_generated} tests)"
                elif phase == "fix" and phase_data.tasks_generated > 0:
                    status_extra = f" ({phase_data.tasks_generated} tasks)"
                print(f"  ✓ {name} - Complete{status_extra}")
        elif state.current_phase == phase:
            print(f"  → {name} - In Progress")
        else:
            print(f"  ○ {name} - Pending")

    if state.is_complete():
        print("\n✓ All phases complete! Project is ready for feature development.")
    else:
        print(f"\nNext: Run 'doctor next' to continue with {get_phase_display_name(state.current_phase)}")

    return 0


def cmd_reset() -> int:
    """Reset doctor state and start over."""
    if DOCTOR_STATE_PATH.exists():
        DOCTOR_STATE_PATH.unlink()
        print("Doctor state reset. Run 'doctor' to start fresh.")
    else:
        print("No doctor state to reset.")
    return 0


def _run_phase(phase: str, state: DoctorState) -> int:
    """
    Run a specific phase and update state.

    Returns 0 on success, non-zero on failure.
    """
    phase_map = {
        "assess": ("diagnose", cmd_diagnose),
        "verify": ("qa", lambda: cmd_qa(interactive=True)),
        "protect": ("solidify", cmd_solidify),
        "coverage": ("baseline", cmd_baseline),
        "fix": ("stabilize", cmd_stabilize),
    }

    if phase not in phase_map:
        print(f"Unknown phase: {phase}")
        return 1

    cmd_name, cmd_func = phase_map[phase]
    print(f"\n{'=' * 50}")
    print(f"  PHASE: {get_phase_display_name(phase)}")
    print("=" * 50 + "\n")

    result = cmd_func()

    if result == 0:
        # Collect phase-specific metadata
        kwargs = {}
        if phase == "assess":
            # Extract status from health report
            if HEALTH_REPORT_PATH.exists():
                content = HEALTH_REPORT_PATH.read_text()
                if "## Status: HEALTHY" in content:
                    kwargs["status"] = "HEALTHY"
                elif "## Status: CRITICAL" in content:
                    kwargs["status"] = "CRITICAL"
                else:
                    kwargs["status"] = "DRIFTING"
        elif phase == "verify":
            # Count verified routes from QA plan
            if MANUAL_QA_PATH.exists():
                content = MANUAL_QA_PATH.read_text()
                total = content.count("- [ ]") + content.count("- [x]")
                verified = content.count("- [x]")
                kwargs["routes_total"] = total
                kwargs["routes_verified"] = verified
        elif phase == "protect":
            # Count generated tests
            baseline_ts = Path("tests/e2e/baseline_verified.spec.ts")
            baseline_py = Path("tests/test_baseline_verified.py")
            if baseline_ts.exists():
                kwargs["tests_generated"] = baseline_ts.read_text().count("test(")
            elif baseline_py.exists():
                kwargs["tests_generated"] = baseline_py.read_text().count("def test_")
        elif phase == "fix":
            # Count generated tasks
            if FEATURES_PATH.exists():
                try:
                    features = json.loads(FEATURES_PATH.read_text())
                    kwargs["tasks_generated"] = len(features)
                except json.JSONDecodeError:
                    pass

        state.mark_phase_complete(phase, **kwargs)
        print(f"\n✓ {get_phase_display_name(phase)} complete.")

    return result


def cmd_next_phase() -> int:
    """Run the next phase in sequence (non-interactive)."""
    state = DoctorState.load()

    if state is None:
        print("No doctor state found. Starting fresh...")
        state = DoctorState()
        state.save()

    if state.is_complete():
        print("All phases complete! Project is ready for feature development.")
        return 0

    phase = state.current_phase

    # Check if phase can be auto-skipped
    can_skip, reason = can_skip_phase(phase, state)
    if can_skip:
        print(f"Auto-skipping {get_phase_display_name(phase)}: {reason}")
        state.skip_phase(phase, reason)
        # Recurse to next phase
        return cmd_next_phase()

    return _run_phase(phase, state)


def cmd_guided_flow() -> int:
    """
    Interactive guided flow through all doctor phases.

    Walks users through: assess -> verify -> protect -> coverage -> fix
    """
    print("\n" + "=" * 50)
    print("  DOCTOR - Brownfield Stabilization")
    print("=" * 50)

    # Load or create state
    state = DoctorState.load()
    is_resume = state is not None

    if state is None:
        print("\nChecking project state...")

        # Check for existing brownfield indicators
        has_code = any(Path(".").glob("**/*.py")) or any(Path(".").glob("**/*.ts"))
        has_health_report = HEALTH_REPORT_PATH.exists()
        has_state = DOCTOR_STATE_PATH.exists()

        if has_code:
            print("  ✓ Existing codebase detected")
        if has_health_report:
            print("  ✓ Health report found")
        else:
            print("  ✗ No health report found")
        if has_state:
            print("  ✓ Doctor state found")
        else:
            print("  ✗ No doctor state found")

        print("\nThis appears to be your first time running Doctor on this project.")
        print("I'll guide you through the stabilization process.\n")

        print("PHASES:")
        for i, phase in enumerate(PHASE_ORDER, 1):
            desc = {
                "assess": "Full health audit",
                "verify": "Manual QA of what works",
                "protect": "Generate baseline tests",
                "coverage": "Plan test strategy",
                "fix": "Generate fix tasks",
            }
            print(f"  {i}. {phase.upper():10} - {desc[phase]}")

        print("")
        response = input("[Press Enter to start with ASSESS, or type a phase name to skip ahead, 'q' to quit]: ").strip().lower()

        if response in ("q", "quit", "exit"):
            print("Exiting without changes.")
            return 0

        state = DoctorState()

        # Handle skip-to-phase
        if response and response in PHASE_ORDER:
            print(f"\nSkipping to {response.upper()}...")
            for phase in PHASE_ORDER:
                if phase == response:
                    break
                state.skip_phase(phase, "Skipped by user")
        elif response:
            print(f"Unknown phase '{response}', starting from ASSESS...")

        state.save()

    else:
        # Resuming
        print(f"\nResuming from saved state...")
        print(f"Started: {state.started_at[:19].replace('T', ' ')}")
        print("\nProgress:")

        for phase in PHASE_ORDER:
            phase_data = state.phases.get(phase, DoctorPhase())
            name = get_phase_display_name(phase)

            if phase_data.completed:
                if phase_data.skipped:
                    print(f"  ⊘ {name} - Skipped")
                else:
                    status_extra = ""
                    if phase == "assess" and phase_data.status:
                        status_extra = f" ({phase_data.status})"
                    print(f"  ✓ {name} - Complete{status_extra}")
            elif state.current_phase == phase:
                print(f"  → {name} - In Progress")
            else:
                print(f"  ○ {name} - Pending")

        print("")

    # Main loop
    while not state.is_complete():
        phase = state.current_phase

        # Check if phase can be auto-skipped
        can_skip, skip_reason = can_skip_phase(phase, state)

        if can_skip:
            print(f"\n{get_phase_display_name(phase)} can be skipped: {skip_reason}")
            response = input(f"Skip this phase? [Y/n]: ").strip().lower()
            if response != "n":
                state.skip_phase(phase, skip_reason)
                print(f"Skipped {get_phase_display_name(phase)}.")
                continue

        # Prompt for this phase
        print(f"\nReady for {get_phase_display_name(phase)}?")
        response = input("[Enter=continue, 'skip'=skip phase, 'q'=save & quit]: ").strip().lower()

        if response in ("q", "quit", "exit"):
            print("\nProgress saved. Run 'doctor' to resume.")
            return 0

        if response == "skip":
            # Ask for confirmation on skip
            print(f"\nSkipping {get_phase_display_name(phase)}...")
            if not can_skip:
                print(f"  Warning: {skip_reason}")
                confirm = input("Are you sure? [y/N]: ").strip().lower()
                if confirm != "y":
                    continue
            state.skip_phase(phase, "Skipped by user")
            continue

        # Run the phase
        result = _run_phase(phase, state)

        if result != 0:
            print(f"\nPhase failed with exit code {result}.")
            response = input("Retry? [Y/n/skip/quit]: ").strip().lower()
            if response == "n":
                print("Phase left incomplete. Run 'doctor' to resume.")
                return result
            elif response == "skip":
                state.skip_phase(phase, "Skipped after failure")
            elif response in ("q", "quit"):
                print("Progress saved. Run 'doctor' to resume.")
                return result
            # else: loop will retry

    # All done!
    print("\n" + "=" * 50)
    print("  ✓ ALL PHASES COMPLETE")
    print("=" * 50)
    print("""
Project stabilization complete!

Summary:
  - Health audit performed
  - Working features verified
  - Baseline tests protect known-good functionality
  - Test strategy documented
  - Stabilization tasks queued

Next steps:
  1. Review specs/features.json for queued fix tasks
  2. Run '/loop' to execute fixes
  3. Once stable, run '/architect' for new features
""")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Brownfield Doctor - Phase 0 Health Audit System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  /doctor                # Interactive guided flow (in harness shell)
  /doctor status         # Show current progress
  /doctor next           # Run next phase (non-interactive)
  /doctor reset          # Reset and start over
  /doctor diagnose       # Full health audit (Phase 1)
  /doctor qa             # Manual QA checklist (Phase 2)
  /doctor solidify       # Generate baseline tests (Phase 3)
  /doctor baseline       # Test strategy (Phase 4)
  /doctor stabilize      # Generate fix tasks (Phase 5)

Direct invocation (outside shell):
  python harness/doctor.py fixtures     # Scaffold webhook fixtures (utility)
  python harness/doctor.py issues       # Scan known issues (utility)
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Guided flow commands
    subparsers.add_parser("status", help="Show current phase and progress")
    subparsers.add_parser("next", help="Run the next phase in sequence")
    subparsers.add_parser("reset", help="Reset doctor state and start over")

    # Phase commands (still work standalone)
    subparsers.add_parser("diagnose", help="Run full health audit (Phase 1: ASSESS)")
    subparsers.add_parser("stabilize", help="Generate stabilization tasks (Phase 5: FIX)")
    subparsers.add_parser("baseline", help="Generate test strategy recommendations (Phase 4: COVERAGE)")
    subparsers.add_parser("fixtures", help="Scaffold webhook fixture structure (utility)")

    qa_parser = subparsers.add_parser("qa", help="Generate manual QA verification checklist (Phase 2: VERIFY)")
    qa_parser.add_argument(
        "-y", "--no-interactive",
        action="store_true",
        help="Skip interactive prompts, use auto-detection only"
    )

    subparsers.add_parser("solidify", help="Generate baseline tests from verified QA (Phase 3: PROTECT)")
    subparsers.add_parser("issues", help="Scan and register known issues from docs and code (utility)")

    args = parser.parse_args()

    # Guided flow commands
    if args.command == "status":
        return cmd_status()
    elif args.command == "next":
        return cmd_next_phase()
    elif args.command == "reset":
        return cmd_reset()

    # Phase commands (standalone, also update state)
    elif args.command == "diagnose":
        result = cmd_diagnose()
        if result == 0:
            _update_state_from_standalone("assess")
        return result
    elif args.command == "stabilize":
        result = cmd_stabilize()
        if result == 0:
            _update_state_from_standalone("fix")
        return result
    elif args.command == "baseline":
        result = cmd_baseline()
        if result == 0:
            _update_state_from_standalone("coverage")
        return result
    elif args.command == "fixtures":
        return cmd_fixtures()
    elif args.command == "qa":
        interactive = not getattr(args, "no_interactive", False)
        result = cmd_qa(interactive=interactive)
        if result == 0:
            _update_state_from_standalone("verify")
        return result
    elif args.command == "solidify":
        result = cmd_solidify()
        if result == 0:
            _update_state_from_standalone("protect")
        return result
    elif args.command == "issues":
        return cmd_issues()
    else:
        # Default: interactive guided flow
        return cmd_guided_flow()


def _update_state_from_standalone(phase: str) -> None:
    """Update doctor state when a phase command is run standalone."""
    state = DoctorState.load()
    if state is None:
        state = DoctorState()

    # Only update if this phase is current or earlier
    try:
        current_idx = PHASE_ORDER.index(state.current_phase) if state.current_phase in PHASE_ORDER else -1
        phase_idx = PHASE_ORDER.index(phase)

        if phase_idx <= current_idx or state.current_phase == "complete":
            # Phase already done or we're past it - just mark complete without advancing
            if not state.phases[phase].completed:
                state.phases[phase].completed = True
                state.phases[phase].completed_at = datetime.now(timezone.utc).isoformat()
                state.save()
        else:
            # This is a future phase - skip to it and mark complete
            for skip_phase in PHASE_ORDER[current_idx:phase_idx]:
                if not state.phases[skip_phase].completed:
                    state.skip_phase(skip_phase, "Skipped - ran later phase directly")
            state.mark_phase_complete(phase)
    except ValueError:
        # Phase not in order (e.g., complete) - just save
        pass


if __name__ == "__main__":
    sys.exit(main())
