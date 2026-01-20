# Agent Constitution

This document defines the strict behavioral constraints for autonomous coding agents operating within this repository.

## Prime Directives

### 1. No Code Without Specs

**HALT** all code generation until `specs/features.json` exists and contains at least one feature.

Before writing any implementation code, you MUST verify:
- `specs/features.json` exists
- The file contains valid JSON with at least one feature entry
- There is a feature with status `in_progress` or `todo`

If these conditions are not met, respond with:
> "Cannot proceed. Missing specs/features.json. Please run the planning phase first."

### 2. Test First (Red-Green-Refactor)

You MUST follow Test-Driven Development strictly:

1. **RED**: Write a failing Playwright test in `tests/e2e/` that describes the expected behavior
2. **RUN**: Execute the test to confirm it fails (this validates the test is meaningful)
3. **GREEN**: Write the minimum code necessary to make the test pass
4. **RUN**: Execute the test to confirm it passes
5. **REFACTOR**: Clean up code while keeping tests green

**Never skip the failing test step.** A test that has never failed provides no confidence.

### 3. One Feature at a Time

You are only permitted to work on **one feature** at any moment:

- Find the feature in `specs/features.json` with status `in_progress`
- If none exists, find the first feature with status `todo` and set it to `in_progress`
- Complete that feature entirely before touching any other
- Never work on multiple features simultaneously

Feature lifecycle:
```
todo → in_progress → (failing) → passing
```

### 4. Git Checkpoints

Commit immediately when a test transitions from failing to passing:

```bash
git add -A
git commit -m "PASSING: [Feature Name]"
```

Commit message format is strict:
- `PASSING: <feature description>` — when test passes
- `WIP: <feature description>` — for intermediate work (use sparingly)
- `FAILING: <feature description>` — when adding a new failing test

### 5. Status Updates

After each significant action, update `specs/features.json`:

```json
{
  "id": "feature-001",
  "status": "passing",
  "last_updated": "2026-01-13T12:00:00Z"
}
```

Valid status transitions:
- `todo` → `in_progress`
- `in_progress` → `failing` (test written, not passing)
- `failing` → `passing` (implementation complete)
- `passing` → `failing` (regression detected)

## Forbidden Actions

1. **No implementation before test** — Writing feature code before its test exists
2. **No multi-feature work** — Touching files unrelated to the current feature
3. **No skipping failures** — Marking a test as passing without running it
4. **No manual status changes** — Status must reflect actual test results
5. **No commits without tests** — Every commit must have associated test coverage

## Test File Conventions

Test files must follow this structure:

```typescript
// tests/e2e/test_<feature-id>.spec.ts
import { test, expect } from '@playwright/test';

test.describe('<Feature Description>', () => {
  test('should <expected behavior>', async ({ page }) => {
    // Arrange
    // Act
    // Assert
  });
});
```

## Recovery Procedures

### If stuck in `failing` state for >3 attempts:
1. Re-read the feature specification
2. Simplify the test to the smallest failing case
3. Check for environmental issues (missing dependencies, wrong config)
4. If still blocked, add `"blocked": true` to the feature and document the reason

### If tests are flaky:
1. Add explicit waits for async operations
2. Isolate test state (no shared fixtures)
3. Mark as `"flaky": true` and fix before proceeding

## Context Files

Always read these files at the start of each session:
- `specs/features.json` — Current feature backlog and status
- `specs/product_spec.md` — High-level product requirements (if exists)
- `specs/tech_plan.md` — Technical architecture decisions (if exists)

## Harness Enforcement

The harness (`harness/loop.py`) enforces these rules automatically:

### External Verification

**Your claims of completion are verified independently.**

When you say "FEATURE PASSING" or indicate a test passes, the harness will:
1. Run `npx playwright test <test_file>` independently
2. Compare your claim against actual test results
3. Reject false claims and feed the actual error back to you

**Do not claim success unless you are certain.** The harness catches hallucinations.

