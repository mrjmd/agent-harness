#!/usr/bin/env python3
"""
Documentation Generation Module

Auto-generates user documentation from feature specifications.

Features:
- Hybrid approach: Template structure with LLM-generated prose
- Idempotent: Tracks what's been documented, updates rather than duplicates
- Backfill capability: Can generate docs for all passing features retroactively
- Living docs: Updated as features complete
"""

import hashlib
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Shared CLI module
sys.path.insert(0, str(Path(__file__).parent))
from cli import call_reflection as _call_claude_cli


# Paths
SPECS_DIR = Path("specs")
DOCS_DIR = SPECS_DIR / "autodocs"  # Use specs/autodocs to avoid conflicts with project docs/
META_PATH = DOCS_DIR / "_meta.json"
INDEX_PATH = DOCS_DIR / "index.md"
FEATURES_DIR = DOCS_DIR / "features"
FEATURES_PATH = SPECS_DIR / "features.json"
PRODUCT_SPEC_PATH = SPECS_DIR / "product_spec.md"
PROJECT_INFO_PATH = SPECS_DIR / "project_info.json"  # Basic project info (dev server, credentials, etc.)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class DocumentedFeature:
    """Metadata about a documented feature."""
    doc_path: str
    feature_version: str  # Hash of feature spec for change detection
    generated_at: str


@dataclass
class DocMeta:
    """Documentation metadata tracking."""
    last_updated: str
    documented_features: dict  # feature_id -> DocumentedFeature as dict
    pending_features: list


# =============================================================================
# LLM Calls
# =============================================================================

def call_claude_cli(prompt_text: str) -> str:
    """
    Call claude CLI for documentation generation.

    Uses the shared CLI module with read-only tools and 5 min timeout.
    """
    return _call_claude_cli(prompt_text)


DESCRIPTION_PROMPT = """Based on this feature specification, write a clear 2-3 sentence description of what this feature does from a user's perspective. Be specific and focus on the value it provides.

Feature ID: {feature_id}
Description: {description}
Acceptance Criteria: {acceptance_criteria}

Write ONLY the description paragraph, no headers or formatting. Be concise and user-focused."""


USAGE_GUIDE_PROMPT = """Write step-by-step instructions for how a user would use this feature. Be specific and actionable. Include 3-5 numbered steps.

Feature ID: {feature_id}
Description: {description}
Acceptance Criteria: {acceptance_criteria}

Write ONLY the numbered steps, no headers or extra text. Format as:
1. First step...
2. Second step...
etc."""


INDEX_OVERVIEW_PROMPT = """Based on these feature descriptions, write a brief 2-3 sentence overview of what this application does. Focus on the main value proposition.

Features:
{features_summary}

Product Spec (if available):
{product_spec}

Write ONLY the overview paragraph, no headers."""


INDEX_GETTING_STARTED_PROMPT = """Based on these foundation features, write a brief "Getting Started" section (3-5 steps) for new users.

Foundation Features:
{foundation_features}

Write numbered steps for a new user to get started. Be practical and actionable."""


# =============================================================================
# Core Functions
# =============================================================================

def compute_feature_hash(feature: dict) -> str:
    """Compute a hash of the feature spec for change detection."""
    # Include fields that would affect documentation
    relevant = {
        "description": feature.get("description", ""),
        "edge_cases": feature.get("edge_cases", []),
        "acceptance_criteria": feature.get("acceptance_criteria", []),
    }
    content = json.dumps(relevant, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def load_meta(project_root: Path = None) -> DocMeta:
    """Load documentation metadata, creating default if missing."""
    if project_root is None:
        project_root = Path.cwd()

    meta_path = project_root / META_PATH

    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text())
            return DocMeta(
                last_updated=data.get("last_updated", ""),
                documented_features=data.get("documented_features", {}),
                pending_features=data.get("pending_features", [])
            )
        except (json.JSONDecodeError, KeyError):
            pass

    return DocMeta(
        last_updated="",
        documented_features={},
        pending_features=[]
    )


def save_meta(meta: DocMeta, project_root: Path = None) -> None:
    """Save documentation metadata."""
    if project_root is None:
        project_root = Path.cwd()

    meta_path = project_root / META_PATH
    meta_path.parent.mkdir(parents=True, exist_ok=True)

    # Update timestamp
    meta.last_updated = datetime.now(timezone.utc).isoformat()

    # Convert to dict, handling documented_features specially
    data = {
        "last_updated": meta.last_updated,
        "documented_features": meta.documented_features,
        "pending_features": meta.pending_features
    }

    meta_path.write_text(json.dumps(data, indent=2))


