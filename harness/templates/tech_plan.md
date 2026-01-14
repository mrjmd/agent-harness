# Technical Plan

> Replace this template with your actual technical architecture decisions.

## Overview

**Project:** [Project Name]
**Tech Lead:** [Name/Agent]
**Last Updated:** [Date]

## Architecture Decision Records (ADRs)

### ADR-001: [Technology Choice]

**Status:** Accepted
**Context:** [What is the issue we're facing?]
**Decision:** [What is the change that we're proposing?]
**Consequences:** [What are the trade-offs?]

---

## Tech Stack

### Frontend

| Category | Technology | Version | Rationale |
|----------|-----------|---------|-----------|
| Framework | [e.g., Next.js] | [e.g., 15.x] | [Why chosen] |
| Styling | [e.g., Tailwind CSS] | [e.g., 4.x] | [Why chosen] |
| State | [e.g., Zustand] | [e.g., 5.x] | [Why chosen] |

### Backend

| Category | Technology | Version | Rationale |
|----------|-----------|---------|-----------|
| Runtime | [e.g., Node.js] | [e.g., 22.x] | [Why chosen] |
| Framework | [e.g., Hono] | [e.g., 4.x] | [Why chosen] |
| Database | [e.g., PostgreSQL] | [e.g., 16.x] | [Why chosen] |

### Infrastructure

| Category | Technology | Rationale |
|----------|-----------|-----------|
| Hosting | [e.g., Vercel] | [Why chosen] |
| CI/CD | [e.g., GitHub Actions] | [Why chosen] |

## Project Structure

```
.
├── src/
│   ├── app/              # Next.js app router pages
│   ├── components/       # React components
│   │   ├── ui/          # Reusable UI primitives
│   │   └── features/    # Feature-specific components
│   ├── lib/             # Utility functions and helpers
│   ├── hooks/           # Custom React hooks
│   └── types/           # TypeScript type definitions
├── tests/
│   └── e2e/             # Playwright end-to-end tests
├── specs/               # Product and feature specifications
└── harness/             # Autonomous agent tooling
```

## Data Model

### Entity: [Entity Name]

```typescript
interface Entity {
  id: string;
  createdAt: Date;
  updatedAt: Date;
  // Add fields
}
```

## API Design

### Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | /api/resource | List resources | Required |
| POST | /api/resource | Create resource | Required |

## Testing Strategy

### Unit Tests
- Framework: Vitest
- Coverage target: 80%

### E2E Tests
- Framework: Playwright
- Coverage: All user flows from product spec

### Test File Naming

```
tests/e2e/test_<feature-id>.spec.ts
```

## Security Considerations

- [ ] Authentication method: [e.g., JWT, session]
- [ ] Authorization model: [e.g., RBAC, ABAC]
- [ ] Data encryption: [at rest, in transit]
- [ ] Input validation: [approach]

## Performance Requirements

- Page load: < 3s on 3G
- API response: < 200ms p95
- Lighthouse score: > 90

## Deployment

### Environments

| Environment | URL | Branch |
|-------------|-----|--------|
| Development | localhost:3000 | feature/* |
| Staging | staging.example.com | main |
| Production | example.com | release/* |

### Environment Variables

```env
# Required
DATABASE_URL=
API_KEY=

# Optional
DEBUG=false
```

## Dependencies

### Critical Dependencies

| Package | Purpose | Risk Level |
|---------|---------|------------|
| [pkg] | [purpose] | [low/medium/high] |

## Open Technical Questions

- [ ] [Question 1]
- [ ] [Question 2]
