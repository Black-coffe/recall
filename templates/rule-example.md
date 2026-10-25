---
paths: ["src/api/**", "server/**"]
---
# API area rules (EXAMPLE - a shape to copy into .claude/rules/<area>.md, not a rule)

- Every new endpoint gets an authz check before any IO; see docs/wiki for the project's auth invariants.
- Validation errors return the shared error envelope - never ad-hoc shapes.
- New external calls go through the existing client wrappers (retry/timeout policy lives there, not at call sites).
