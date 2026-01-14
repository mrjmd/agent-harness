# Agent Harness

Two-phase autonomous coding: rigorous specification followed by TDD execution.

## Overview

This harness provides a template for autonomous coding agents that:

1. **Refuses to code without specs** - The Architect interrogates you until ambiguity is eliminated
2. **Follows TDD strictly** - Test first, then implementation
3. **Verifies externally** - The harness runs tests, not the agent (catches hallucinations)
4. **Checkpoints with git** - Commits on green, rolls back on red
5. **Transfers knowledge** - Lessons learned persist between features
6. **Respects existing code** - Extracts and enforces patterns from brownfield projects

## Quick Start

```bash
# 1. Specify your product (required first)
python harness/architect.py new "Your product idea"

# 2. Set API key for coding loop (uses Anthropic SDK)
export ANTHROPIC_API_KEY=your-key

# 3. Execute the implementation
python harness/coding/loop.py
```

> **Note:** The specification phase uses the `claude` CLI (install via `npm install -g @anthropic-ai/claude-code`). The coding loop uses the Anthropic SDK directly.

## The Two Harnesses

### Specification Harness (`harness/architect.py`)

Adversarial interrogation that refuses to generate specs until ambiguity is eliminated.

**The Five Gates:**

| Gate | Name | Purpose |
|------|------|---------|
| 1 | Problem Discovery | Understand WHY, not HOW |
| 2 | Solution Space | Explore 3+ alternatives before committing |
| 3 | Technical Design | Lock down architecture -> `specs/tech_plan.md` |
| 4 | Edge Cases | Force 3+ edge cases per feature (Rule of 3) |
| 5 | Synthesis | Generate `specs/features.json` |

**Key behaviors:**
- Challenges vague terms ("login" -> "Magic link? Password? OAuth?")
- Enforces granularity (>1 day = must decompose)
- Demands falsifiability ("User can log in" -> "User sees dashboard within 2s after valid credentials")
- Won't generate specs until all criteria are met

### Coding Harness (`harness/coding/`)

TDD implementation with external verification.

**Key behaviors:**
- MCP-native tool execution (actually runs tools, not just talks about them)
- Harness runs tests, not the agent (catches hallucinations)
- Git checkpoint on green, rollback on red
- Regression fence: ALL tests must pass before feature is marked complete
- Pattern enforcement: blocks unauthorized imports and forbidden patterns
- Reflection: extracts lessons for future agents

## Commands

| Command | Purpose |
|---------|---------|
| `python harness/architect.py new "idea"` | Start new specification session |
| `python harness/architect.py resume` | Continue existing specification |
| `python harness/architect.py audit` | Check specification completeness |
| `python harness/architect.py add-feature "desc"` | Add feature to existing spec |
| `python harness/architect.py scan` | Extract patterns from existing codebase |
| `python harness/coding/loop.py` | Run the coding loop |

## Key Files

| File | Purpose |
|------|---------|
| `specs/features.json` | Feature backlog (shared contract between architect and loop) |
| `specs/session.json` | Architect session state (for resume) |
| `specs/tech_plan.md` | Technical architecture (Gate 3 output) |
| `specs/learnings.json` | Lessons from completed features |
| `specs/context/patterns.md` | Extracted codebase patterns (brownfield) |
| `.claude/CLAUDE.md` | Constitution for coding agents |

## Features.json Schema

The shared contract between Architect and Coding Loop:

```json
{
  "features": [
    {
      "id": "auth-001-email-login",
      "description": "When user submits valid email/password, they see dashboard within 2s",
      "acceptance_criteria": "Dashboard page visible with user's name displayed",
      "edge_cases": [
        {
          "id": "invalid_credentials",
          "description": "User submits wrong password",
          "expected_behavior": "Show 'Invalid email or password' error"
        },
        {
          "id": "empty_fields",
          "description": "User submits empty email",
          "expected_behavior": "Show 'Email required' validation error"
        },
        {
          "id": "lockout",
          "description": "User fails 5 times in 15 minutes",
          "expected_behavior": "Show 'Too many attempts' error"
        }
      ],
      "priority": 1,
      "status": "todo"
    }
  ]
}
```

**Required fields (set by Architect):**
- `id` - Kebab-case identifier
- `description` - Falsifiable statement
- `acceptance_criteria` - Single testable assertion
- `edge_cases` - Array with minimum 3 entries
- `priority` - Integer (1 = highest)

**Runtime fields (set by Coding Loop):**
- `status` - todo | in_progress | failing | passing | blocked
- `test_file` - Path to generated test
- `last_updated` - ISO timestamp
- `retries` - Failed attempt count

## Installation

### Bootstrap a new project

```bash
# From an existing agent-harness checkout
./harness/bootstrap.sh /path/to/your/project
```

### Manual setup