def needs_update(feature_id: str, feature: dict, meta: DocMeta) -> bool:
    """Check if a feature's documentation needs to be generated or updated."""
    if feature_id not in meta.documented_features:
        return True

    current_hash = compute_feature_hash(feature)
    stored_hash = meta.documented_features[feature_id].get("feature_version", "")

    return current_hash != stored_hash


def load_features(project_root: Path = None) -> dict:
    """Load features.json."""
    if project_root is None:
        project_root = Path.cwd()

    features_path = project_root / FEATURES_PATH

    if not features_path.exists():
        return {"features": []}

    try:
        return json.loads(features_path.read_text())
    except json.JSONDecodeError:
        return {"features": []}


def format_acceptance_criteria(feature: dict) -> str:
    """Format acceptance criteria for prompts."""
    criteria = feature.get("acceptance_criteria", [])
    if not criteria:
        return "(none specified)"

    if isinstance(criteria, list):
        return "\n".join(f"- {c}" for c in criteria)
    return str(criteria)


def format_edge_cases_table(feature: dict) -> str:
    """Format edge cases as a markdown table."""
    edge_cases = feature.get("edge_cases", [])
    if not edge_cases:
        return "| Scenario | Expected Behavior |\n|----------|-------------------|\n| (none documented) | - |"

    rows = ["| Scenario | Expected Behavior |", "|----------|-------------------|"]
    for ec in edge_cases:
        scenario = ec.get("description", ec.get("scenario", ""))
        behavior = ec.get("expected_behavior", "")
        # Escape pipe characters in content
        scenario = scenario.replace("|", "\\|")
        behavior = behavior.replace("|", "\\|")
        rows.append(f"| {scenario} | {behavior} |")

    return "\n".join(rows)


def generate_feature_doc(feature: dict, project_root: Path = None) -> str:
    """
    Generate documentation for a single feature.

    Uses hybrid approach: template structure with LLM-generated prose.
    """
    feature_id = feature.get("id", "unknown")
    description = feature.get("description", "")
    acceptance_criteria = format_acceptance_criteria(feature)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"  Generating description...")
    try:
        llm_description = call_claude_cli(DESCRIPTION_PROMPT.format(
            feature_id=feature_id,
            description=description,
            acceptance_criteria=acceptance_criteria
        )).strip()
    except Exception as e:
        print(f"  Warning: LLM description failed: {e}")
        llm_description = description

    print(f"  Generating usage guide...")
    try:
        llm_usage = call_claude_cli(USAGE_GUIDE_PROMPT.format(
            feature_id=feature_id,
            description=description,
            acceptance_criteria=acceptance_criteria
        )).strip()
    except Exception as e:
        print(f"  Warning: LLM usage guide failed: {e}")
        llm_usage = "1. Use the feature as described above."

    edge_cases_table = format_edge_cases_table(feature)

    # Technical notes from learnings (if available)
    technical_notes = get_relevant_learnings_for_feature(feature_id, project_root)
    if not technical_notes:
        technical_notes = "(No technical notes available)"

    # Build the document
    title = feature_id.replace("-", " ").title()

    doc = f"""# {title}

> Auto-generated documentation. Last updated: {timestamp}

## What This Does

{llm_description}

## How to Use It

{llm_usage}

## Edge Cases & Behavior

{edge_cases_table}

## Technical Notes

{technical_notes}
"""

    return doc


def get_relevant_learnings_for_feature(feature_id: str, project_root: Path = None) -> str:
    """Get learnings relevant to a specific feature."""
    if project_root is None:
        project_root = Path.cwd()

    learnings_path = project_root / SPECS_DIR / "learnings.json"

    if not learnings_path.exists():
        return ""

    try:
        data = json.loads(learnings_path.read_text())
        learnings = data.get("learnings", [])
    except (json.JSONDecodeError, KeyError):
        return ""

    # Filter learnings for this feature
    relevant = [l for l in learnings if l.get("feature_id") == feature_id]

    if not relevant:
        return ""

    lines = []
    for learning in relevant:
        lesson = learning.get("lesson", "")
        context = learning.get("context", "")
        category = learning.get("category", "")

        if lesson:
            lines.append(f"- **{category}**: {lesson}")
            if context:
                lines.append(f"  - Context: {context}")

    return "\n".join(lines) if lines else ""


