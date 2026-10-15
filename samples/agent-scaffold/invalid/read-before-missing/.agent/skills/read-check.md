---
id: "read-check"
purpose: "Fixture Skill for readBefore validation."
triggers:
  - "test"
excludes:
  - "test"
inputs:
  - "question"
outputs:
  - "answer"
sideEffects: "none"
confirmation: "not-required"
implementation: "Fixture only."
readBefore:
  - "missing.md"
---

# Invalid missing readBefore fixture
