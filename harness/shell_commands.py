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
  /architect [new|resume|audit]
                      Run architect specification workflow
                      - new "idea"  Start new specification
                      - resume      Continue existing session
                      - audit       Check specification status

  /loop [--autonomous] [--cadence N]
                      Run the implementation loop
                      - --autonomous, -a  Run without pausing at checkpoints
                      - --cadence N       Checkpoint every N features

  /doctor [diagnose|stabilize|baseline|qa|solidify]
                      Health diagnostics and stabilization
                      - diagnose    Full health audit (default)
                      - stabilize   Generate fix tasks
                      - baseline    Generate test strategy
                      - qa          Generate QA checklist
                      - solidify    Generate baseline tests

  /docs [backfill|status]
                      Documentation generation
                      - backfill   Generate docs for all passing features
                      - status     Show documentation status

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
        from architect import cmd_new, cmd_resume, audit_spec
    except ImportError as e:
        return f"Error: Could not import architect module: {e}"

    args = args.strip()
    parts = args.split(maxsplit=1)
    subcommand = parts[0] if parts else "resume"
    sub_args = parts[1] if len(parts) > 1 else ""

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

    elif subcommand == "audit":
        result = audit_spec()
        return f"\nAudit completed with exit code: {result}"

    else:
        return """Usage: /architect [command]

Commands:
  new "idea"   Start new specification session
  resume       Resume existing session (default)
  audit        Check specification completeness
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
    cadence = DEFAULT_CHECKPOINT_CADENCE

    # Parse --cadence N
    if "--cadence" in args or "-c" in args:
        import re
        match = re.search(r'(?:--cadence|-c)\s+(\d+)', args)
        if match:
            cadence = int(match.group(1))

    mode_str = "autonomous" if autonomous else "interactive"
    print(f"\nStarting implementation loop ({mode_str}, cadence={cadence})...\n")

    # Build sys.argv for the loop main function
    old_argv = sys.argv
    try:
        new_argv = ["loop.py"]
        if autonomous:
            new_argv.append("--autonomous")
        new_argv.extend(["--cadence", str(cadence)])
        sys.argv = new_argv
        result = loop_main()
        return f"\nImplementation loop completed with exit code: {result}"
    finally:
        sys.argv = old_argv


# =============================================================================
# Doctor Commands
# =============================================================================

@register_command("doctor", aliases=["doc"])
def cmd_doctor(args: str) -> str:
    """Run health diagnostics."""
    try:
        from doctor import (
            cmd_diagnose,
            cmd_stabilize,
            cmd_baseline,
            cmd_qa,
            cmd_solidify,
        )
    except ImportError as e:
        return f"Error: Could not import doctor module: {e}"

    args = args.strip()
    subcommand = args.split()[0] if args else "diagnose"

    if subcommand == "diagnose":
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

Commands:
  diagnose    Full health audit (default)
  stabilize   Generate fix tasks from health report
  baseline    Generate test strategy recommendations
  qa          Generate manual QA verification checklist
  solidify    Generate baseline tests from verified QA
"""


# =============================================================================
# Docs Commands
# =============================================================================

@register_command("docs")
def cmd_docs(args: str) -> str:
    """Documentation generation commands."""
    try:
        from docs import backfill_docs, cmd_status as docs_status
    except ImportError as e:
        return f"Error: Could not import docs module: {e}"

    args = args.strip()
    subcommand = args.split()[0] if args else "status"

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

    else:
        return """Usage: /docs [command]

Commands:
  status    Show documentation status (default)
  backfill  Generate docs for all passing features
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
