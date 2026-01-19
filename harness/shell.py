#!/usr/bin/env python3
"""
Harness Interactive Shell

A conversational interface for the agent harness that allows:
1. Natural language questions about your codebase/project
2. Slash commands to invoke harness workflows
3. Conversation context maintained across queries

Usage:
    python harness/shell.py

Commands start with /. Everything else is sent to Claude as a query.
"""

import readline
import sys
from pathlib import Path
from typing import Optional

# Add harness to path
sys.path.insert(0, str(Path(__file__).parent))

from cli import call_claude_cli, STRICT_READ_ONLY_TOOLS
from shell_commands import handle_slash_command, get_command_completions
from shell_context import build_project_context, get_quick_status, is_project_info_missing


# =============================================================================
# Configuration
# =============================================================================

VERSION = "0.1"
MAX_HISTORY_MESSAGES = 20
HISTORY_FILE = Path.home() / ".harness_history"


# =============================================================================
# Shell Class
# =============================================================================

class HarnessShell:
    """Interactive shell for the agent harness."""

    def __init__(self):
        self.conversation_history: list[dict] = []
        self.project_context: str = ""
        self.running = True

    def print_welcome(self) -> None:
        """Print welcome message and status summary."""
        status = get_quick_status()

        print(f"\nAgent Harness v{VERSION}")
        print("Type a message or use /commands. Try /help for available commands.\n")

        # Show quick status if project has content
        if status["features_total"] > 0:
            passing = status["features_passing"]
            total = status["features_total"]
            print(f"Features: {passing}/{total} passing", end="")
            if status["current_feature"]:
                print(f" | Current: {status['current_feature']}", end="")
            print()

        if status["architect_phase"]:
            print(f"Architect: Gate {status['architect_phase']}")

        if status["health_status"]:
            print(f"Health: {status['health_status'].upper()}")

        # Hint about setup if project info is missing
        if is_project_info_missing():
            print("\nTip: Run /setup to capture basic project info (dev server, credentials)")

        print()

    def setup_readline(self) -> None:
        """Configure readline for history and tab completion."""
        # Load history
        if HISTORY_FILE.exists():
            try:
                readline.read_history_file(str(HISTORY_FILE))
            except (IOError, OSError):
                pass

        # Set up tab completion
        readline.set_completer(self.completer)
        readline.parse_and_bind("tab: complete")

        # Configure history size
        readline.set_history_length(1000)

    def save_history(self) -> None:
        """Save readline history to file."""
        try:
            readline.write_history_file(str(HISTORY_FILE))
        except (IOError, OSError):
            pass

    def completer(self, text: str, state: int) -> Optional[str]:
        """Tab completion for commands."""
        if text.startswith("/"):
            # Complete command names
            partial = text[1:]  # Remove /
            matches = ["/" + c for c in get_command_completions(partial)]
        else:
            # No completion for regular text
            matches = []

        try:
            return matches[state]
        except IndexError:
            return None

    def refresh_context(self) -> None:
        """Refresh project context (called after commands complete)."""
        self.project_context = build_project_context()

    def format_history(self) -> str:
        """Format conversation history for inclusion in prompt."""
        if not self.conversation_history:
            return ""

        lines = []
        for msg in self.conversation_history[-MAX_HISTORY_MESSAGES:]:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            # Truncate long messages
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"{role}: {content}")

        return "\n\n".join(lines)

    def build_prompt(self, query: str) -> str:
        """Build the full prompt for Claude with project context and history."""
        history_str = self.format_history()

        prompt = f"""You are a helpful assistant for this software project. You are running inside the Agent Harness interactive shell.

PROJECT CONTEXT:
{self.project_context}

"""

        if history_str:
            prompt += f"""CONVERSATION HISTORY:
{history_str}

"""

        prompt += f"""USER QUERY:
{query}

Instructions:
- Answer questions about the codebase, features, and project status
- You have access to Read, Glob, and Grep tools to explore the codebase
- Be concise and helpful
- If asked about harness commands, mention /help
- Do not make changes to files - this is a read-only query mode
"""

        return prompt

    def query_claude(self, query: str) -> str:
        """
        Send a natural language query to Claude with project context.

        Uses read-only tools for safe exploration.
        """
        prompt = self.build_prompt(query)

        try:
            # Use read-only tools for shell queries
            response = call_claude_cli(
                prompt,
                timeout=120,
                allowed_tools=STRICT_READ_ONLY_TOOLS,
                stream=True,
                label="Shell"
            )

            # Update conversation history
            self.conversation_history.append({"role": "user", "content": query})
            self.conversation_history.append({"role": "assistant", "content": response})

            # Trim history if too long
            if len(self.conversation_history) > MAX_HISTORY_MESSAGES * 2:
                self.conversation_history = self.conversation_history[-MAX_HISTORY_MESSAGES * 2:]

            return ""  # Response already streamed

        except KeyboardInterrupt:
            print("\n(Query interrupted)")
            return ""
        except Exception as e:
            return f"Error querying Claude: {e}"

    def handle_input(self, user_input: str) -> bool:
        """
        Handle user input (command or query).

        Returns False if shell should exit.
        """
        user_input = user_input.strip()

        if not user_input:
            return True

        # Handle common exit commands without slash
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            return False

        # Check for slash command
        if user_input.startswith("/"):
            result = handle_slash_command(user_input)

            if result == "__QUIT__":
                print("Goodbye!")
                return False

            if result == "__CLEAR__":
                self.conversation_history = []
                print("Conversation history cleared.")
                return True

            # Print command result
            if result:
                print(result)

            # Refresh context after commands (they may have changed state)
            self.refresh_context()

            return True

        # Natural language query
        error = self.query_claude(user_input)
        if error:
            print(error)

        return True

    def run(self) -> int:
        """
        Main REPL loop.

        Returns exit code.
        """
        self.setup_readline()
        self.print_welcome()
        self.refresh_context()

        while self.running:
            try:
                user_input = input("> ")

                if not self.handle_input(user_input):
                    break

            except KeyboardInterrupt:
                print("\n(Use /quit to exit)")
            except EOFError:
                print("\nGoodbye!")
                break

        self.save_history()
        return 0


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    """Main entry point."""
    shell = HarnessShell()
    return shell.run()


if __name__ == "__main__":
    sys.exit(main())
