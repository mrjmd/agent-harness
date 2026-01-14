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