```bash
# Install Claude CLI (for specification phase)
npm install -g @anthropic-ai/claude-code

# Install Python dependencies (for coding loop)
pip install anthropic

# Install Playwright (for testing)
npm install --save-dev @playwright/test
npx playwright install chromium
```

## Architecture

```
agent-harness/
├── harness/
│   ├── architect.py          # Specification REPL
│   ├── archaeologist.py      # Pattern extraction (brownfield)
│   ├── coding/               # Execution harness
│   │   ├── loop.py           # Main orchestration
│   │   ├── mcp_manager.py    # MCP server lifecycle
│   │   ├── verify.py         # External test verification
│   │   ├── git_utils.py      # Checkpoint/rollback
│   │   ├── repo_map.py       # Codebase structure
│   │   ├── reflection.py     # Lesson extraction
│   │   └── review.py         # Pattern enforcement
│   ├── bootstrap.sh          # Project setup script
│   └── templates/            # Template files
├── specs/
│   ├── features.json         # Feature backlog
│   ├── session.json          # Architect state
│   ├── tech_plan.md          # Technical architecture
│   ├── learnings.json        # Accumulated lessons
│   └── context/
│       └── patterns.md       # Extracted patterns
├── tests/e2e/                # Playwright tests
└── .claude/
    └── CLAUDE.md             # Agent constitution
```

## How It Works

### Specification Phase

```
User: "I want to build a todo app"
     ↓
┌────────────────┐
│   GATE 1       │  "WHY do you need this? WHO uses it?"
│   Problem      │
└───────┬────────┘
        ↓
┌────────────────┐
│   GATE 2       │  "What are THREE ways to solve this?"
│   Solution     │  "What are the trade-offs?"
└───────┬────────┘
        ↓
┌────────────────┐
│   GATE 3       │  "What data models? APIs? Patterns?"
│   Technical    │  → specs/tech_plan.md
└───────┬────────┘
        ↓
┌────────────────┐
│   GATE 4       │  "What if the network is down?"
│   Edge Cases   │  "What if the user enters nothing?"
└───────┬────────┘
        ↓
┌────────────────┐
│   GATE 5       │  Validates all criteria
│   Synthesis    │  → specs/features.json
└────────────────┘
```

### Coding Phase

```
specs/features.json
     ↓
┌────────────────┐
│  Find next     │  status: todo → in_progress
│  feature       │
└───────┬────────┘
        ↓
┌────────────────┐
│  Write test    │  RED: Test must fail first
│  (TDD)         │
└───────┬────────┘
        ↓
┌────────────────┐
│  Implement     │  GREEN: Minimum code to pass
│                │
└───────┬────────┘
        ↓
┌────────────────┐
│  Verify        │  Harness runs test externally
│  (external)    │  Catches hallucinations
└───────┬────────┘
        ↓
┌────────────────┐
│  Pattern       │  Check import compliance
│  check         │  Enforce forbidden patterns
└───────┬────────┘
        ↓
┌────────────────┐
│  Regression    │  ALL tests must pass
│  check         │  No breaking existing features
└───────┬────────┘
        ↓
┌────────────────┐
│  Commit        │  Git checkpoint on success
│                │  Rollback on failure
└───────┬────────┘
        ↓
┌────────────────┐
│  Reflect       │  Extract lessons learned
│                │  → specs/learnings.json
└────────────────┘
```

## Brownfield Support

When working in an existing codebase, the harness extracts and enforces patterns to prevent "pattern drift."

### Pattern Extraction

Run manually or auto-triggered when starting `architect.py new` in a folder with existing code:

```bash
python harness/architect.py scan
```

This analyzes:
- `.claude/CLAUDE.md` - Project constitution
- `package.json` / `requirements.txt` - Dependencies
- Source directory structure
- 5 representative files (components, pages, APIs, utilities, tests)

Output: `specs/context/patterns.md` containing:
- Naming conventions
- Import patterns
- Component structure
- API patterns
- Forbidden patterns
- Required libraries

### Pattern Enforcement

During the coding loop, after tests pass but before committing:

1. **Import validation** - Checks that all imports exist in `package.json`
2. **Forbidden pattern check** - Scans for patterns marked as forbidden
3. **Feedback loop** - Violations are fed back to agent for correction

```
Test passes → Pattern check → Regression check → Commit
                   ↓
              Violation?
                   ↓
           Feedback to agent
```

### Usage

```bash
# Existing project - patterns extracted automatically
cd existing-project
python harness/architect.py new "Add user preferences"
# → Detects src/ or package.json
# → Runs archaeologist.py automatically
# → specs/context/patterns.md created

# Manual extraction
python harness/architect.py scan

# Coding loop enforces patterns
python harness/coding/loop.py
# → Pattern violations block commits
# → Agent must fix before proceeding
```

## License

MIT