def generate_doc_for_feature(feature: dict, project_root: Path = None) -> Optional[str]:
    """
    Generate and save documentation for a single feature.

    Returns the path to the generated doc, or None if skipped.
    """
    if project_root is None:
        project_root = Path.cwd()

    feature_id = feature.get("id", "unknown")
    meta = load_meta(project_root)

    # Check if update needed
    if not needs_update(feature_id, feature, meta):
        print(f"  Skipping {feature_id} (unchanged)")
        return None

    print(f"Generating documentation for: {feature_id}")

    # Generate the document
    doc_content = generate_feature_doc(feature, project_root)

    # Save the document
    features_dir = project_root / FEATURES_DIR
    features_dir.mkdir(parents=True, exist_ok=True)

    doc_path = features_dir / f"{feature_id}.md"
    doc_path.write_text(doc_content)

    # Update metadata
    meta.documented_features[feature_id] = {
        "doc_path": str(FEATURES_DIR / f"{feature_id}.md"),
        "feature_version": compute_feature_hash(feature),
        "generated_at": datetime.now(timezone.utc).isoformat()
    }

    # Remove from pending if present
    if feature_id in meta.pending_features:
        meta.pending_features.remove(feature_id)

    save_meta(meta, project_root)

    print(f"  Saved to {doc_path}")
    return str(doc_path)


def generate_index(project_root: Path = None) -> str:
    """
    Generate the documentation index (TOC with overview).

    This is more than a simple TOC - it's the user-facing README.
    """
    if project_root is None:
        project_root = Path.cwd()

    features_data = load_features(project_root)
    features = features_data.get("features", [])
    meta = load_meta(project_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Get passing features only
    passing_features = [f for f in features if f.get("status") == "passing"]

    if not passing_features:
        return f"""# Documentation

> Auto-generated documentation. Last updated: {timestamp}

No features have been completed yet.
"""

    # Build features summary for LLM
    features_summary = "\n".join(
        f"- {f.get('id')}: {f.get('description', '')}"
        for f in passing_features[:20]  # Limit for context
    )

    # Load product spec if available
    product_spec = ""
    product_spec_path = project_root / PRODUCT_SPEC_PATH
    if product_spec_path.exists():
        try:
            product_spec = product_spec_path.read_text()[:2000]  # Limit
        except Exception:
            pass

    # Generate overview
    print("Generating index overview...")
    try:
        overview = call_claude_cli(INDEX_OVERVIEW_PROMPT.format(
            features_summary=features_summary,
            product_spec=product_spec
        )).strip()
    except Exception as e:
        print(f"  Warning: LLM overview failed: {e}")
        overview = "This application provides the features documented below."

    # Find foundation features for getting started
    foundation_features = [
        f for f in passing_features
        if f.get("id", "").startswith("foundation-") or f.get("priority", 99) <= 3
    ]

    getting_started = ""
    if foundation_features:
        foundation_summary = "\n".join(
            f"- {f.get('id')}: {f.get('description', '')}"
            for f in foundation_features[:10]
        )

        print("Generating getting started guide...")
        try:
            getting_started = call_claude_cli(INDEX_GETTING_STARTED_PROMPT.format(
                foundation_features=foundation_summary
            )).strip()
            # Remove any leading header the LLM might have included
            getting_started = re.sub(r'^#+ .*\n+', '', getting_started)
        except Exception as e:
            print(f"  Warning: LLM getting started failed: {e}")
            getting_started = "1. Start the development server\n2. Follow the feature documentation below"

    # Group features by prefix (e.g., auth-*, posts-*)
    groups = {}
    for f in passing_features:
        feature_id = f.get("id", "unknown")
        parts = feature_id.split("-")
        if len(parts) >= 2:
            group = parts[0].title()
        else:
            group = "General"

        if group not in groups:
            groups[group] = []
        groups[group].append(f)

    # Build feature list
    feature_list_lines = []
    for group_name in sorted(groups.keys()):
        group_features = groups[group_name]
        feature_list_lines.append(f"\n### {group_name}\n")

        for f in sorted(group_features, key=lambda x: x.get("priority", 99)):
            feature_id = f.get("id", "unknown")
            title = feature_id.replace("-", " ").title()

            # Check if doc exists
            if feature_id in meta.documented_features:
                # Make path relative to the index location (docs/)
                doc_path = f"features/{feature_id}.md"
                feature_list_lines.append(f"- [{title}]({doc_path})")
            else:
                feature_list_lines.append(f"- {title} (documentation pending)")

    feature_list = "\n".join(feature_list_lines)

    # Build the index
    index_content = f"""# User Documentation

> Auto-generated documentation. Last updated: {timestamp}

## Overview

{overview}
"""

    if getting_started:
        index_content += f"""
## Getting Started

{getting_started}
"""

    index_content += f"""
## All Features
{feature_list}
"""

    return index_content


def save_index(project_root: Path = None) -> str:
    """Generate and save the documentation index."""
    if project_root is None:
        project_root = Path.cwd()

    index_content = generate_index(project_root)

    docs_dir = project_root / DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)

    index_path = docs_dir / "index.md"
    index_path.write_text(index_content)

    print(f"Saved index to {index_path}")
    return str(index_path)


