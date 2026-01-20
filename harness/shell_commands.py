#!/usr/bin/env python3
"""
Shell Command Handlers

Handles slash commands in the interactive shell, routing them to
the appropriate harness modules (architect, loop, doctor, docs).
"""

import sys
from pathlib import Path
from typing import Optional, Callable

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from shell_context import (
    get_quick_status,
    build_project_context,
    is_project_info_missing,
    save_project_info,
    get_project_info,
)


# =============================================================================
# Command Registry
# =============================================================================

COMMANDS = {}


def register_command(name: str, aliases: list = None):
    """Decorator to register a command handler."""
    def decorator(func: Callable):
        COMMANDS[name] = func
        if aliases:
            for alias in aliases:
                COMMANDS[alias] = func
        return func
    return decorator


# =============================================================================
# Built-in Commands
# =============================================================================

@register_command("help", aliases=["h", "?"])
def cmd_help(args: str) -> str:
    """Show available commands."""
    help_text = """
Available Commands
==================

Navigation:
  /help, /h, /?       Show this help
  /status             Show project status summary
  /context            Show full project context
  /setup              Capture basic project info (dev server, credentials)
  /clear              Clear conversation history
  /quit, /exit, /q    Exit the shell

Harness Workflows:
  /architect [cmd]    Run architect specification workflow
                      - (no args)        Smart-start auto-detection (default)
                      - new "idea"       Start new specification
                      - resume           Continue existing session
                      - audit            Check specification status (alias: audit-spec)
                      - add-feature "d"  Add feature to existing spec
                      - scan             Scan codebase for patterns
                      - docs [cmd]       Documentation (backfill|status)

  /loop [flags]       Run the implementation loop
                      - --autonomous, -a   Run without pausing
                      - --interactive, -i  Always pause at checkpoints
                      - --cadence/-c N     Checkpoint every N features

  /bug "description"  Quick bugfix entry (bypasses architect ceremony)
                      Creates feature → /loop does TDD fix → full verification

  /doctor [cmd]       Health diagnostics and stabilization
                      - (no args)   Interactive guided flow (default)
                      - status      Show current phase and progress
                      - next        Run the next phase (non-interactive)
                      - reset       Reset state and start over
                      - diagnose    Full health audit
                      - qa          QA verification checklist
                      - solidify    Generate baseline tests
                      - baseline    Test strategy recommendations
                      - stabilize   Generate fix tasks

  /docs [cmd]         Documentation generation
                      - status           Show documentation status (default)
                      - backfill         Generate docs for passing features
                      - generate <id>    Generate doc for specific feature

  /memory [cmd]       Working memory management
                      - status [comp]    Show memory stats (default)
                      - search <query>   Search learnings and archive
                      - clear <comp>     Clear memory for component

Tips:
  - Type a natural language question to query the codebase
  - Long-running commands (loop, architect) take over until done
  - Press Ctrl+C to interrupt commands gracefully
"""
    return help_text.strip()


@register_command("status", aliases=["s"])
def cmd_status(args: str) -> str:
    """Show project status summary."""
    status = get_quick_status()

    lines = ["Project Status", "=" * 40]

    # Features
    if status["features_total"] > 0:
        lines.append(f"\nFeatures: {status['features_passing']}/{status['features_total']} passing")
        if status["features_failing"]:
            lines.append(f"  Failing: {status['features_failing']}")
        if status["features_todo"]:
            lines.append(f"  Todo: {status['features_todo']}")
        if status["current_feature"]:
            lines.append(f"  Current: {status['current_feature']}")
    else:
        lines.append("\nNo features defined yet.")

    # Architect session
    if status["architect_phase"]:
        lines.append(f"\nArchitect: Gate {status['architect_phase']}")

    # Health
    if status["health_status"]:
        lines.append(f"\nHealth: {status['health_status'].upper()}")

    return "\n".join(lines)


@register_command("context")
def cmd_context(args: str) -> str:
    """Show full project context."""
    return build_project_context()


@register_command("clear")
def cmd_clear(args: str) -> str:
    """Clear conversation history (handled in shell.py)."""
    return "__CLEAR__"  # Special return value handled by shell


