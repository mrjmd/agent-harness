#!/usr/bin/env python3
"""
Shared Claude CLI interface with streaming and tool safety.

This module centralizes all Claude CLI calls with:
- Streaming output support for real-time feedback
- Configurable tool restrictions for safety
- Appropriate timeouts per use case

Tool Access Matrix:
- Architect:   Read-only (Read, Glob, Grep, WebFetch, WebSearch)
- Implementer: Full access (all tools)
- Reviewer:    Read-only (Read, Glob, Grep)
- Doctor:      Read-only (Read, Glob, Grep)
"""

import json
import select
import subprocess
import sys
import time
from typing import Optional


def call_claude_cli(
    prompt_text: str,
    timeout: int = 600,
    allowed_tools: Optional[list[str]] = None,
    stream: bool = True,
    label: str = "Claude"
) -> str:
    """
    Call Claude CLI with streaming output and optional tool restrictions.

    Args:
        prompt_text: The prompt to send
        timeout: Activity timeout in seconds (default 10 min)
        allowed_tools: List of tools to allow, or None for all tools
                      Use [] to disable all tools
                      Use ["Read", "Glob", "Grep"] for read-only
        stream: Whether to stream output in real-time
        label: Label for progress messages

    Returns:
        The complete response text from Claude
    """
    cmd = ["claude", "--print", "--dangerously-skip-permissions"]

    # Add tool restrictions if specified
    if allowed_tools is not None:
        if len(allowed_tools) == 0:
            cmd.extend(["--tools", ""])
        else:
            cmd.extend(["--tools", ",".join(allowed_tools)])

    if stream:
        cmd.extend(["--output-format", "stream-json", "--verbose", "--include-partial-messages"])

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        process.stdin.write(prompt_text)
        process.stdin.close()

        full_response = []
        final_result = None
        last_activity = time.time()

        while True:
            if time.time() - last_activity > timeout:
                process.kill()
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            if ready:
                line = process.stdout.readline()
                if not line:
                    break
                last_activity = time.time()

                if stream:
                    try:
                        data = json.loads(line)
                        if data.get("type") == "stream_event":
                            event = data.get("event", {})
                            if event.get("type") == "content_block_delta":
                                delta = event.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    text = delta.get("text", "")
                                    full_response.append(text)
                                    print(text, end="", flush=True)
                        elif data.get("type") == "result":
                            final_result = data.get("result", "")
                    except json.JSONDecodeError:
                        pass
                else:
                    full_response.append(line)

            if process.poll() is not None:
                break

        print()  # Newline after streaming

        # Drain remaining output
        remaining = process.stdout.read()
        if remaining and stream:
            for line in remaining.strip().split("\n"):
                if line:
                    try:
                        data = json.loads(line)
                        if data.get("type") == "result":
                            final_result = data.get("result", "")
                    except json.JSONDecodeError:
                        pass

        process.wait()

        if process.returncode != 0:
            stderr = process.stderr.read()
            raise subprocess.CalledProcessError(process.returncode, "claude", stderr=stderr)

        if final_result is not None:
            return final_result
        return "".join(full_response)

    except FileNotFoundError:
        print("ERROR: 'claude' CLI not found.")
        print("Install: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)


# =============================================================================
# Convenience wrappers with appropriate defaults
# =============================================================================

# Read-only tools for specification and analysis phases
READ_ONLY_TOOLS = ["Read", "Glob", "Grep", "WebFetch", "WebSearch"]

# Strict read-only tools (no web access)
STRICT_READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]


def call_architect(prompt_text: str, timeout: int = 600) -> str:
    """
    Call Claude for specification work - READ-ONLY tools.

    The architect phase should only read existing code to understand context.
    It should NOT create or modify any files - that's the harness's job
    based on Claude's text output.
    """
    return call_claude_cli(
        prompt_text,
        timeout=timeout,
        allowed_tools=READ_ONLY_TOOLS,
        stream=True,
        label="Architect"
    )


def call_implementer(prompt_text: str, timeout: int = 1800) -> str:
    """
    Call Claude for implementation work - FULL tool access.

    The implementation phase needs Edit, Write, Bash, etc. to create
    and modify project files.
    """
    return call_claude_cli(
        prompt_text,
        timeout=timeout,
        allowed_tools=None,  # All tools
        stream=True,
        label="Implementer"
    )


def call_reviewer(prompt_text: str, timeout: int = 300) -> str:
    """
    Call Claude for code review - READ-ONLY tools (strict).

    Reviewers should only read code, not modify it.
    No web access needed for review.
    """
    return call_claude_cli(
        prompt_text,
        timeout=timeout,
        allowed_tools=STRICT_READ_ONLY_TOOLS,
        stream=True,
        label="Reviewer"
    )


def call_doctor(prompt_text: str, timeout: int = 120) -> str:
    """
    Call Claude for health analysis - READ-ONLY tools (strict).

    Doctor/analysis phase should only read and analyze, not modify.
    """
    return call_claude_cli(
        prompt_text,
        timeout=timeout,
        allowed_tools=STRICT_READ_ONLY_TOOLS,
        stream=True,
        label="Doctor"
    )


def call_reflection(prompt_text: str, timeout: int = 300) -> str:
    """
    Call Claude for learning extraction - READ-ONLY tools (strict).

    Reflection phase extracts lessons from past work, doesn't modify anything.
    """
    return call_claude_cli(
        prompt_text,
        timeout=timeout,
        allowed_tools=STRICT_READ_ONLY_TOOLS,
        stream=True,
        label="Reflection"
    )