# =============================================================================
# Integration Functions
# =============================================================================

def maybe_generate_doc(feature: dict, project_root: Path = None) -> Optional[str]:
    """
    Generate documentation for a feature if it's passing.

    Called from reflection.py after a feature completes.
    Returns the doc path if generated, None otherwise.
    """
    if feature.get("status") != "passing":
        return None

    doc_path = generate_doc_for_feature(feature, project_root)

    # Regenerate index if doc was created
    if doc_path:
        save_index(project_root)

    return doc_path


def backfill_docs(project_root: Path = None) -> dict:
    """
    Generate documentation for all passing features that don't have docs.

    Returns a summary of what was generated.
    """
    if project_root is None:
        project_root = Path.cwd()

    features_data = load_features(project_root)
    features = features_data.get("features", [])

    passing_features = [f for f in features if f.get("status") == "passing"]

    if not passing_features:
        print("No passing features found.")
        return {"generated": [], "skipped": [], "errors": []}

    print(f"Found {len(passing_features)} passing features")

    generated = []
    skipped = []
    errors = []

    for feature in passing_features:
        feature_id = feature.get("id", "unknown")
        try:
            doc_path = generate_doc_for_feature(feature, project_root)
            if doc_path:
                generated.append(feature_id)
            else:
                skipped.append(feature_id)
        except Exception as e:
            print(f"Error generating doc for {feature_id}: {e}")
            errors.append({"feature_id": feature_id, "error": str(e)})

    # Regenerate index
    if generated:
        save_index(project_root)

    print(f"\nSummary:")
    print(f"  Generated: {len(generated)}")
    print(f"  Skipped (unchanged): {len(skipped)}")
    print(f"  Errors: {len(errors)}")

    return {
        "generated": generated,
        "skipped": skipped,
        "errors": errors
    }


def update_pending_features(project_root: Path = None) -> None:
    """Update the list of pending features in metadata."""
    if project_root is None:
        project_root = Path.cwd()

    features_data = load_features(project_root)
    features = features_data.get("features", [])
    meta = load_meta(project_root)

    # Find passing features without docs
    pending = []
    for f in features:
        if f.get("status") == "passing":
            feature_id = f.get("id", "")
            if feature_id and feature_id not in meta.documented_features:
                pending.append(feature_id)

    meta.pending_features = pending
    save_meta(meta, project_root)


# =============================================================================
# CLI
# =============================================================================

def cmd_backfill() -> int:
    """Backfill documentation for all passing features."""
    print("Starting documentation backfill...\n")
    result = backfill_docs()

    if result["errors"]:
        return 1
    return 0


def cmd_status() -> int:
    """Show documentation status."""
    meta = load_meta()
    features_data = load_features()
    features = features_data.get("features", [])

    passing = [f for f in features if f.get("status") == "passing"]
    documented = len(meta.documented_features)

    print("Documentation Status")
    print("=" * 40)
    print(f"Passing features: {len(passing)}")
    print(f"Documented:       {documented}")
    print(f"Pending:          {len(passing) - documented}")

    if meta.last_updated:
        print(f"\nLast updated: {meta.last_updated}")

    if meta.pending_features:
        print(f"\nPending features:")
        for fid in meta.pending_features[:10]:
            print(f"  - {fid}")
        if len(meta.pending_features) > 10:
            print(f"  ... and {len(meta.pending_features) - 10} more")

    return 0


def cmd_generate(feature_id: str) -> int:
    """Generate documentation for a specific feature."""
    features_data = load_features()
    features = features_data.get("features", [])

    feature = next((f for f in features if f.get("id") == feature_id), None)
    if not feature:
        print(f"Feature not found: {feature_id}")
        return 1

    doc_path = generate_doc_for_feature(feature)
    if doc_path:
        save_index()
        print(f"\nGenerated: {doc_path}")
    else:
        print("No changes needed")

    return 0


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Documentation Generator - Auto-generate docs from feature specs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  /docs backfill     # Generate docs for all passing features (in harness shell)
  /docs status       # Show documentation status
  /docs generate auth-001-login  # Generate doc for specific feature
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # backfill command
    subparsers.add_parser("backfill", help="Generate docs for all passing features")

    # status command
    subparsers.add_parser("status", help="Show documentation status")

    # generate command
    gen_parser = subparsers.add_parser("generate", help="Generate doc for specific feature")
    gen_parser.add_argument("feature_id", help="Feature ID to document")

    args = parser.parse_args()

    if args.command == "backfill":
        return cmd_backfill()
    elif args.command == "status":
        return cmd_status()
    elif args.command == "generate":
        return cmd_generate(args.feature_id)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
