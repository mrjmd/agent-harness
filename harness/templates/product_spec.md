# Product Specification

> Replace this template with your actual product specification.

## Overview

**Product Name:** [Your Product Name]
**Version:** 0.1.0
**Last Updated:** [Date]

## Problem Statement

What problem does this product solve? Who experiences this problem?

## Target Users

- **Primary:** [Description of primary user persona]
- **Secondary:** [Description of secondary user persona]

## Goals

1. [Goal 1]
2. [Goal 2]
3. [Goal 3]

## Non-Goals

What is explicitly out of scope?

- [Non-goal 1]
- [Non-goal 2]

## Features

### MVP Features (Must Have)

| ID | Feature | Description | Acceptance Criteria |
|----|---------|-------------|---------------------|
| F001 | [Feature Name] | [Description] | [Criteria] |
| F002 | [Feature Name] | [Description] | [Criteria] |

### Post-MVP Features (Nice to Have)

| ID | Feature | Description |
|----|---------|-------------|
| F101 | [Feature Name] | [Description] |

## User Flows

### Flow 1: [Primary User Flow Name]

1. User does X
2. System responds with Y
3. User sees Z

## Success Metrics

How will we know if this product is successful?

- **Metric 1:** [Description]
- **Metric 2:** [Description]

## Constraints

- Technical: [Any technical limitations]
- Timeline: [Any time constraints]
- Resources: [Any resource constraints]

## Open Questions

- [ ] [Question 1]
- [ ] [Question 2]

---

## Conversion to features.json

Once this spec is approved, convert each MVP feature into an entry in `specs/features.json`:

```json
{
  "id": "f001-feature-name",
  "description": "As a user, I can [action] so that [benefit]",
  "status": "todo",
  "test_file": "tests/e2e/test_f001-feature-name.spec.ts"
}
```