@register_command("setup")
def cmd_setup(args: str) -> str:
    """Capture basic project info (dev server, credentials, etc.)."""
    print("\n=== Project Setup ===")
    print("Capture basic info about your project for documentation and context.\n")

    # Load existing info if any
    existing = get_project_info() or {}

    def prompt_field(field: str, description: str, default: str = "") -> str:
        existing_val = existing.get(field, default)
        prompt_str = f"{description}"
        if existing_val:
            prompt_str += f" [{existing_val}]"
        prompt_str += ": "
        try:
            value = input(prompt_str).strip()
            return value if value else existing_val
        except (EOFError, KeyboardInterrupt):
            return existing_val

    info = {}

    info["name"] = prompt_field("name", "Project name")
    info["description"] = prompt_field("description", "Brief description")
    info["dev_server"] = prompt_field("dev_server", "Dev server command (e.g., npm run dev)")
    info["dev_url"] = prompt_field("dev_url", "Dev URL (e.g., http://localhost:3000)")

    # Credentials
    print("\nDefault credentials (for testing/dev):")
    has_creds = existing.get("default_credentials", {})
    username = prompt_field("username", "  Username", has_creds.get("username", ""))
    password = prompt_field("password", "  Password", has_creds.get("password", ""))
    if username or password:
        info["default_credentials"] = {"username": username, "password": password}

    info["notes"] = prompt_field("notes", "\nAny other important notes")

    # Remove empty fields
    info = {k: v for k, v in info.items() if v}

    if info:
        save_project_info(info)
        print("\nProject info saved to specs/project_info.json")
        return ""
    else:
        return "\nNo info provided, nothing saved."


@register_command("quit", aliases=["exit", "q"])
def cmd_quit(args: str) -> str:
    """Exit the shell."""
    return "__QUIT__"  # Special return value handled by shell


# =============================================================================
# Architect Commands
# =============================================================================

@register_command("architect", aliases=["arch"])
def cmd_architect(args: str) -> str:
    """Run architect specification workflow."""
    try:
        from architect import (
            cmd_new,
            cmd_resume,
            audit_spec,
            cmd_add_feature,
            cmd_scan,
            cmd_smart_start,
        )
    except ImportError as e:
        return f"Error: Could not import architect module: {e}"

    # Docs integration
    try:
        from docs import backfill_docs, cmd_status as docs_status
        docs_available = True
    except ImportError:
        docs_available = False

    args = args.strip()
    parts = args.split(maxsplit=1)
    subcommand = parts[0] if parts else ""
    sub_args = parts[1] if len(parts) > 1 else ""

    # Default: smart-start auto-detection
    if subcommand == "" or subcommand == "help":
        if subcommand == "":
            print("\nRunning smart-start auto-detection...\n")
            result = cmd_smart_start()
            return f"\nArchitect session completed with exit code: {result}"
        else:
            return """Usage: /architect [command]

Commands:
  (no args)         Smart-start - auto-detect project state (default)
  new "idea"        Start new specification session
  resume            Resume existing session
  audit             Check specification completeness
  audit-spec        Alias for audit
  add-feature "desc"  Add feature to existing spec (streamlined)
  scan              Scan codebase for patterns
  docs [cmd]        Documentation commands (backfill|status)
"""

    if subcommand == "new":
        if not sub_args:
            return "Usage: /architect new \"product idea\""
        # Remove quotes if present
        idea = sub_args.strip("\"'")
        print(f"\nStarting new specification for: {idea}\n")
        result = cmd_new(idea)
        return f"\nArchitect session completed with exit code: {result}"

    elif subcommand == "resume":
        print("\nResuming architect session...\n")
        result = cmd_resume()
        return f"\nArchitect session completed with exit code: {result}"

    elif subcommand == "audit" or subcommand == "audit-spec":
        result = audit_spec()
        return f"\nAudit completed with exit code: {result}"

    elif subcommand == "add-feature":
        if not sub_args:
            return "Usage: /architect add-feature \"feature description\""
        # Remove quotes if present
        description = sub_args.strip("\"'")
        print(f"\nAdding feature: {description}\n")
        result = cmd_add_feature(description)
        return f"\nAdd feature completed with exit code: {result}"

    elif subcommand == "scan":
        print("\nScanning codebase for patterns...\n")
        result = cmd_scan()
        return f"\nScan completed with exit code: {result}"

    elif subcommand == "docs":
        if not docs_available:
            return "Error: docs module not available"

        docs_cmd = sub_args.split()[0] if sub_args else "status"

        if docs_cmd == "backfill":
            print("\nBackfilling documentation...\n")
            result = backfill_docs()
            errors = result.get("errors", [])
            if errors:
                return f"\nBackfill completed with {len(errors)} errors"
            return f"\nBackfill completed: {len(result.get('generated', []))} docs generated"

        elif docs_cmd == "status":
            result = docs_status()
            return f"\nDocumentation status check completed with exit code: {result}"

        else:
            return """Usage: /architect docs [command]

Commands:
  status    Show documentation status (default)
  backfill  Generate docs for all passing features
"""

    else:
        return """Usage: /architect [command]

Commands:
  (no args)         Smart-start - auto-detect project state (default)
  new "idea"        Start new specification session
  resume            Resume existing session
  audit             Check specification completeness
  audit-spec        Alias for audit
  add-feature "desc"  Add feature to existing spec (streamlined)
  scan              Scan codebase for patterns
  docs [cmd]        Documentation commands (backfill|status)
"""


