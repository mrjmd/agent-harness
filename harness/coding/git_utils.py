#!/usr/bin/env python3
"""
Git Utilities for Agent Harness

Provides checkpoint/commit/rollback functionality.
Simple and critical: commit on green, reset on red.
"""

import subprocess
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional


@dataclass
class GitStatus:
    """Current git repository status."""
    branch: str
    clean: bool
    staged_files: list[str]
    modified_files: list[str]
    untracked_files: list[str]


def get_status() -> GitStatus:
    """Get current git status."""
    # Get current branch
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

    # Get status
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True
    )

    staged = []
    modified = []
    untracked = []

    for line in status_result.stdout.strip().split("\n"):
        if not line:
            continue

        status_code = line[:2]
        filename = line[3:]

        if status_code[0] in "MADRC":  # Staged changes
            staged.append(filename)
        if status_code[1] in "MD":  # Unstaged changes
            modified.append(filename)
        if status_code == "??":  # Untracked
            untracked.append(filename)

    return GitStatus(
        branch=branch,
        clean=len(staged) == 0 and len(modified) == 0 and len(untracked) == 0,
        staged_files=staged,
        modified_files=modified,
        untracked_files=untracked
    )


def create_checkpoint(feature_id: str) -> str:
    """
    Create a checkpoint before starting work on a feature.

    Uses git stash as a lightweight checkpoint mechanism.
    Returns the stash reference or empty string if nothing to stash.
    """
    status = get_status()

    if status.clean:
        return ""  # Nothing to checkpoint

    # Stash all changes including untracked
    stash_msg = f"checkpoint-{feature_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    result = subprocess.run(
        ["git", "stash", "push", "-u", "-m", stash_msg],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        return stash_msg
    else:
        print(f"Warning: Failed to create checkpoint: {result.stderr}")
        return ""


def commit_feature(feature_id: str, description: str = "") -> bool:
    """
    Commit all changes for a passing feature.

    Returns True if commit succeeded, False otherwise.
    """
    # Stage all changes
    subprocess.run(["git", "add", "-A"], capture_output=True)

    status = get_status()
    if not status.staged_files:
        print("Nothing to commit")
        return True  # Not an error, just nothing to do

    # Build commit message
    if description:
        message = f"PASSING: {description}\n\nFeature ID: {feature_id}"
    else:
        message = f"PASSING: {feature_id}"

    # Commit
    result = subprocess.run(
        ["git", "commit", "-m", message, "--author", "Agent Harness <agent@harness.local>"],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        print(f"Committed: {message.split(chr(10))[0]}")
        return True
    else:
        print(f"Commit failed: {result.stderr}")
        return False


def rollback() -> bool:
    """
    Rollback all uncommitted changes.

    Used when verification fails after agent claimed success.
    """
    # Discard all staged and unstaged changes
    subprocess.run(["git", "checkout", "--", "."], capture_output=True)

    # Remove untracked files and directories
    result = subprocess.run(
        ["git", "clean", "-fd"],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        print("Rolled back all uncommitted changes")
        return True
    else:
        print(f"Rollback warning: {result.stderr}")
        return False


def rollback_to_checkpoint(stash_ref: str) -> bool:
    """
    Rollback to a specific checkpoint (stash).

    First discards current changes, then applies the stash.
    """
    # First, clean everything
    rollback()

    if not stash_ref:
        return True  # No checkpoint to restore

    # Find the stash by message
    result = subprocess.run(
        ["git", "stash", "list"],
        capture_output=True,
        text=True
    )

    stash_index = None
    for line in result.stdout.strip().split("\n"):
        if stash_ref in line:
            # Extract stash@{N} from the line
            stash_index = line.split(":")[0]
            break

    if stash_index:
        result = subprocess.run(
            ["git", "stash", "pop", stash_index],
            capture_output=True,
            text=True
        )
        return result.returncode == 0

    return False


def get_changed_files() -> list[str]:
    """Get list of all changed files (staged + unstaged + untracked)."""
    status = get_status()
    all_files = set()
    all_files.update(status.staged_files)
    all_files.update(status.modified_files)
    all_files.update(status.untracked_files)
    return list(all_files)


def get_diff(staged: bool = False) -> str:
    """Get git diff output."""
    cmd = ["git", "diff"]
    if staged:
        cmd.append("--staged")

    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout


def is_repo() -> bool:
    """Check if current directory is a git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True,
        text=True
    )
    return result.returncode == 0


def ensure_repo() -> bool:
    """Ensure current directory is a git repo, initialize if not."""
    if is_repo():
        return True

    print("Initializing git repository...")
    result = subprocess.run(
        ["git", "init"],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        # Create initial commit
        subprocess.run(["git", "add", "-A"], capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit (Agent Harness)", "--allow-empty"],
            capture_output=True
        )
        return True

    return False


# Test the module
if __name__ == "__main__":
    print("Testing git utilities...")

    if not is_repo():
        print("Not a git repository!")
    else:
        status = get_status()
        print(f"Branch: {status.branch}")
        print(f"Clean: {status.clean}")
        print(f"Staged: {status.staged_files}")
        print(f"Modified: {status.modified_files}")
        print(f"Untracked: {status.untracked_files}")