### Regression Fence

Before any feature is marked as `passing`:
1. The harness runs the **entire test suite**
2. If ANY test fails (not just your feature's test), the feature is rejected
3. You must fix regressions before proceeding

**Your changes must not break existing features.**

### Git Checkpoints

The harness manages git automatically:
- **Checkpoint** created before each feature starts
- **Commit** only happens after harness verification passes
- **Rollback** happens automatically if you fail after max iterations

You do not need to run git commands manually.

### File Scope (Optional)

Features may define a `file_scope` that restricts which files you can modify:
```json
{
  "file_scope": {
    "create": ["src/components/LoginForm.tsx"],
    "modify": ["src/app/login/page.tsx"],
    "forbidden": [".env*", "package.json"]
  }
}
```

If defined, changes outside this scope will be rejected.

## Knowledge Transfer

### Reading Lessons

Check `specs/learnings.json` for lessons from previous features:
- API gotchas that previous agents discovered
- Package issues and workarounds
- Patterns that work well in this codebase

This knowledge prevents you from repeating past mistakes.

### Contributing Lessons

After your feature passes, the harness will ask you to reflect on:
- APIs that behaved unexpectedly
- Packages that had issues
- Patterns you discovered

Your lessons help future agents work more efficiently.

## Exit Conditions

Stop execution and report when:
1. All features have status `passing`
2. A feature is marked `blocked`
3. Maximum retry count (5) reached for a single feature
4. Critical error in test infrastructure

## Harness Development Rules

When modifying the harness codebase itself (files in `harness/`, `bin/`, etc.):

### Mandatory: Update Deployment Scripts When Adding New Files

**BEFORE completing any feature that adds new files to the harness, you MUST update BOTH:**

1. `bin/harness-up` - Updates existing projects
2. `harness/bootstrap.sh` - Initializes new projects

If you forget either one, users will get import errors or missing functionality.

### Checklist for new harness module:

**Step 1: Update `bin/harness-up`**
```bash
# Add to CORE_FILES array
"harness/coding/my_new_module.py"

# Add to the appropriate rsync command
"$HARNESS_SOURCE/harness/coding/my_new_module.py" \
```

**Step 2: Update `harness/bootstrap.sh`**
```bash
# Add cp command in the appropriate section
cp "$HARNESS_ROOT/harness/coding/my_new_module.py" harness/coding/
```

**Step 3: Verify**
```bash
# Test harness-up
cd /path/to/test/project
harness-up --dry-run
# Should show: + harness/coding/my_new_module.py

# Test bootstrap.sh
./harness/bootstrap.sh /tmp/test-project
ls /tmp/test-project/harness/coding/my_new_module.py
# Should exist
```

### Why this matters

Both scripts use **explicit file lists**, not directory syncing:
- `harness-up` has a `CORE_FILES` array and individual `rsync` commands
- `bootstrap.sh` has individual `cp` commands

This is intentional (allows excluding files) but means new files are silently ignored unless explicitly added to both scripts.

### Mandatory: Verify Integration Works

**After modifying harness code, ALWAYS verify the integration actually works:**

1. **Check feature availability flags:**
   ```bash
   cd /path/to/project && python -c "
   import sys
   from pathlib import Path
   sys.path.insert(0, str(Path('harness')))
   sys.path.insert(0, str(Path('harness/coding')))
   from loop import ATTEMPT_JOURNAL_AVAILABLE, MEMORY_AVAILABLE, REVIEW_BOARD_AVAILABLE
   print(f'ATTEMPT_JOURNAL_AVAILABLE: {ATTEMPT_JOURNAL_AVAILABLE}')
   print(f'MEMORY_AVAILABLE: {MEMORY_AVAILABLE}')
   print(f'REVIEW_BOARD_AVAILABLE: {REVIEW_BOARD_AVAILABLE}')
   "
   ```

2. **Run self-tests for new modules:**
   ```bash
   cd harness/coding && python attempt_journal.py
   cd harness/coding && python loop_detector.py
   ```

3. **Run pytest if tests exist:**
   ```bash
   python -m pytest harness/coding/tests/ -v
   ```

4. **Test the actual user path** - Don't assume imports work just because tests pass. Run `/loop` and verify the feature actually activates.

**Why this matters:** Silent `try/except ImportError` blocks can hide failures. The harness may appear to work but bypass new functionality entirely.

## Working Memory & Loop Detection

The harness includes automatic loop detection for stuck agents:

### How It Works
- **Attempt journals** stored at `specs/memory/attempts/{feature_id}.json`
- **Loop detection** analyzes patterns across attempts
- **Escalation** pauses execution when truly stuck

### Thresholds
- Same error: 3+ occurrences triggers warning
- Same approach: 2+ repetitions triggers warning
- Reviewer ping-pong: 3+ consecutive rejections

### Debugging Loop Detection
If you're stuck but not seeing loop warnings, check:
1. `ATTEMPT_JOURNAL_AVAILABLE` is True in the running process
2. Files exist in `specs/memory/attempts/`
3. You're claiming completion (recording only happens on `IMPLEMENTATION COMPLETE`)
4. Loop detection starts checking after iteration 3

## Shell Commands Are the Primary Interface

The file `harness/shell_commands.py` is the **primary user interface** for the harness. All module capabilities should be exposed through shell commands.

### Keeping Commands Aligned with Modules

When a backing module gains new features, the shell command must be updated too:

1. **Check module exports** - Review what functions the module exposes
2. **Update shell command** - Add handlers for new subcommands/flags
3. **Update help text** - Both in cmd_help() and in the command's own help

### Current Command → Module Mapping

| Command | Module | Key Functions |
|---------|--------|---------------|
| `/architect` | architect.py | cmd_new, cmd_resume, audit_spec, cmd_add_feature, cmd_scan, cmd_smart_start |
| `/bug` | shell_commands.py | Direct feature entry for bugfixes (no backing module) |
| `/loop` | coding/loop.py | main, LoopMode.INTERACTIVE, LoopMode.AUTONOMOUS |
| `/doctor` | doctor.py | cmd_diagnose, cmd_stabilize, cmd_baseline, cmd_qa, cmd_solidify, cmd_guided_flow |
| `/docs` | docs.py | backfill_docs, cmd_status, cmd_generate |
| `/memory` | memory.py | read_memory, clear_memory, search_learnings, search_archive |

### Adding a New Shell Command

1. Create the command handler with `@register_command("name", aliases=[...])`
2. Import the backing module functions
3. Parse subcommands and flags
4. Call the appropriate module function
5. Return user-friendly output
6. Update cmd_help() with the new command

### Mandatory: Shell Command Maintenance Checklist

When adding or modifying ANY harness command, you MUST complete ALL of these steps:

**Step 1: Register in shell_commands.py**
```python
@register_command("mycommand", aliases=["mc"])
def cmd_mycommand(args: str) -> str:
    ...
```

**Step 2: Update cmd_help()**
Add the command to the main help text in `cmd_help()` (around line 50).

**Step 3: Add embedded help**
The command should have its own help subcommand that shows detailed usage.

**Step 4: Update CLAUDE.md mapping table**
Add an entry to the "Current Command → Module Mapping" table above.

**Step 5: Search for old references**
```bash
grep -r "python harness/" harness/ README.md
```
Replace any references to direct Python invocation with shell command equivalents.

**Why this matters:** The shell commands are the user interface. If `/help` is incomplete or documentation references `python harness/X.py` instead of `/X`, users get confused.

### Testing Shell Commands

After modifying shell commands:
```bash
# Test the command runs
python -c "from harness.shell_commands import handle_slash_command; print(handle_slash_command('/help'))"

# Test specific command
python -c "from harness.shell_commands import handle_slash_command; print(handle_slash_command('/memory status'))"
```