# =============================================================================
# Loop Commands
# =============================================================================

@register_command("loop")
def cmd_loop(args: str) -> str:
    """Run the implementation loop."""
    try:
        # Import main function from loop
        sys.path.insert(0, str(Path(__file__).parent / "coding"))
        from loop import main as loop_main, LoopMode, DEFAULT_CHECKPOINT_CADENCE
    except ImportError as e:
        return f"Error: Could not import loop module: {e}"

    # Parse args
    args = args.strip()
    autonomous = "--autonomous" in args or "-a" in args
    interactive = "--interactive" in args or "-i" in args
    cadence = DEFAULT_CHECKPOINT_CADENCE

    # Parse --cadence N
    if "--cadence" in args or "-c" in args:
        import re
        match = re.search(r'(?:--cadence|-c)\s+(\d+)', args)
        if match:
            cadence = int(match.group(1))

    # Determine mode (interactive takes precedence if both specified)
    if interactive:
        mode_str = "interactive"
    elif autonomous:
        mode_str = "autonomous"
    else:
        mode_str = "interactive"  # Default

    print(f"\nStarting implementation loop ({mode_str}, cadence={cadence})...\n")

    # Build sys.argv for the loop main function
    old_argv = sys.argv
    try:
        new_argv = ["loop.py"]
        if autonomous and not interactive:
            new_argv.append("--autonomous")
        elif interactive:
            new_argv.append("--interactive")
        new_argv.extend(["--cadence", str(cadence)])
        sys.argv = new_argv
        result = loop_main()
        return f"\nImplementation loop completed with exit code: {result}"
    finally:
        sys.argv = old_argv


# =============================================================================
# Bug Command (Quick Feature Entry for Bugfixes)
# =============================================================================

@register_command("bug", aliases=["fix"])
def cmd_bug(args: str) -> str:
    """
    Quick bug entry - creates a feature for the loop to fix.

    Bypasses architect ceremony but still goes through full TDD loop
    with verification, regression tests, etc.
    """
    import json
    import re
    from datetime import datetime, timezone

    FEATURES_PATH = Path("specs/features.json")

    args = args.strip()

    if not args or args == "help":
        return """Usage: /bug "description of the bug"

Creates a bugfix feature entry that goes through the full loop:
- TDD: Write failing test first
- Implementation: Fix the bug
- Verification: Harness verifies the fix
- Regression: Full test suite runs

Examples:
  /bug "Settings form throws error when saving API key"
  /bug "Login redirects to wrong page after authentication"
  /bug "Date picker shows wrong format in reports"

The bug will be added to specs/features.json with status 'todo'.
Run /loop to start working on it.
"""

    # Remove surrounding quotes if present
    description = args.strip("\"'")

    # Load existing features
    if FEATURES_PATH.exists():
        try:
            data = json.loads(FEATURES_PATH.read_text())
            features = data.get("features", [])
        except json.JSONDecodeError:
            return f"Error: {FEATURES_PATH} contains invalid JSON"
    else:
        # Create new features file
        FEATURES_PATH.parent.mkdir(parents=True, exist_ok=True)
        features = []
        data = {"features": features}

    # Find next bug number
    existing_bug_nums = []
    for f in features:
        fid = f.get("id", "")
        match = re.match(r"bug-(\d+)", fid)
        if match:
            existing_bug_nums.append(int(match.group(1)))

    next_num = max(existing_bug_nums, default=0) + 1

    # Generate slug from description (first few meaningful words)
    slug_words = re.findall(r'[a-zA-Z]+', description.lower())[:4]
    slug = "-".join(slug_words) if slug_words else "fix"

    # Create feature entry
    feature_id = f"bug-{next_num:03d}-{slug}"

    # Generate a concise name from description
    name = description[:60] + "..." if len(description) > 60 else description
    if not name.lower().startswith("fix"):
        name = f"Fix: {name}"

    new_feature = {
        "id": feature_id,
        "name": name,
        "description": description,
        "type": "bugfix",
        "status": "todo",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Add to features list
    features.append(new_feature)
    data["features"] = features

    # Save
    FEATURES_PATH.write_text(json.dumps(data, indent=2))

    # Count pending bugs and features
    pending = [f for f in features if f.get("status") in ("todo", "in_progress", "failing")]
    pending_bugs = [f for f in pending if f.get("type") == "bugfix"]

    return f"""
Bug added to specs/features.json:

  ID: {feature_id}
  Name: {name}
  Status: todo

Pending items: {len(pending)} ({len(pending_bugs)} bugs)

Run /loop to start the TDD fix cycle:
  1. Loop writes a failing test that reproduces the bug
  2. Loop implements the fix
  3. Harness verifies the test passes
  4. Full regression suite runs
  5. Commit on success
"""


# =============================================================================
# Doctor Commands
# =============================================================================

@register_command("doctor", aliases=["doc"])
def cmd_doctor(args: str) -> str:
    """Run health diagnostics and guided stabilization workflow."""
    try:
        from doctor import (
            cmd_diagnose,
            cmd_stabilize,
            cmd_baseline,
            cmd_qa,
            cmd_solidify,
            cmd_guided_flow,
            cmd_status,
            cmd_next_phase,
            cmd_reset,
        )
    except ImportError as e:
        return f"Error: Could not import doctor module: {e}"

    args = args.strip()
    subcommand = args.split()[0] if args else ""

    # Guided workflow commands
    if subcommand == "" or subcommand == "flow":
        # Default: interactive guided flow
        result = cmd_guided_flow()
        return f"\nGuided flow completed with exit code: {result}"

    elif subcommand == "status":
        result = cmd_status()
        return ""  # status already prints output

    elif subcommand == "next":
        result = cmd_next_phase()
        return f"\nPhase completed with exit code: {result}"

    elif subcommand == "reset":
        result = cmd_reset()
        return ""  # reset already prints output

    # Phase commands (standalone)
    elif subcommand == "diagnose":
        print("\nRunning health diagnosis...\n")
        result = cmd_diagnose()
        return f"\nDiagnosis completed with exit code: {result}"

    elif subcommand == "stabilize":
        print("\nGenerating stabilization tasks...\n")
        result = cmd_stabilize()
        return f"\nStabilization completed with exit code: {result}"

    elif subcommand == "baseline":
        print("\nGenerating test strategy...\n")
        result = cmd_baseline()
        return f"\nBaseline completed with exit code: {result}"

    elif subcommand == "qa":
        print("\nGenerating QA checklist...\n")
        result = cmd_qa()
        return f"\nQA plan generated with exit code: {result}"

    elif subcommand == "solidify":
        print("\nGenerating baseline tests from verified QA...\n")
        result = cmd_solidify()
        return f"\nSolidify completed with exit code: {result}"

    else:
        return """Usage: /doctor [command]

Guided Workflow (recommended for brownfield projects):
  (no args)   Interactive guided flow through all phases
  status      Show current phase and progress
  next        Run the next phase (non-interactive)
  reset       Reset state and start over

Individual Phase Commands:
  diagnose    Phase 1: Full health audit (ASSESS)
  qa          Phase 2: Manual QA verification checklist (VERIFY)
  solidify    Phase 3: Generate baseline tests (PROTECT)
  baseline    Phase 4: Test strategy recommendations (COVERAGE)
  stabilize   Phase 5: Generate fix tasks (FIX)

The guided flow walks you through: ASSESS → VERIFY → PROTECT → COVERAGE → FIX
"""


# =============================================================================
# Docs Commands
# =============================================================================

@register_command("docs")
def cmd_docs(args: str) -> str:
    """Documentation generation commands."""
    try:
        from docs import backfill_docs, cmd_status as docs_status, cmd_generate
    except ImportError as e:
        return f"Error: Could not import docs module: {e}"

    args = args.strip()
    parts = args.split(maxsplit=1)
    subcommand = parts[0] if parts else "status"
    sub_args = parts[1] if len(parts) > 1 else ""

    if subcommand == "backfill":
        print("\nBackfilling documentation...\n")
        result = backfill_docs()
        errors = result.get("errors", [])
        if errors:
            return f"\nBackfill completed with {len(errors)} errors"
        return f"\nBackfill completed: {len(result.get('generated', []))} docs generated"

    elif subcommand == "status":
        result = docs_status()
        return f"\nDocumentation status check completed with exit code: {result}"

    elif subcommand == "generate":
        if not sub_args:
            return "Usage: /docs generate <feature_id>"
        feature_id = sub_args.strip()
        print(f"\nGenerating documentation for feature: {feature_id}\n")
        result = cmd_generate(feature_id)
        return f"\nGenerate completed with exit code: {result}"

    else:
        return """Usage: /docs [command]

Commands:
  status              Show documentation status (default)
  backfill            Generate docs for all passing features
  generate <id>       Generate doc for specific feature
"""


# =============================================================================
# Memory Commands
# =============================================================================

@register_command("memory", aliases=["mem"])
def cmd_memory(args: str) -> str:
    """Working memory management commands."""
    try:
        from memory import (
            read_memory,
            clear_memory,
            search_learnings,
            search_archive,
            search_similar_problems,
            format_historical_matches,
            MEMORY_DIR,
            INDEX_PATH,
            ATTEMPT_JOURNAL_AVAILABLE,
        )
    except ImportError as e:
        return f"Error: Could not import memory module: {e}"

    import json
    from pathlib import Path

    args = args.strip()
    parts = args.split(maxsplit=1)
    subcommand = parts[0] if parts else "status"
    sub_args = parts[1] if len(parts) > 1 else ""

    if subcommand == "status":
        # Show memory stats
        lines = ["\n=== Working Memory Status ===\n"]

        # Check memory directory
        if not MEMORY_DIR.exists():
            lines.append("Memory directory: Not created yet")
        else:
            # List memory files
            memory_files = list(MEMORY_DIR.glob("*.md"))
            lines.append(f"Memory directory: {MEMORY_DIR}")
            lines.append(f"Memory files: {len(memory_files)}")

            if memory_files:
                lines.append("\nComponents:")
                for mf in memory_files:
                    mem = read_memory(mf.stem)
                    q_count = len(mem.questions)
                    d_count = len(mem.decisions)
                    lines.append(f"  - {mf.stem}: {q_count} Q&A, {d_count} decisions")

        # Check index
        if INDEX_PATH.exists():
            try:
                index = json.loads(INDEX_PATH.read_text())
                lines.append(f"\nIndex entries: {len(index)}")
            except json.JSONDecodeError:
                lines.append("\nIndex: Invalid JSON")
        else:
            lines.append("\nIndex: Not created yet")

        # Check learnings
        learnings_path = Path("specs/learnings.json")
        if learnings_path.exists():
            try:
                data = json.loads(learnings_path.read_text())
                learnings = data.get("learnings", [])
                lines.append(f"Learnings: {len(learnings)} entries")
            except json.JSONDecodeError:
                lines.append("Learnings: Invalid JSON")
        else:
            lines.append("Learnings: Not created yet")

        # Check attempt journal availability
        lines.append(f"\nAttempt journal available: {ATTEMPT_JOURNAL_AVAILABLE}")

        if sub_args:
            # Show detail for specific component
            component = sub_args.strip()
            mem = read_memory(component)
            if mem.questions or mem.decisions or mem.understanding:
                lines.append(f"\n=== {component} Memory Detail ===")
                lines.append(f"Questions: {len(mem.questions)}")
                for q, entry in mem.questions.items():
                    lines.append(f"  Q: {q[:60]}...")
                    lines.append(f"  A: {entry.answer[:60]}...")
                lines.append(f"Decisions: {len(mem.decisions)}")
                for d in mem.decisions:
                    lines.append(f"  - {d.summary}")
            else:
                lines.append(f"\nNo memory found for component: {component}")

        return "\n".join(lines)

    elif subcommand == "search":
        if not sub_args:
            return "Usage: /memory search <query>"

        query = sub_args.strip()
        print(f"\nSearching for: {query}\n")

        # Search learnings
        learning_matches = search_learnings(query, max_results=5)

        # Search archive if available
        archive_matches = []
        if ATTEMPT_JOURNAL_AVAILABLE:
            archive_matches = search_archive(error_query=query, max_results=5)

        all_matches = learning_matches + archive_matches
        all_matches.sort(key=lambda m: m.similarity_score, reverse=True)
        all_matches = all_matches[:10]  # Top 10

        if all_matches:
            return format_historical_matches(all_matches)
        else:
            return "No matches found."

    elif subcommand == "clear":
        if not sub_args:
            return """Usage: /memory clear <component|all>

Components: architect, doctor, loop, or 'all' for everything

WARNING: This permanently deletes stored memory!"""

        target = sub_args.strip().lower()

        if target == "all":
            # Clear all memory
            if MEMORY_DIR.exists():
                for mf in MEMORY_DIR.glob("*.md"):
                    mf.unlink()
                if INDEX_PATH.exists():
                    INDEX_PATH.unlink()
            return "All memory cleared."
        else:
            # Clear specific component
            clear_memory(target)
            return f"Memory cleared for component: {target}"

    else:
        return """Usage: /memory [command]

Commands:
  status [component]  Show memory stats (default)
  search <query>      Search learnings and archive
  clear <component>   Clear memory (component name or 'all')
"""


# =============================================================================
# Command Dispatcher
# =============================================================================

def parse_command(input_str: str) -> tuple[str, str]:
    """
    Parse a slash command into command name and arguments.

    Returns (command_name, args) or (None, None) if not a command.
    """
    if not input_str.startswith("/"):
        return None, None

    # Remove leading slash
    input_str = input_str[1:]

    # Split into command and args
    parts = input_str.split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    return command, args


def handle_slash_command(input_str: str) -> Optional[str]:
    """
    Handle a slash command, dispatching to the appropriate handler.

    Returns:
        - Response string for display
        - "__QUIT__" to signal exit
        - "__CLEAR__" to signal history clear
        - None if not a valid command
    """
    command, args = parse_command(input_str)

    if command is None:
        return None

    handler = COMMANDS.get(command)
    if handler is None:
        # Suggest similar commands
        similar = [c for c in COMMANDS if c.startswith(command[:2])]
        if similar:
            suggestions = ", ".join(f"/{c}" for c in similar[:5])
            return f"Unknown command: /{command}\nDid you mean: {suggestions}"
        return f"Unknown command: /{command}\nType /help for available commands."

    try:
        return handler(args)
    except KeyboardInterrupt:
        return "\n(Command interrupted)"
    except Exception as e:
        return f"Error executing command: {e}"


def get_command_completions(partial: str) -> list[str]:
    """
    Get possible command completions for tab completion.

    Args:
        partial: Partial command (without leading /)

    Returns:
        List of matching command names
    """
    partial = partial.lower()
    return sorted([c for c in COMMANDS if c.startswith(partial)])


if __name__ == "__main__":
    # Test command handling
    test_commands = [
        "/help",
        "/status",
        "/architect audit",
        "/unknown",
        "not a command",
    ]

    for cmd in test_commands:
        print(f"\n>>> {cmd}")
        result = handle_slash_command(cmd)
        if result:
            print(result[:500])
        else:
            print("(not a command)")
